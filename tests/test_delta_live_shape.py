"""Checks against real Delta Exchange India API responses.

The fixture is a trimmed copy of the public BTCUSD product and ticker responses captured on 27 Nov 2026,
so these tests prove the client parses Delta's actual field names, not just the ones in our mocks.
Set ALGOGUARD_LIVE=1 to also hit the public API itself.
"""
import json
import os
from pathlib import Path

import httpx
import pytest

from algoguard.delta import DeltaClient
from algoguard.models import Account, Position
from algoguard.rules import Limits, check
from tests.helpers import sig

FIX = json.loads((Path(__file__).parent / "fixtures" / "delta_india_btcusd.json").read_text())


def recorded_client() -> DeltaClient:
    def handler(req: httpx.Request):
        if req.url.path == "/v2/products/BTCUSD":
            return httpx.Response(200, json=FIX["product"])
        if req.url.path == "/v2/tickers/BTCUSD":
            return httpx.Response(200, json=FIX["ticker"])
        return httpx.Response(404, json={"success": False})
    return DeltaClient("https://api.india.delta.exchange", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_parses_real_product_and_ticker():
    dc = recorded_client()
    p = dc.product("BTCUSD")
    assert p["id"] == 27 and float(p["contract_value"]) == 0.001
    assert dc.mark_price("BTCUSD") == pytest.approx(float(FIX["ticker"]["result"]["mark_price"]))


def test_default_200x_leverage_is_blocked_by_default_limits():
    """Delta India's BTCUSD defaults to 200x (initial margin 0.5%, maintenance 0.25%). A bot opening at that
    leverage is within about 0.25% of liquidation, so AlgoGuard's default limits block it."""
    p = FIX["product"]["result"]
    assert float(p["default_leverage"]) == 200
    mark = float(FIX["ticker"]["result"]["mark_price"])
    acct = Account(10_000, 8_000, mark, float(p["contract_value"]), Position("BTCUSD", 0, None, None, 200.0), 0.0,
                   float(p["maintenance_margin"]))
    codes = {f.code for f in check(sig(size=10, price=mark, leverage=200), acct, Limits()) if f.severity == "block"}
    assert {"leverage_setting", "liquidation_distance"} <= codes


@pytest.mark.skipif(os.environ.get("ALGOGUARD_LIVE") != "1", reason="set ALGOGUARD_LIVE=1 to call Delta's public API")
def test_live_public_api():
    dc = DeltaClient("https://api.india.delta.exchange")
    assert dc.product("BTCUSD")["symbol"] == "BTCUSD"
    assert dc.mark_price("BTCUSD") > 0
