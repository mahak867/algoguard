"""Estimated liquidation price for an isolated-margin linear perpetual.

This is an estimate for a position that does not exist yet (the order AlgoGuard is checking). For an open
position, use the liquidation price Delta's API reports, which accounts for fees, funding and the exact
maintenance-margin schedule. Check the formula against Delta's documentation before relying on it.

Long:  entry x (1 - 1/leverage + mm)
Short: entry x (1 + 1/leverage - mm)
where mm is the maintenance margin as a fraction.
"""
from __future__ import annotations


def liquidation_price(side: str, entry: float, leverage: float, maintenance_margin_pct: float = 0.5) -> float:
    if entry <= 0 or leverage <= 0:
        raise ValueError("entry and leverage must be positive")
    mm = maintenance_margin_pct / 100
    if side == "long":
        return max(entry * (1 - 1 / leverage + mm), 0.0)
    if side == "short":
        return entry * (1 + 1 / leverage - mm)
    raise ValueError("side must be long or short")
