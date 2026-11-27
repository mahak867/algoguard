"""The guard: every signal passes the anomaly checker, the drift state and then the hard rules.

Order of authority, highest first:
  1. Hard rules (algoguard.rules). Any BLOCK there is final. No model output is an input to them.
  2. Exact duplicate check. BLOCK.
  3. Everything learned (anomaly model, context checks, drift). These can only HOLD or shrink an order.
If a model raises or is slow, the guard records it and decides on the rules alone: a broken AI makes the
system more cautious, never less.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from algoguard.anomaly import AnomalyChecker, AnomalyConfig
from algoguard.confirm import Confirmations
from algoguard.drift import Baseline, DriftConfig, DriftDetector, LiveTrade
from algoguard.explain import Explainer
from algoguard.models import Account, Decision, Finding, Outcome, Signal
from algoguard.rules import Limits, check as check_rules


@dataclass
class Bot:
    bot_id: str
    secret: str
    limits: Limits = field(default_factory=Limits)
    drift_config: DriftConfig = field(default_factory=DriftConfig)
    drift: DriftDetector | None = None
    realised_pnl_today: float = 0.0
    pnl_day: str = ""

    def set_baseline(self, returns, market_vol: float | None = None) -> None:
        self.drift = DriftDetector(Baseline.from_returns(returns, market_vol), self.drift_config)


@dataclass
class Held:
    decision: Decision
    account: Account
    token: str
    created: float


class Guard:
    def __init__(self, secret_key: str, hold_seconds: int = 120, anomaly: AnomalyConfig | None = None,
                 explainer: Explainer | None = None):
        self.bots: dict[str, Bot] = {}
        self.anomaly = AnomalyChecker(anomaly)
        self.confirm = Confirmations(secret_key, hold_seconds)
        self.explainer = explainer or Explainer()
        self.kill_switch = False
        self.held: dict[str, Held] = {}
        self.log: list[Decision] = []

    def add_bot(self, bot: Bot) -> Bot:
        self.bots[bot.bot_id] = bot
        return bot

    def _drift_findings(self, bot: Bot, sig: Signal) -> list[Finding]:
        if bot.drift is None or not bot.drift.state.trades:
            return []
        r = bot.drift.report()
        facts = {"score": r["score"], "trades": r["trades"], "cause": r["cause"]}
        if r["level"] == "act" and r["action"] == "pause":
            return [Finding("drift_pause", "drift", "hold", f"Drift {r['score']}/100 ({r['cause']}): your rule is to "
                            "pause this bot, so new orders wait for you.", facts)]
        if r["level"] == "act" and r["action"] == "halve_size":
            return [Finding("drift_halve", "drift", "info", f"Drift {r['score']}/100 ({r['cause']}): applying your "
                            "rule, order size halved.", facts)]
        if r["level"] in ("act", "warn"):
            return [Finding("drift_warning", "drift", "info", f"Drift {r['score']}/100: live results are moving "
                            f"away from the backtest ({r['cause']}).", facts)]
        return []

    def process(self, sig: Signal, acct: Account, now: float | None = None) -> Decision:
        t0 = time.perf_counter()
        sig.received_at = sig.received_at or now or time.time()
        bot = self.bots.get(sig.bot_id)
        if bot is None:
            raise KeyError(f"unknown bot {sig.bot_id}")
        self._roll_day(bot, sig.received_at)
        acct.realised_pnl_today = min(acct.realised_pnl_today, bot.realised_pnl_today)

        findings: list[Finding] = []
        score = None
        # Learned and contextual checks. Any failure here falls back to rules only.
        try:
            f, score = self.anomaly.check(sig, acct)
            findings += f
        except Exception as e:  # noqa: BLE001
            findings.append(Finding("model_unavailable", "system", "info",
                                    f"The anomaly model failed ({type(e).__name__}); decided on your rules alone.", {}))
        try:
            dfs = self._drift_findings(bot, sig)
        except Exception as e:  # noqa: BLE001
            dfs = [Finding("drift_unavailable", "system", "info", f"Drift check failed ({type(e).__name__}).", {})]
        findings += dfs
        opening = (acct.position.size == 0) or ((sig.side == "buy") == (acct.position.size > 0))
        if any(f.code == "drift_halve" for f in dfs) and opening and not sig.reduce_only:
            half = sig.size // 2
            if half >= 1:
                sig.raw.setdefault("original_size", sig.size)
                sig.size = half
            else:
                findings.append(Finding("drift_zero_size", "drift", "hold", "Halving this order leaves less than one "
                                        "contract, so it waits for you.", {}))

        # Hard rules last, on the (possibly smaller) order. Their BLOCKs are final.
        findings += check_rules(sig, acct, bot.limits, self.kill_switch)

        sev = {f.severity for f in findings}
        outcome = Outcome.BLOCK if "block" in sev else Outcome.HOLD if "hold" in sev else Outcome.PASS
        d = Decision(outcome, sig, findings, score)
        d.explanation = self.explainer.explain(d)
        if outcome is Outcome.HOLD:
            d.hold_id = uuid.uuid4().hex[:12]
            self.held[d.hold_id] = Held(d, acct, self.confirm.token(d.hold_id, sig, sig.received_at), sig.received_at)
        if outcome is Outcome.PASS:
            self.anomaly.record(sig, acct)
        d.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        self.log.append(d)
        return d

    def expire_holds(self, now: float | None = None) -> list[Decision]:
        """Drop HOLDs nobody answered in time and log each one: an unanswered hold is never sent."""
        now = now or time.time()
        out = []
        for hid, h in list(self.held.items()):
            if now - h.created > self.confirm.seconds:
                del self.held[hid]
                d = Decision(Outcome.BLOCK, h.decision.signal,
                             [Finding("hold_expired", "system", "block",
                                      f"Not confirmed within {self.confirm.seconds} s, so it was never sent.", {})],
                             h.decision.anomaly_score)
                d.explanation = self.explainer.explain(d)
                d.hold_id = hid
                self.log.append(d)
                out.append(d)
        return out

    def maintain(self, bot_id: str) -> None:
        """Slow upkeep (model refits). The API runs it after the response is sent, off the order path."""
        try:
            self.anomaly.refit_if_due(bot_id)
        except Exception:  # noqa: BLE001 - a failed refit keeps the previous model
            pass

    def resolve_hold(self, hold_id: str, token: str, approve: bool, now: float | None = None) -> Decision:
        """The trader's answer to a HOLD. A confirmed order is re-checked against the hard rules first."""
        h = self.held.pop(hold_id, None)
        if h is None:
            raise KeyError("no such hold (already answered or expired)")
        self.confirm.check(hold_id, h.decision.signal, token, now)
        sig = h.decision.signal
        self.anomaly.feedback(sig.bot_id, {f.code for f in h.decision.findings}, approve)
        if not approve:
            d = Decision(Outcome.BLOCK, sig, [Finding("rejected_by_trader", "system", "block",
                                                      "You rejected this order.", {})], h.decision.anomaly_score)
        else:
            bot = self.bots[sig.bot_id]
            rf = check_rules(sig, h.account, bot.limits, self.kill_switch)
            blocked = any(f.severity == "block" for f in rf)
            note = Finding("confirmed_by_trader", "system", "info", "You confirmed this order.", {})
            d = Decision(Outcome.BLOCK if blocked else Outcome.PASS, sig, [note, *rf], h.decision.anomaly_score)
            if not blocked:
                self.anomaly.record(sig, h.account)
        d.explanation = self.explainer.explain(d)
        self.log.append(d)
        return d

    @staticmethod
    def _roll_day(bot: Bot, t: float) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(t))  # the trading day is UTC; make it configurable later
        if bot.pnl_day != day:
            bot.pnl_day, bot.realised_pnl_today = day, 0.0

    def record_trade(self, bot_id: str, trade: LiveTrade, pnl: float = 0.0, now: float | None = None) -> dict | None:
        """A closed trade: feeds the drift detector and today's loss for the daily-loss rule."""
        bot = self.bots[bot_id]
        self._roll_day(bot, now or time.time())
        bot.realised_pnl_today += pnl
        return bot.drift.update(trade) if bot.drift else None
