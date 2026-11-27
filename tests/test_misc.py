import json

import httpx
import pytest

from algoguard.confirm import ConfirmError, Confirmations
from algoguard.delta import DeltaClient, DeltaError
from algoguard.explain import Explainer, grounded
from algoguard.models import Decision, Finding, Outcome
from algoguard.vault import Vault, VaultError
from tests.helpers import T0, sig


def test_confirm_token_bound_to_order():
    c = Confirmations("k", 120)
    s = sig()
    tok = c.token("h1", s, now=T0)
    s.size = 99
    with pytest.raises(ConfirmError):
        c.check("h1", s, tok, now=T0 + 1)


def test_vault_roundtrip_and_wrong_key():
    v = Vault("a")
    t = v.seal("secret")
    assert v.unseal(t) == "secret"
    with pytest.raises(VaultError):
        Vault("b").unseal(t)


def test_grounded_rejects_invented_numbers():
    facts = json.dumps({"size": 10, "ratio": 8.0})
    assert grounded("Held: 10 contracts is 8x the usual.", facts)
    assert not grounded("Held: 10 contracts is 12x the usual.", facts)


def test_llm_explanation_falls_back_when_ungrounded():
    def handler(req):
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Blocked: you would lose 9999."}]})
    e = Explainer("key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    d = Decision(Outcome.BLOCK, sig(), [Finding("kill_switch", "rules", "block", "Trading is paused.", {})], None)
    assert e.explain(d, use_llm=True).startswith("Blocked: buy 10 BTCUSD")


def test_delta_signing_and_order_body():
    seen = {}

    def handler(req: httpx.Request):
        if req.url.path == "/v2/products/BTCUSD":
            return httpx.Response(200, json={"success": True, "result": {"id": 27, "contract_value": "0.001"}})
        seen["headers"], seen["body"] = req.headers, json.loads(req.content)
        return httpx.Response(200, json={"success": True, "result": {"id": 1}})

    dc = DeltaClient("https://x", "key", "sec", client=httpx.Client(transport=httpx.MockTransport(handler)))
    dc.place_order(sig(size=3))
    assert seen["body"] == {"product_id": 27, "size": 3, "side": "buy", "order_type": "market_order",
                            "reduce_only": False}
    h = seen["headers"]
    assert h["api-key"] == "key"
    assert h["signature"] == dc.sign("POST", "/v2/orders", "", json.dumps(seen["body"], separators=(",", ":")),
                                     h["timestamp"])


def test_delta_error_surfaces():
    dc = DeltaClient("https://x", client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"success": False, "error": {"code": "bad"}}))))
    with pytest.raises(DeltaError):
        dc.mark_price("BTCUSD")
