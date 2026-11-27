from fastapi.testclient import TestClient

from algoguard.api import create_app
from algoguard.config import Settings
from algoguard.notify import NullSender
from tests.helpers import acct

ADMIN = {"Authorization": "Bearer adm"}


def client():
    cfg = Settings(SECRET_KEY="k", ADMIN_TOKEN="adm", DRY_RUN=True)
    sender = NullSender()
    app = create_app(cfg, account_source=lambda symbol, bot: acct(pnl=bot.realised_pnl_today), sender=sender)
    return TestClient(app), sender


def setup_bot(c):
    r = c.post("/bots", json={"bot_id": "trend", "limits": {"max_daily_loss": 50}}, headers=ADMIN)
    assert r.status_code == 200
    return r.json()["secret"]


def test_admin_required():
    c, _ = client()
    assert c.post("/bots", json={"bot_id": "x"}).status_code == 401
    assert c.get("/decisions").status_code == 401


def test_webhook_pass_hold_block_flow():
    c, sender = client()
    secret = setup_bot(c)
    alert = {"secret": secret, "symbol": "BTCUSD", "side": "buy", "size": 10, "price": 60000}
    assert c.post("/webhook/trend", json={**alert, "secret": "wrong"}).status_code == 401

    r = c.post("/webhook/trend", json=alert).json()
    assert r["outcome"] == "PASS" and r["sent"] == {"dry_run": True}

    assert c.post("/webhook/trend", json=alert).json()["outcome"] == "BLOCK"  # duplicate

    r = c.post("/webhook/trend", json={**alert, "size": 11, "time": "2020-01-01T00:00:00Z"}).json()
    assert r["outcome"] == "HOLD" and "confirm_token" not in r
    holds = c.get("/holds", headers=ADMIN).json()
    tok = holds[0]["confirm_token"]
    r = c.post(f"/holds/{r['hold_id']}/confirm", json={"token": tok, "approve": True}).json()
    assert r["outcome"] == "PASS"
    assert sender.sent  # the HOLD and the BLOCK were notified


def test_api_order_and_kill_switch():
    c, _ = client()
    secret = setup_bot(c)
    c.post("/kill-switch", json={"on": True}, headers=ADMIN)
    r = c.post("/orders?bot_id=trend", json={"symbol": "BTCUSD", "side": "buy", "size": 1},
               headers={"X-AlgoGuard-Key": secret}).json()
    assert r["outcome"] == "BLOCK" and any(f["code"] == "kill_switch" for f in r["findings"])


def test_backtest_and_drift_endpoints():
    c, _ = client()
    setup_bot(c)
    r = c.post("/bots/trend/backtest", json={"returns": [0.5, -0.3, 0.8, 0.2, -0.6, 1.1, 0.4, -0.2, 0.3, 0.9]},
               headers=ADMIN)
    assert r.json()["trades"] == 10
    r = c.post("/bots/trend/trades", json={"return_pct": -3.0, "pnl": -60}, headers=ADMIN).json()
    assert "score" in r
    assert c.get("/bots/trend/drift", headers=ADMIN).json()["trades"] == 1
