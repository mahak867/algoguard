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


def test_accepts_deltas_own_webhook_json_unchanged():
    """Delta's Trading Bot generates this message; AlgoGuard takes it as is, keyed by the URL."""
    c, _ = client()
    secret = setup_bot(c)
    delta_msg = {"symbol": "BTCUSD.P", "side": "buy", "qty": "20", "strategy_id": "abc123"}
    r = c.post(f"/webhook/trend?key={secret}", json=delta_msg).json()
    assert r["outcome"] == "PASS" and r["order"]["symbol"] == "BTCUSD" and r["order"]["size"] == 20
    assert c.post(f"/webhook/trend?key={secret}", json={**delta_msg, "qty": "0.5"}).status_code == 422
    assert c.post("/webhook/trend?key=wrong", json=delta_msg).status_code == 401


def tg_client():
    cfg = Settings(SECRET_KEY="k", ADMIN_TOKEN="adm", DRY_RUN=True, TELEGRAM_CHAT_ID="42", TELEGRAM_WEBHOOK_SECRET="s3")
    sender = NullSender()
    app = create_app(cfg, account_source=lambda symbol, bot: acct(pnl=bot.realised_pnl_today), sender=sender)
    return TestClient(app), sender


def held_alert(c, secret, size=11):
    alert = {"secret": secret, "symbol": "BTCUSD", "side": "buy", "size": size, "price": 60000, "time": "2020-01-01T00:00:00Z"}
    r = c.post("/webhook/trend", json=alert).json()
    assert r["outcome"] == "HOLD"
    return r["hold_id"]


def tap(c, data, chat=42, secret="s3"):
    upd = {"update_id": 1, "callback_query": {"id": "cb1", "data": data,
                                              "message": {"message_id": 7, "chat": {"id": chat}, "text": "HOLD"}}}
    return c.post("/telegram/webhook", json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def test_hold_message_has_confirm_and_reject_buttons():
    c, sender = tg_client()
    hid = held_alert(c, setup_bot(c))
    assert [b["callback_data"] for b in sender.buttons[-1]] == [f"c:{hid}", f"r:{hid}"]


def test_telegram_confirm_sends_and_reject_blocks():
    c, sender = tg_client()
    secret = setup_bot(c)
    hid = held_alert(c, secret)
    r = tap(c, f"c:{hid}").json()
    assert r["handled"] and r["outcome"] == "PASS" and r["sent"] == {"dry_run": True}
    assert "Confirmed" in sender.edits[-1]
    assert tap(c, f"c:{hid}").json()["handled"] is False  # a second tap cannot send it again
    hid2 = held_alert(c, secret, size=12)  # a different order; the same one again would be a duplicate BLOCK
    assert tap(c, f"r:{hid2}").json()["outcome"] == "BLOCK" and "Rejected" in sender.edits[-1]


def test_telegram_taps_need_the_secret_and_the_traders_chat():
    c, _ = tg_client()
    hid = held_alert(c, setup_bot(c))
    assert tap(c, f"c:{hid}", secret="wrong").status_code == 403
    assert tap(c, f"c:{hid}", chat=999).json()["handled"] is False
    assert hid in [h["hold_id"] for h in c.get("/holds", headers=ADMIN).json()]  # still waiting


def test_telegram_disabled_without_secret():
    c, _ = client()
    assert c.post("/telegram/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": ""}).status_code == 403


def test_bots_summary_and_dashboard_page():
    c, _ = client()
    setup_bot(c)
    assert c.get("/bots").status_code == 401
    b = c.get("/bots", headers=ADMIN).json()
    assert b["bots"][0]["bot_id"] == "trend" and b["dry_run"] is True
    page = c.get("/dashboard")
    assert page.status_code == 200 and "AlgoGuard" in page.text
