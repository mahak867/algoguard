"""The deck's slide-4 week, run through the real AlgoGuard code.

    python scripts/demo_week.py

A part-time trader runs a TradingView trend bot on BTC perpetual futures (Delta India BTCUSD, 1 lot = 0.001 BTC).
The bot normally trades 50-60 lots. Every decision below is made by algoguard.pipeline.Guard; the explanations are
the explainer's own text. Prices, sizes and P&L are simulated; amounts are in the account's settlement currency (USD).
"""
from __future__ import annotations

import random
import sys

import numpy as np

from algoguard.drift import DriftConfig, LiveTrade
from algoguard.models import Account, Outcome, Position, Signal
from algoguard.pipeline import Bot, Guard
from algoguard.rules import Limits

T0 = 1_790_035_200.0  # a Monday, 00:00 UTC
DAY = 86_400.0
COLOR = {"PASS": "green", "HOLD": "amber", "BLOCK": "red", "DRIFT": "blue", "": "plain"}


class Week:
    def __init__(self, seed: int = 3):
        self.rnd = random.Random(seed)
        self.g = Guard("demo")
        self.bot = self.g.add_bot(Bot("btc-trend", "k", Limits(max_daily_loss=300, max_leverage=5, max_order_notional=50_000),
                                      DriftConfig(action="halve_size")))
        self.bot.set_baseline(np.random.default_rng(1).normal(0.4, 1.5, 200), market_vol=2.0)
        self.pos, self.mark, self.t = 0, 61_500.0, T0
        self.lines: list[tuple[str, str]] = []

    def say(self, text: str, kind: str = "") -> None:
        self.lines.append((text, COLOR.get(kind, "plain")))

    def acct(self) -> Account:
        return Account(20_000.0, 16_000.0, self.mark, 0.001, Position("BTCUSD", self.pos, self.mark, None, 5.0), 0.0, 0.25)

    def alert(self, side: str, lots: int, at: float, sent_ago: float = 1.5, price: float | None = None) -> object:
        s = Signal("btc-trend", "BTCUSD", side, lots, price=price or round(self.mark, 1), sent_at=at - sent_ago,
                   received_at=at)
        d = self.g.process(s, self.acct(), now=at)
        self.g.maintain("btc-trend")
        if d.outcome is Outcome.PASS:
            self.pos += s.size if side == "buy" else -s.size
        return d

    def learn(self, n: int = 60) -> None:
        """The bot's recent history: long-only, 50-60 lots, an order every couple of hours."""
        t = T0 + DAY - n * 7200  # ends late Monday, just before the week's first alert
        for _ in range(n):
            t += 7200 + self.rnd.uniform(-900, 900)
            lots = self.rnd.randint(50, 60) if self.pos == 0 else self.pos
            self.alert("buy" if self.pos == 0 else "sell", lots, t)

    def run(self) -> list[tuple[str, str]]:
        self.learn()
        self.say("AlgoGuard demo: one week with a BTC futures trend bot (Delta India BTCUSD, simulated)")
        self.say("")
        self.say("MON  Set up: same Delta alert JSON, new webhook URL. Limits: max daily loss 300, max 5x,")
        self.say("     15% from liquidation. Backtest: 200 trades uploaded. Bot history: 60 orders learned.")
        self.say("")

        # Tue 02:10: the first alert of the week, spaced like the bot's usual orders (about 2 h after the last)
        t = self.g.anomaly.profile("btc-trend").history[-1].t + 7200
        if self.pos > 0:
            self.alert("sell", self.pos, t - 3600)  # close Monday's trade
            t += 3600
        d1 = self.alert("buy", 55, t)
        d2 = self.alert("buy", 55, t + 3)
        self.say(f"TUE 02:10  alert: buy 55 lots  ->  {d1.outcome.value}", d1.outcome.value)
        self.say(f"TUE 02:10  same alert again, 3 s later  ->  {d2.outcome.value}", d2.outcome.value)
        self.say(f"     {d2.explanation}")
        self.say("")

        t = T0 + 2 * DAY + 14 * 3600 + 32 * 60  # Wed 14:32
        if self.pos > 0:
            self.alert("sell", self.pos, t)  # the normal exit of Tuesday's trade
        d = self.alert("buy", 500, t + 7200)
        if d.hold_id is None:
            raise SystemExit(f"expected a HOLD, got {d.outcome.value}: {d.explanation}")
        self.say(f"WED 14:32  alert: buy 500 lots (0.5 BTC; a decimal slip)  ->  {d.outcome.value}", d.outcome.value)
        self.say(f"     {d.explanation}")
        r = self.g.resolve_hold(d.hold_id, self.g.held[d.hold_id].token, approve=False, now=t + 7230)
        self.say(f"     Trader taps Reject on Telegram  ->  {r.outcome.value}, never sent", r.outcome.value)
        self.say("")

        self.say("THU  Volatility doubles after a macro event. Live trades stop matching the backtest:", "")
        level = None
        for i, ret in enumerate(np.random.default_rng(7).normal(-1.4, 2.5, 12), 1):
            rep = self.g.record_trade("btc-trend", LiveTrade(float(ret), 6.0, 4.1), pnl=0.0, now=T0 + 3 * DAY + i * 600)
            if rep["level"] == "act":
                level = rep
                self.say(f"     after {i} trades: drift {rep['score']}/100, cause: {rep['cause']}, "
                         f"action: {rep['action']}", "DRIFT")
                break
        d = self.alert("buy", 56, T0 + 3 * DAY + 15 * 3600)
        halved = d.signal.raw.get("original_size")
        self.say(f"     next alert: buy 56 lots  ->  {d.outcome.value} with {d.signal.size} lots"
                 + (f" (halved from {halved})" if halved else ""), d.outcome.value)
        self.say("")

        t = T0 + 4 * DAY + 11 * 3600  # Fri
        self.pos = 0
        self.g.record_trade("btc-trend", LiveTrade(-2.0), pnl=-310.0, now=t - 60)
        d = self.alert("buy", 55, t)
        self.say(f"FRI  Losses today: 310 (limit 300). alert: buy 55 lots (drift rule: {d.signal.size})  ->  "
                 f"{d.outcome.value}", d.outcome.value)
        self.say(f"     {d.explanation[:150]}")
        self.say("")
        lat = sorted(x.latency_ms for x in self.g.log)
        self.say(f"{len(self.g.log)} decisions this run; added time per order: median {lat[len(lat)//2]:.1f} ms, "
                 f"max {lat[-1]:.1f} ms")
        assert level is not None
        return self.lines


ANSI = {"green": "\033[32m", "amber": "\033[33m", "red": "\033[31m", "blue": "\033[36m", "plain": ""}

if __name__ == "__main__":
    tty = sys.stdout.isatty()
    for text, color in Week().run():
        print((ANSI[color] + text + "\033[0m") if tty and color != "plain" else text)
