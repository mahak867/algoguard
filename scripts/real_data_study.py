"""Does the drift detector help on real prices? A walk-forward study on Delta Exchange India's hourly candles.

    python scripts/real_data_study.py          # fetches BTCUSD, ETHUSD, SOLUSD candles into data/ on first run

Every choice below was fixed before the held-out results were looked at:
  * The bot: an EMA crossover trend bot on 1-hour candles, always long or short, 3x leverage, 0.05% fee per side on
    notional (an assumption). Fills at the hourly close, no slippage model. Prices are real; the bot is simulated.
  * Every quarter the trader backtests a fixed grid of EMA settings on the previous 6 months and runs the best one
    live for the next 3 months. Picking the best backtest is how an over-fitted baseline happens in real life.
  * AlgoGuard gets that backtest as the baseline and sees every live trade through the real DriftDetector. Once
    drift reaches "act", the trader's chosen action (halve_size) applies to the following trades.
  * Drift settings are tuned on the development period only (live quarters starting before 2025-07-01), choosing
    the setting that saves the most money there while raising a false "act" in at most 10% of no-change runs
    (30 trades resampled from each real backtest). The held-out period (from 2025-07-01, including the
    10 October 2025 crash) is then reported for both the tuned and the default settings.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # run from anywhere without PYTHONPATH

import csv
import gzip
import itertools
import json
import time
import urllib.request
from pathlib import Path

import numpy as np

from algoguard.drift import Baseline, DriftConfig, DriftDetector, LiveTrade

ROOT = Path(__file__).resolve().parent.parent / "data"
API = "https://api.india.delta.exchange/v2/history/candles?resolution=1h&symbol={}&start={}&end={}"
SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD"]
GRID = [(8, 21), (10, 30), (12, 26), (20, 50), (24, 72), (30, 90), (50, 100), (50, 200)]
LEV, FEE = 3.0, 0.0005
QUARTER, LOOKBACK = 91 * 24, 182 * 24  # hours
SPLIT = 1751328000                       # 2025-07-01 00:00 UTC
CANDIDATES = [dict(k=k, h=h, range_act_pct=ra, range_warn_pct=max(ra * 3, 0.005))
              for k, h, ra in itertools.product((0.25, 0.5, 0.75), (3.0, 4.0, 5.0), (0.001, 0.01, 0.03))]


def path(sym: str) -> Path:
    return ROOT / f"delta_{sym.lower()}_1h.csv.gz"


def fetch(sym: str) -> None:
    rows, t, end = {}, 1704067200, int(time.time())
    while t < end:
        for c in json.load(urllib.request.urlopen(API.format(sym, t, min(t + 2000 * 3600, end)), timeout=30))["result"]:
            rows[c["time"]] = c
        t += 2000 * 3600
    ROOT.mkdir(exist_ok=True)
    with gzip.open(path(sym), "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for c in sorted(rows.values(), key=lambda c: c["time"]):
            w.writerow([c["time"], c["open"], c["high"], c["low"], c["close"], c["volume"]])


def load(sym: str) -> tuple[np.ndarray, np.ndarray]:
    if not path(sym).exists():
        fetch(sym)
    with gzip.open(path(sym), "rt") as f:
        r = list(csv.DictReader(f))
    return np.array([int(x["time"]) for x in r]), np.array([float(x["close"]) for x in r])


def ema(x: np.ndarray, n: int) -> np.ndarray:
    a, out = 2 / (n + 1), np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def trades(close, emas, fast, slow, lo, hi):
    """(exit index, return % of equity) for each trade that closes in [lo, hi)."""
    side = np.sign(emas[fast] - emas[slow])
    out, entry, pos = [], None, 0
    for i in range(max(lo - 1, slow), hi):
        if side[i] != pos and side[i] != 0:
            if pos and entry is not None and i >= lo:
                out.append((i, 100 * (LEV * pos * (close[i] / close[entry] - 1) - 2 * FEE * LEV)))
            pos, entry = int(side[i]), i
    return out


def vol(close, i):
    r = np.diff(np.log(close[max(0, i - 24):i + 1]))
    return float(r.std() * 100) if len(r) > 2 else 0.0


def quarters() -> list[dict]:
    """Each live quarter on each symbol: the trader's chosen backtest and the live trades that followed."""
    out = []
    for sym in SYMBOLS:
        t, close = load(sym)
        emas = {n: ema(close, n) for n in {x for g in GRID for x in g}}
        start = LOOKBACK
        while start + QUARTER <= len(close):
            scored = []
            for fast, slow in GRID:
                bt = trades(close, emas, fast, slow, start - LOOKBACK, start)
                if len(bt) >= 10:
                    scored.append((sum(r for _, r in bt), fast, slow, bt))
            if scored:
                _, fast, slow, bt = max(scored)
                live = trades(close, emas, fast, slow, start, start + QUARTER)
                out.append({"symbol": sym, "start": int(t[start]), "ema": f"{fast}/{slow}",
                            "backtest": [r for _, r in bt], "base_vol": float(np.mean([vol(close, i) for i, _ in bt])),
                            "live": [(r, vol(close, i)) for i, r in live]})
            start += QUARTER
    return out


def guard(q: dict, cfg: DriftConfig) -> tuple[list[float], int | None]:
    det = DriftDetector(Baseline.from_returns(q["backtest"], q["base_vol"]), cfg)
    out, level, acted = [], "ok", None
    for k, (r, v) in enumerate(q["live"]):
        out.append(r * (0.5 if level == "act" else 1.0))
        level = det.update(LiveTrade(r, market_vol=v))["level"]
        if level == "act" and acted is None:
            acted = k + 1
    return out, acted


def null_act_rate(qs: list[dict], cfg: DriftConfig, runs: int = 20) -> float:
    """No change at all: live trades resampled from the backtest itself. How often does drift wrongly act?"""
    hits = n = 0
    for q in qs:
        base = Baseline.from_returns(q["backtest"], q["base_vol"])
        for s in range(runs):
            det = DriftDetector(base, cfg)
            sample = np.random.default_rng(s).choice(q["backtest"], 30)
            hits += any(det.update(LiveTrade(float(r)))["level"] == "act" for r in sample)
            n += 1
    return hits / n


def equity(rets):
    eq = np.cumprod([1.0] + [1 + r / 100 for r in rets])
    return float(eq[-1] - 1) * 100, float(np.max(1 - eq / np.maximum.accumulate(eq))) * 100


def evaluate(qs: list[dict], cfg: DriftConfig) -> dict:
    rows = []
    for q in qs:
        g, acted = guard(q, cfg)
        p = [r for r, _ in q["live"]]
        rows.append({"symbol": q["symbol"], "from": time.strftime("%Y-%m-%d", time.gmtime(q["start"])), "ema": q["ema"],
                     "trades": len(p), "acted_after": acted, "plain": equity(p), "guarded": equity(g),
                     "losing": bool(p) and equity(p)[0] < 0})
    acted = [r for r in rows if r["acted_after"]]
    losing = [r for r in rows if r["losing"]]
    return {
        "symbol_quarters": len(rows), "losing_quarters": len(losing),
        "acted_in": len(acted), "acted_in_losing": sum(r["losing"] for r in acted),
        "losing_caught": f"{sum(1 for r in losing if r['acted_after'])}/{len(losing)}",
        "median_trades_to_act": float(np.median([r["acted_after"] for r in acted])) if acted else None,
        "avg_quarter_return_without": round(float(np.mean([r["plain"][0] for r in rows])), 1),
        "avg_quarter_return_with": round(float(np.mean([r["guarded"][0] for r in rows])), 1),
        "avg_quarter_max_drawdown_without": round(float(np.mean([r["plain"][1] for r in rows])), 1),
        "avg_quarter_max_drawdown_with": round(float(np.mean([r["guarded"][1] for r in rows])), 1),
        "worst_quarter_without": round(min(r["plain"][0] for r in rows), 1),
        "worst_quarter_with": round(min(r["guarded"][0] for r in rows), 1),
        "rows": rows,
    }


def main() -> dict:
    qs = quarters()
    dev = [q for q in qs if q["start"] < SPLIT]
    test = [q for q in qs if q["start"] >= SPLIT]
    best = None
    for c in CANDIDATES:
        cfg = DriftConfig(action="halve_size", bootstrap_sims=500, **c)
        far = null_act_rate(dev, cfg)
        if far > 0.10:
            continue
        saved = sum(np.log1p(equity(guard(q, cfg)[0])[0] / 100) - np.log1p(equity([r for r, _ in q["live"]])[0] / 100)
                    for q in dev)
        if best is None or saved > best[0]:
            best = (saved, c, far)
    tuned = DriftConfig(action="halve_size", **best[1])
    default = DriftConfig(action="halve_size")
    out = {
        "data": {s: time.strftime("%Y-%m-%d", time.gmtime(int(load(s)[0][0]))) for s in SYMBOLS},
        "tuned_on_development_period": {**best[1], "false_act_rate_no_change": round(best[2], 3),
                                        "false_act_rate_held_out_no_change": round(null_act_rate(test, tuned), 3)},
        "held_out_default": evaluate(test, default),
        "held_out_tuned": evaluate(test, tuned),
        "development_tuned": evaluate(dev, tuned),
    }
    return out


if __name__ == "__main__":
    res = main()
    print(json.dumps({k: v for k, v in res.items() if not isinstance(v, dict) or "rows" not in v}, indent=1))
    for name in ("held_out_default", "held_out_tuned", "development_tuned"):
        print(name, json.dumps({k: v for k, v in res[name].items() if k != "rows"}))
    print("\nheld out, tuned settings:")
    for r in res["held_out_tuned"]["rows"]:
        print(f"  {r['symbol']} {r['from']} ema {r['ema']:>6} trades {r['trades']:>3} acted {r['acted_after']}"
              f"  quarter {r['plain'][0]:+6.1f}% -> {r['guarded'][0]:+6.1f}%   maxDD {r['plain'][1]:5.1f}% -> {r['guarded'][1]:5.1f}%")
    Path(ROOT / "real_data_study.json").write_text(json.dumps(res, indent=1, default=str))
