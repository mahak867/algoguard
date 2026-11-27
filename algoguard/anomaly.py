"""Alert anomaly checker: should this alert become an order?

Three layers, cheapest first:
  1. Exact checks. Duplicate (same order from the same bot inside a short window), stale (the alert is old,
     or the price it saw is far from the mark price now), and a burst of alerts.
  2. Context. Does the alert make sense for the position the trader actually holds? An alert that would flip
     a bot that has never flipped, or open a short on a bot that has only ever been long, is suspicious.
  3. Learned. An Isolation Forest per bot, trained on that bot's own past orders (size, side, hour of day,
     time since the last order, leverage). New bots with little history fall back to simple size ratios.

Only exact duplicates BLOCK. Everything else here can only HOLD an order for the trader to confirm: the AI
makes things stricter, never looser. The final veto belongs to algoguard.rules.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from sklearn.ensemble import IsolationForest

from algoguard.models import Account, Finding, Signal


@dataclass
class AnomalyConfig:
    duplicate_window_s: float = 15.0
    max_alert_age_s: float = 60.0
    max_price_drift_pct: float = 1.0     # alert price vs mark now, for market orders
    burst_window_s: float = 60.0
    burst_limit: int = 5
    size_ratio_hold: float = 3.0         # more than 3x the bot's largest past order
    min_history_for_model: int = 30
    hold_score: float = 0.97             # starting point: more unusual than 97% of the bot's own history
    hold_score_min: float = 0.90         # the trader's answers move each bot's threshold within these bounds
    hold_score_max: float = 0.995
    feedback_step: float = 0.005
    refit_every: int = 20


@dataclass
class _Past:
    t: float
    side: int
    size: int
    leverage: float
    position_after: int


@dataclass
class BotProfile:
    """What a bot normally does. Built from the orders it actually sent."""
    history: list[_Past] = field(default_factory=list)
    seen: deque = field(default_factory=lambda: deque(maxlen=500))  # (t, key) of every alert received
    model: IsolationForest | None = None
    hold_score: float | None = None      # this bot's own threshold, tuned by the trader's answers to HOLDs
    confirmed: int = 0
    rejected: int = 0
    train_scores: np.ndarray | None = None
    fitted_on: int = 0

    def features(self, t: float, side: int, size: int, leverage: float, prev_t: float | None) -> list[float]:
        hour = (t % 86400) / 3600
        # Seconds since the previous alert, capped at a day: a quiet weekend should not make every later
        # alert look strange.
        gap = min(max(t - prev_t, 1.0), 86400.0) if prev_t else 3600.0
        return [math.log1p(size), side, math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24),
                math.log(gap), leverage]

    def matrix(self) -> np.ndarray:
        rows, prev = [], None
        for p in self.history:
            rows.append(self.features(p.t, p.side, p.size, p.leverage, prev))
            prev = p.t
        return np.array(rows)


def _key(sig: Signal) -> tuple:
    return (sig.symbol, sig.side, sig.size, sig.order_type, round(sig.price or 0.0, 2), sig.reduce_only)


class AnomalyChecker:
    def __init__(self, config: AnomalyConfig | None = None):
        self.cfg = config or AnomalyConfig()
        self.profiles: dict[str, BotProfile] = {}

    def profile(self, bot_id: str) -> BotProfile:
        return self.profiles.setdefault(bot_id, BotProfile())

    # ── called for every alert, before the rules ──
    def check(self, sig: Signal, acct: Account) -> tuple[list[Finding], float | None]:
        c, p = self.cfg, self.profile(sig.bot_id)
        now = sig.received_at or time.time()
        out: list[Finding] = []

        def add(code, sev, msg, **facts):
            out.append(Finding(code, "anomaly", sev, msg, facts))

        # 1. exact checks
        key = _key(sig)
        dup = next((t for t, k in reversed(p.seen) if k == key and now - t <= c.duplicate_window_s), None)
        prev_alert = p.seen[-1][0] if p.seen else None
        p.seen.append((now, key))
        if dup is not None:
            add("duplicate", "block", f"Duplicate of an identical alert {now - dup:.1f} s earlier; not sent.",
                seconds_apart=round(now - dup, 1), window_s=c.duplicate_window_s)
        if sig.sent_at:
            age = now - sig.sent_at
            if age > c.max_alert_age_s:
                add("stale_alert", "hold", f"The alert is {age:.0f} s old; alerts older than {c.max_alert_age_s:g} s "
                    "may act on a price that has moved.", age_s=round(age), limit_s=c.max_alert_age_s)
        if sig.price and acct.mark_price and sig.order_type == "market":
            drift = abs(sig.price - acct.mark_price) / acct.mark_price * 100
            if drift > c.max_price_drift_pct:
                add("stale_price", "hold", f"The alert saw {sig.price:g} but the mark price is now "
                    f"{acct.mark_price:g} ({drift:.1f}% away).", alert_price=sig.price, mark=acct.mark_price,
                    drift_pct=round(drift, 1), limit_pct=c.max_price_drift_pct)
        recent = sum(1 for t, _ in p.seen if now - t <= c.burst_window_s)
        if recent > c.burst_limit:
            add("burst", "hold", f"{recent} alerts in the last {c.burst_window_s:g} s; this bot's limit is "
                f"{c.burst_limit}.", alerts=recent, limit=c.burst_limit)

        # 2. context: the alert against the real position
        side = 1 if sig.side == "buy" else -1
        pos = acct.position.size
        after = pos + side * sig.size
        hist = p.history
        if hist:
            ever_long = any(h.position_after > 0 for h in hist)
            ever_short = any(h.position_after < 0 for h in hist)
            flipped = pos != 0 and after != 0 and (pos > 0) != (after > 0)
            if flipped and not any(a.position_after * b.position_after < 0 for a, b in zip(hist, hist[1:])):
                add("unexpected_flip", "hold", f"This order would flip the position from {pos} to {after} "
                    "contracts; this bot has never flipped directly.", position=pos, position_after=after)
            elif after < 0 and not ever_short and not sig.reduce_only:
                add("unexpected_short", "hold", f"This order would open a short ({after} contracts); this bot has "
                    "only ever been long. The alert may not know the current position.", position=pos,
                    position_after=after)
            elif after > 0 and not ever_long and not sig.reduce_only:
                add("unexpected_long", "hold", f"This order would open a long ({after} contracts); this bot has "
                    "only ever been short.", position=pos, position_after=after)

        # 3. learned profile
        score = None
        if hist:
            biggest = max(h.size for h in hist)
            ratio = sig.size / biggest
            if ratio > c.size_ratio_hold:
                add("size_unusual", "hold", f"{sig.size} contracts is {ratio:.0f}x this bot's largest order "
                    f"({biggest}).", size=sig.size, largest=biggest, ratio=round(ratio, 1))
        if len(hist) >= c.min_history_for_model and p.model is None:
            self._fit(p)  # first fit only; later refits run off the order path (refit_if_due)
        if p.model is not None:
            # Spacing is measured from the previous alert received, not the previous order sent; otherwise a few
            # held alerts in a row would make every later alert look unusual and the holds would cascade.
            x = np.array([p.features(now, side, sig.size, sig.leverage or acct.position.leverage, prev_alert)])
            s = p.model.score_samples(x)[0]
            score = float(np.mean(p.train_scores > s))  # share of the bot's own history that looks more normal
            if score >= (p.hold_score if p.hold_score is not None else c.hold_score):
                add("model_unusual", "hold", f"This order looks unlike {score:.0%} of this bot's past orders "
                    "(size, side, time of day, spacing, leverage).", unusual_pct=round(score * 100))
        else:
            out.append(Finding("cold_start", "anomaly", "info", f"Only {len(hist)} past orders; the learned check "
                               f"starts at {c.min_history_for_model}.", {"history": len(hist)}))
        return out, score

    def feedback(self, bot_id: str, findings_codes: set[str], approved: bool) -> float:
        """The trader answered a HOLD. Confirming a model-flagged order means it was a false alarm for this bot,
        so the bot's threshold rises a little; rejecting one means the model was right, so it falls. Bounded both
        ways, and it only affects the learned check, never the exact checks or the hard rules."""
        c, p = self.cfg, self.profile(bot_id)
        cur = p.hold_score if p.hold_score is not None else c.hold_score
        if "model_unusual" in findings_codes:
            cur += c.feedback_step if approved else -c.feedback_step
        p.hold_score = min(max(cur, c.hold_score_min), c.hold_score_max)
        if approved:
            p.confirmed += 1
        else:
            p.rejected += 1
        return p.hold_score

    def refit_if_due(self, bot_id: str) -> bool:
        """Retrain a bot's model once enough new orders have arrived. Call it after the order is sent, not before."""
        p = self.profile(bot_id)
        if len(p.history) >= self.cfg.min_history_for_model and (
                p.model is None or len(p.history) - p.fitted_on >= self.cfg.refit_every):
            self._fit(p)
            return True
        return False

    def _fit(self, p: BotProfile) -> None:
        X = p.matrix()
        p.model = IsolationForest(n_estimators=100, random_state=7).fit(X)
        p.train_scores = p.model.score_samples(X)
        p.fitted_on = len(p.history)

    # ── called once an order is actually sent ──
    def record(self, sig: Signal, acct: Account) -> None:
        side = 1 if sig.side == "buy" else -1
        self.profile(sig.bot_id).history.append(_Past(sig.received_at or time.time(), side, sig.size,
                                                      sig.leverage or acct.position.leverage,
                                                      acct.position.size + side * sig.size))
