"""Replay a simulated TradingView bot through AlgoGuard, with faults injected, and measure it.

    python scripts/replay_demo.py [--seed N] [--quiet]

The bot is long-only on BTCUSD: it buys 8-12 contracts and sells back to flat, every 2 hours or so.
It runs 80 normal alerts to learn the bot, then a test stream of normal alerts with faults mixed in:
duplicate, 10x size typo, stale alert, price far from mark, a short the bot never takes, 25x leverage,
a limit price typo, and a burst. Prints each fault's outcome and explanation, then:
  * faults caught (HOLD or BLOCK) - the deck's target is >= 95%
  * normal alerts held or blocked - target < 5%
  * added latency per order - target < 100 ms
All data is simulated. These numbers show the prototype works as designed; they are not results on real bots.
"""
from __future__ import annotations

import argparse
import random
import statistics

from algoguard.models import Account, Outcome, Position, Signal
from algoguard.pipeline import Bot, Guard
from algoguard.rules import Limits

T0 = 1_790_000_000.0
MARK = 60_000.0


class Sim:
    def __init__(self, seed: int):
        self.rnd = random.Random(seed)
        self.pos, self.t, self.mark = 0, T0, MARK
        self.g = Guard("demo-secret")
        self.g.add_bot(Bot("trend", "s", Limits(max_daily_loss=500, max_leverage=5)))

    def acct(self) -> Account:
        return Account(10_000.0, 8_000.0, self.mark, 0.001, Position("BTCUSD", self.pos, MARK, None, 5.0))

    def tick(self, gap: float = 7200) -> None:
        self.t += gap + self.rnd.uniform(-900, 900)
        self.mark *= 1 + self.rnd.gauss(0, 0.004)

    def normal(self) -> Signal:
        size = self.rnd.randint(8, 12) if self.pos == 0 else self.pos
        side = "buy" if self.pos == 0 else "sell"
        return Signal("trend", "BTCUSD", side, size, price=round(self.mark, 1), sent_at=self.t - self.rnd.uniform(0.5, 3),
                      received_at=self.t)

    def run(self, s: Signal):
        d = self.g.process(s, self.acct())
        self.g.maintain("trend")  # as in the API: after the decision, outside the measured latency
        if d.outcome is Outcome.PASS:
            self.pos += s.size if s.side == "buy" else -s.size
        return d


def _short(s: Signal, sim: Sim) -> None:
    s.side = "sell"
    s.size = 10 if sim.pos == 0 else sim.pos + 10  # opens a short, or flips a long into one


def _price_typo(s: Signal, sim: Sim) -> None:
    s.order_type, s.price = "limit", round(sim.mark / 10, 1)


def _stale(s: Signal, sim: Sim) -> None:
    s.sent_at = s.received_at - 240


def _far_price(s: Signal, sim: Sim) -> None:
    s.price = round(sim.mark * 0.97, 1)


def _size10(s: Signal, sim: Sim) -> None:
    s.size *= 10


def _lev(s: Signal, sim: Sim) -> None:
    s.side, s.size, s.leverage = "buy", 10, 25  # an opening order; closing orders are always allowed


# Each fault takes the bot's next normal alert and breaks it in one way.
FAULTS = [("10x size typo", _size10), ("stale alert (4 min old)", _stale), ("alert price 3% from mark", _far_price),
          ("short or flip the bot never takes", _short), ("asks for 25x leverage", _lev),
          ("limit price typo (1/10)", _price_typo)]


def main(seed: int = 7, quiet: bool = False) -> dict:
    sim = Sim(seed)
    for _ in range(80):  # learning period
        sim.tick()
        sim.run(sim.normal())

    lat, normal_flagged, normal_n = [], 0, 0
    rows, missed, per = [], [], {}

    def tally(name, d):
        per.setdefault(name, [0, 0])
        per[name][0] += 1
        per[name][1] += d.outcome is not Outcome.PASS
        if d.outcome is Outcome.PASS:
            missed.append(name)
        if per[name][0] == 1:
            rows.append((name, d.outcome.value, d.explanation))

    def record(name, s):
        pos = sim.pos  # a fault that slips through must not change the simulated position
        d = sim.run(s)
        sim.pos = pos
        lat.append(d.latency_ms)
        tally(name, d)
    for round_ in range(12):
        for _ in range(8):
            sim.tick()
            d = sim.run(sim.normal())
            lat.append(d.latency_ms)
            normal_n += 1
            normal_flagged += d.outcome is not Outcome.PASS
            if d.outcome is not Outcome.PASS and not quiet:
                print(f"  [normal alert flagged] {d.outcome.value}: {d.explanation}")
        # a duplicate: the same normal alert arrives twice, 2 s apart
        sim.tick()
        s = sim.normal()
        lat.append(sim.run(s).latency_ms)
        dup = Signal(**{**s.__dict__, "received_at": s.received_at + 2, "raw": {}})
        record("duplicate (second copy)", dup)
        for name, breaker in FAULTS:
            sim.tick()
            s = sim.normal()
            breaker(s, sim)
            record(name, s)
        # a burst: six alerts in 30 seconds
        sim.tick()
        for i in range(6):
            s = sim.normal(); s.size = 8 + i; s.received_at = s.sent_at = sim.t + i * 5
            pos = sim.pos
            d = sim.run(s)
            sim.pos = pos
            if i == 5:
                tally("burst: 6th alert in 30 s", d)

    fault_n = sum(v[0] for v in per.values())
    caught = sum(v[1] for v in per.values())
    if not quiet:
        print("\nFault replay (first round):")
        for name, out, why in rows:
            print(f"- {name:36s} {out:5s}  {why}")
    lat.sort()
    res = {
        "faults": fault_n, "faults_caught_pct": round(100 * caught / fault_n, 1),
        "normal_alerts": normal_n, "normal_flagged_pct": round(100 * normal_flagged / normal_n, 1),
        "latency_ms_p50": round(statistics.median(lat), 2), "latency_ms_p99": round(lat[int(0.99 * (len(lat) - 1))], 2),
    }
    res["caught_by_fault"] = {k: f"{v[1]}/{v[0]}" for k, v in per.items()}
    print("\nResult (simulated data):", res)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    main(a.seed, a.quiet)
