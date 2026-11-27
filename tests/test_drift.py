import numpy as np
import pytest

from algoguard.drift import Baseline, DriftConfig, DriftDetector, LiveTrade, bootstrap_percentile, parse_tradingview_csv


def backtest(n=200, seed=3):
    rng = np.random.default_rng(seed)
    return rng.normal(0.4, 1.5, n)  # % per trade, positive edge


def test_baseline_needs_enough_trades():
    with pytest.raises(ValueError):
        Baseline.from_returns([1, 2, 3])


def test_live_like_backtest_stays_quiet():
    base = backtest()
    d = DriftDetector(Baseline.from_returns(base))
    rng = np.random.default_rng(11)
    for r in rng.normal(0.4, 1.5, 30):
        rep = d.update(LiveTrade(float(r), 5.0))
    assert rep["score"] < 60 and rep["level"] == "ok"


def test_regime_shift_detected_within_few_trades_and_attributed_to_market():
    d = DriftDetector(Baseline.from_returns(backtest(), market_vol=2.0), DriftConfig(action="halve_size"))
    rng = np.random.default_rng(5)
    reps = [d.update(LiveTrade(float(r), 6.0, 4.0)) for r in rng.normal(-1.2, 2.5, 12)]
    first_act = next(i for i, r in enumerate(reps) if r["level"] == "act")
    assert first_act < 10
    assert reps[-1]["cause"] == "market_regime" and reps[-1]["action"] == "halve_size"


def test_worse_fills_attributed_to_execution():
    d = DriftDetector(Baseline.from_returns(backtest(), market_vol=2.0))
    for _ in range(10):
        rep = d.update(LiveTrade(0.3, 40.0, 2.1))
    assert rep["level"] in ("warn", "act") and rep["cause"] == "execution"


def test_bootstrap_percentile_bounds():
    base = backtest()
    assert bootstrap_percentile(base, -1e9, 10, 500, 5) == 0.0
    assert bootstrap_percentile(base, 1e9, 10, 500, 5) == 1.0


def test_parse_tradingview_csv():
    text = ("Trade #,Type,Signal,Date/Time,Price USD,Contracts,Profit USD,Profit %\n"
            "1,Entry long,L,2026-01-01 10:00,60000,1,,\n"
            "1,Exit long,X,2026-01-01 14:00,61200,1,1200,2.00%\n"
            "2,Entry long,L,2026-01-02 10:00,61000,1,,\n"
            "2,Exit long,X,2026-01-02 12:00,60390,1,-610,−1.00%\n")
    assert parse_tradingview_csv(text) == [2.0, -1.0]
    with pytest.raises(ValueError):
        parse_tradingview_csv("a,b\n1,2\n")
