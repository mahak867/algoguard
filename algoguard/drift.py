"""Drift detector: is the bot still behaving like its backtest?

Baseline: the trader's own backtest trade list (for example TradingView's Strategy Tester export). AlgoGuard
does not backtest; it reads the result the trader already has.

On every closed live trade:
  * One-sided CUSUM tests on standardised trade return (falling), win/loss (falling) and slippage (rising).
    These react within a handful of trades instead of waiting for a month of losses.
  * A block bootstrap of the backtest trades (ported from IntelliStock's backtest.bootstrap_drawdown) gives
    the range of cumulative returns the backtest considers normal after the same number of trades; the live
    result's percentile in that range is the fourth signal.
  * A market check: realised volatility now against the backtest period's. It separates "the market changed"
    (regime shift) from "the bot's fills got worse" (execution) and "the edge faded" (strategy decay).

The drift score (0-100) is the strongest of the four signals. The action at each level is chosen by the
trader in advance; AlgoGuard never predicts the market or suggests trades.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


@dataclass
class DriftConfig:
    k: float = 0.75                 # CUSUM allowance, in standard deviations
    h: float = 5.0                  # CUSUM alarm level
    # Measured with scripts/tune_drift.py (synthetic, 400 runs): on 30 trades drawn from the backtest itself,
    # 8.5% of runs reach "warn" and 3% reach "act"; a shift to a losing regime (mean -1.2%, sd 2.5% against a
    # +0.4% / 1.5% backtest) reaches "act" after a median of 8 trades (90th percentile 14).
    warn_at: int = 60
    act_at: int = 80
    action: str = "halve_size"      # what to do at act_at: alert_only | halve_size | pause
    expected_slippage_bps: float = 5.0
    slippage_sd_bps: float = 5.0
    bootstrap_sims: int = 2000
    block: int = 5
    vol_ratio_regime: float = 1.5   # market vol this many times the baseline (or 1/x) counts as a regime shift
    range_warn_pct: float = 0.005   # live total in the backtest's bottom 0.5% over as many trades: warn
    range_act_pct: float = 0.001    # bottom 0.1%: act

    @classmethod
    def delta_calibrated(cls, **kw) -> "DriftConfig":
        """Settings tuned on real Delta India BTC/ETH/SOL hourly prices (trend bots, Jan 2024 - Jun 2025) and checked on
        held-out data from Jul 2025: see docs/real_data_study.md. More sensitive than the defaults (catches more
        decaying bots, sooner) at the cost of more false alarms when nothing changed (about 9-10% vs about 3%)."""
        return cls(**{"k": 0.5, "h": 3.0, "range_act_pct": 0.01, "range_warn_pct": 0.03, **kw})


@dataclass
class Baseline:
    returns: np.ndarray             # per-trade return, % of equity or R
    mean: float
    sd: float
    win_rate: float
    market_vol: float | None = None  # realised vol during the backtest, same units as LiveTrade.market_vol

    @classmethod
    def from_returns(cls, returns, market_vol: float | None = None) -> "Baseline":
        r = np.asarray(list(returns), dtype=float)
        if len(r) < 10:
            raise ValueError("need at least 10 backtest trades for a baseline")
        sd = float(r.std(ddof=1)) or 1e-9
        return cls(r, float(r.mean()), sd, float((r > 0).mean()), market_vol)


@dataclass
class LiveTrade:
    return_pct: float
    slippage_bps: float | None = None
    market_vol: float | None = None


@dataclass
class DriftState:
    trades: list[LiveTrade] = field(default_factory=list)
    s_return: float = 0.0
    s_win: float = 0.0
    s_slip: float = 0.0
    history: list[int] = field(default_factory=list)


def bootstrap_percentile(base: np.ndarray, live_cum: float, n: int, sims: int, block: int, seed: int = 7) -> float:
    """Share of block-bootstrapped n-trade paths from the backtest that ended at or below the live result."""
    m = len(base)
    block = max(1, min(block, m // 2))
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, m - block + 1, size=(sims, math.ceil(n / block)))
    idx = (starts[:, :, None] + np.arange(block)).reshape(sims, -1)[:, :n]
    cums = base[idx].sum(axis=1)
    return float(np.mean(cums <= live_cum))


class DriftDetector:
    def __init__(self, baseline: Baseline, config: DriftConfig | None = None):
        self.base, self.cfg, self.state = baseline, config or DriftConfig(), DriftState()

    def update(self, t: LiveTrade) -> dict:
        b, c, s = self.base, self.cfg, self.state
        s.trades.append(t)
        z = (t.return_pct - b.mean) / b.sd
        s.s_return = max(0.0, s.s_return - z - c.k)
        p = min(max(b.win_rate, 0.01), 0.99)
        zw = ((1.0 if t.return_pct > 0 else 0.0) - p) / math.sqrt(p * (1 - p))
        s.s_win = max(0.0, s.s_win - zw - c.k)
        if t.slippage_bps is not None:
            zs = (t.slippage_bps - c.expected_slippage_bps) / c.slippage_sd_bps
            s.s_slip = max(0.0, s.s_slip + zs - c.k)
        return self.report()

    def _cusum_score(self, stat: float) -> float:
        c = self.cfg
        x = stat / c.h
        if x < 0.75:
            return c.warn_at * x / 0.75
        return min(c.warn_at + (100 - c.warn_at) * (x - 0.75) / 0.25, 100.0)

    def report(self) -> dict:
        b, c, s = self.base, self.cfg, self.state
        n = len(s.trades)
        if n == 0:
            return {"trades": 0, "score": 0, "level": "ok", "cause": None, "action": None, "signals": {}}
        live = np.array([t.return_pct for t in s.trades])
        pct = bootstrap_percentile(b.returns, float(live.sum()), n, c.bootstrap_sims, c.block)
        signals = {  # each on the 0-100 scale: warn_at when a CUSUM reaches 75% of h, act_at at 87.5%
            "return": self._cusum_score(s.s_return),
            "win_rate": self._cusum_score(s.s_win),
            "slippage": self._cusum_score(s.s_slip),
            # Checked after every trade, so only extreme results count, and only from 10 trades on: the live total
            # in the bottom 0.5% of what the backtest produces over as many trades warns, bottom 0.1% acts.
            "vs_backtest_range": float(np.interp(pct, [0.0, c.range_act_pct, c.range_warn_pct, max(0.05, 2 * c.range_warn_pct)],
                                                 [100, c.act_at, c.warn_at, 0]))
            if n >= 10 else 0.0,
        }
        score = int(round(max(signals.values())))
        s.history.append(score)

        vols = [t.market_vol for t in s.trades[-10:] if t.market_vol is not None]
        vol_ratio = float(np.mean(vols) / b.market_vol) if vols and b.market_vol else None
        cause = None
        if score >= c.warn_at:
            if vol_ratio and (vol_ratio >= c.vol_ratio_regime or vol_ratio <= 1 / c.vol_ratio_regime):
                cause = "market_regime"
            elif signals["slippage"] >= max(signals["return"], signals["win_rate"]):
                cause = "execution"
            else:
                cause = "strategy_decay"
        level = "act" if score >= c.act_at else "warn" if score >= c.warn_at else "ok"
        return {
            "trades": n, "score": score, "level": level, "cause": cause,
            "action": c.action if level == "act" else ("alert_only" if level == "warn" else None),
            "signals": {k: round(v) for k, v in signals.items()},
            "live_mean": round(float(live.mean()), 3), "backtest_mean": round(b.mean, 3),
            "live_win_rate": round(float((live > 0).mean()), 2), "backtest_win_rate": round(b.win_rate, 2),
            "backtest_percentile": round(pct, 3), "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
        }


def parse_tradingview_csv(text: str) -> list[float]:
    """Per-trade returns (%) from a TradingView Strategy Tester 'List of trades' CSV export.

    Looks for an exit row per trade and a column whose name contains 'Profit %' or 'P&L %'. TradingView has
    changed these headers over time, so check this against a real export before the demo.
    """
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        raise ValueError("the CSV has no rows")
    cols = list(rows[0].keys())
    pcol = next((h for h in cols if h and ("profit %" in h.lower() or "p&l %" in h.lower())), None)
    tcol = next((h for h in cols if h and h.strip().lower() == "type"), None)
    if not pcol:
        raise ValueError("no 'Profit %' column found; is this TradingView's List of trades export?")
    out = []
    for r in rows:
        if tcol and not str(r.get(tcol, "")).lower().startswith("exit"):
            continue
        v = str(r.get(pcol, "")).replace("%", "").replace(",", "").replace("−", "-").strip()
        if v:
            out.append(float(v))
    return out
