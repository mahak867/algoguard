import pytest

from algoguard.liquidation import liquidation_price
from algoguard.rules import Limits, check
from tests.helpers import acct, sig


def codes(fs, sev="block"):
    return {f.code for f in fs if f.severity == sev}


def test_normal_order_passes_and_names_binding_limit():
    fs = check(sig(size=10), acct(), Limits())
    assert codes(fs) == set()
    b = next(f for f in fs if f.code == "binding_limit")
    assert b.facts["limit"] in ("leverage", "position cap")


def test_kill_switch_blocks_everything():
    assert "kill_switch" in codes(check(sig(), acct(), Limits(), kill_switch=True))
    assert "kill_switch" in codes(check(sig(), acct(), Limits(paused=True)))


def test_daily_loss_blocks_new_risk_but_allows_closing():
    a = acct(size=10, entry=60_000.0, pnl=-150.0)
    assert "daily_loss_limit" in codes(check(sig("buy", 5), a, Limits(max_daily_loss=100)))
    assert "daily_loss_limit" not in codes(check(sig("sell", 10), a, Limits(max_daily_loss=100)))


def test_leverage_limit_on_position_after_fill():
    # 1000 contracts x 0.001 BTC x 60,000 = 60,000 notional on 10,000 equity = 6x
    fs = check(sig(size=1000), acct(avail=1e9), Limits(max_leverage=5, max_order_notional=1e9))
    assert "leverage" in codes(fs)


def test_requested_leverage_over_limit():
    assert "leverage_setting" in codes(check(sig(size=1, leverage=25), acct(), Limits(max_leverage=5)))


def test_liquidation_distance():
    # 20x leverage: liquidation about 4.5% away, under a 15% minimum
    fs = check(sig(size=1), acct(lev=20), Limits(max_leverage=50, min_liq_distance_pct=15))
    assert "liquidation_distance" in codes(fs)
    assert "liquidation_distance" not in codes(check(sig(size=1), acct(lev=3), Limits()))


def test_price_typo_guard_applies_to_closing_orders_too():
    a = acct(size=10, entry=60_000.0)
    fs = check(sig("sell", 10, price=6_000.0, order_type="limit"), a, Limits())
    assert "price_gap" in codes(fs)


def test_order_value_cap():
    assert "order_value" in codes(check(sig(size=200), acct(), Limits(max_order_notional=5_000, max_leverage=50)))


def test_reduce_only_that_opens_is_blocked():
    assert "reduce_only_increases" in codes(check(sig("buy", 5, reduce_only=True), acct(size=0), Limits()))


def test_position_cap():
    assert "position_cap" in codes(check(sig(size=60), acct(), Limits(max_position_contracts=50)))


def test_blocked_symbol():
    assert "blocked_symbol" in codes(check(sig(), acct(), Limits(blocked_symbols=["btcusd"])))


def test_liquidation_formula():
    assert liquidation_price("long", 100, 10, 0.5) == pytest.approx(90.5)
    assert liquidation_price("short", 100, 10, 0.5) == pytest.approx(109.5)
    with pytest.raises(ValueError):
        liquidation_price("up", 100, 10)
