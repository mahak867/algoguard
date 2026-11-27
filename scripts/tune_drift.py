"""Measure the drift detector's false-alarm rate and detection delay on simulated trades.

    python scripts/tune_drift.py [runs]

Null case: 30 live trades drawn from the same distribution as the backtest (nothing changed).
Shift case: live trades from a losing regime (mean -1.2%, sd 2.5%) against a +0.4% / 1.5% backtest.
Synthetic data only: rerun on real backtests and live fills before quoting numbers about real bots.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # run from anywhere without PYTHONPATH

import sys

import numpy as np

from algoguard.drift import Baseline, DriftConfig, DriftDetector, LiveTrade


def first(levels, name):
    return next((i + 1 for i, lv in enumerate(levels) if lv == name or (name == "warn" and lv == "act")), None)


def main(runs: int = 200) -> dict:
    base = Baseline.from_returns(np.random.default_rng(3).normal(0.4, 1.5, 200))
    cfg = DriftConfig(bootstrap_sims=500)
    warn = act = 0
    delays = []
    for s in range(runs):
        d = DriftDetector(base, cfg)
        lv = [d.update(LiveTrade(float(r)))["level"] for r in np.random.default_rng(s).normal(0.4, 1.5, 30)]
        warn += first(lv, "warn") is not None
        act += first(lv, "act") is not None
        d = DriftDetector(base, cfg)
        lv = [d.update(LiveTrade(float(r)))["level"] for r in np.random.default_rng(10_000 + s).normal(-1.2, 2.5, 30)]
        delays.append(first(lv, "act") or 31)
    out = {"runs": runs, "false_warn_rate": warn / runs, "false_act_rate": act / runs,
           "median_trades_to_act": float(np.median(delays)), "p90_trades_to_act": float(np.percentile(delays, 90))}
    print(out)
    return out


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 200)
