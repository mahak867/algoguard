"""HTTP API.

  POST /webhook/{bot_id}         TradingView alert (JSON body with a `secret` field; TradingView cannot set headers)
  POST /orders                   an API trader's order (header X-AlgoGuard-Key: the bot's secret)
  POST /holds/{id}/confirm       {"token": ..., "approve": true|false}
  GET  /holds                    admin: pending holds with their confirmation tokens
  POST /bots                     admin: register a bot with its limits
  POST /bots/{id}/backtest       admin: baseline from returns or a TradingView "List of trades" CSV
  POST /bots/{id}/trades         admin: a closed live trade (feeds drift and today's loss)
  GET  /bots/{id}/drift          admin
  POST /kill-switch              admin: {"on": true|false}
  GET  /decisions                admin
  GET  /bots                     admin: each bot's limits, today's P&L, drift and what the anomaly model has learned
  POST /telegram/webhook         Telegram's Confirm / Reject taps (checked by secret header and chat id)
  GET  /dashboard                the trader's dashboard (asks for the admin token in the browser)
Admin endpoints need `Authorization: Bearer <ALGOGUARD_ADMIN_TOKEN>`.
"""
from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import asdict

from pathlib import Path

from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from algoguard.config import Settings, settings as default_settings
from algoguard.confirm import ConfirmError
from algoguard.delta import DeltaClient, DeltaError
from algoguard.drift import DriftConfig, LiveTrade, parse_tradingview_csv
from algoguard.explain import Explainer
from algoguard.models import Account, Decision, Outcome, Signal
from algoguard.notify import NullSender, TelegramSender, hold_buttons
from algoguard.pipeline import Bot, Guard
from algoguard.rules import Limits


class AlertIn(BaseModel):
    """The TradingView alert message.

    Accepts Delta's own Trading Bot JSON unchanged ({"symbol": "{{ticker}}", "side": ..., "qty": ...,
    "strategy_id": ...}, where {{ticker}} is e.g. BTCUSD.P on a DELTAIN: chart), so a trader keeps the message
    Delta generated and only swaps the webhook URL for AlgoGuard's (the bot key goes in the URL as ?key=).
    Also accepts AlgoGuard's own fields: size, price, time, reduce_only, leverage, secret.
    """
    model_config = {"extra": "allow"}
    secret: str | None = None
    symbol: str
    side: str
    size: int | None = Field(default=None, gt=0)
    qty: int | str | None = None         # Delta's field; a string such as "20" in Delta's template
    order_type: str = "market"
    price: float | None = None
    time: float | str | None = None      # {{timenow}} (ISO) or epoch seconds
    reduce_only: bool = False
    leverage: float | None = None
    strategy_id: str | None = None

    def contracts(self) -> int:
        n = self.size if self.size is not None else self.qty
        try:
            v = float(n)
        except (TypeError, ValueError):
            raise ValueError("the alert needs a whole number of contracts in qty (or size)") from None
        if v <= 0 or v != int(v):
            raise ValueError("qty must be a whole number of lots greater than 0 (Delta rejects decimals)")
        return int(v)

    def delta_symbol(self) -> str:
        """{{ticker}} on a DELTAIN: chart is e.g. BTCUSD.P; Delta's API product symbol is BTCUSD."""
        sym = self.symbol.strip().upper()
        if ":" in sym:
            sym = sym.split(":", 1)[1]
        return sym[:-2] if sym.endswith(".P") else sym

    def kind(self) -> str:
        return {"market_order": "market", "limit_order": "limit"}.get(self.order_type, self.order_type)


class BotIn(BaseModel):
    bot_id: str
    limits: dict = {}
    drift: dict = {}


class BacktestIn(BaseModel):
    returns: list[float] | None = None
    csv: str | None = None
    market_vol: float | None = None


class TradeIn(BaseModel):
    return_pct: float
    pnl: float = 0.0
    slippage_bps: float | None = None
    market_vol: float | None = None


class HoldAnswer(BaseModel):
    token: str
    approve: bool = True


def _epoch(t) -> float | None:
    if t is None or t == "":
        return None
    if isinstance(t, (int, float)):
        return float(t) / 1000 if t > 1e12 else float(t)
    from datetime import datetime
    return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()


def _view(d: Decision) -> dict:
    return {"outcome": d.outcome.value, "explanation": d.explanation, "hold_id": d.hold_id,
            "anomaly_score": d.anomaly_score, "latency_ms": d.latency_ms, "time": d.signal.received_at,
            "order": {"bot_id": d.signal.bot_id, "symbol": d.signal.symbol, "side": d.signal.side,
                      "size": d.signal.size, "order_type": d.signal.order_type, "price": d.signal.price},
            "findings": [asdict(f) for f in d.findings]}


def create_app(cfg: Settings | None = None, account_source=None, guard: Guard | None = None,
               delta: DeltaClient | None = None, sender=None) -> FastAPI:
    """`account_source(symbol, bot) -> Account` defaults to reading Delta; tests and the demo pass their own."""
    cfg = cfg or default_settings
    app = FastAPI(title="AlgoGuard", version="0.1.0")
    g = guard or Guard(cfg.SECRET_KEY, cfg.HOLD_SECONDS,
                       explainer=Explainer(cfg.ANTHROPIC_API_KEY, cfg.EXPLAINER_MODEL))
    dx = delta or DeltaClient(cfg.DELTA_BASE_URL, cfg.DELTA_API_KEY, cfg.DELTA_API_SECRET)
    out = sender or (TelegramSender(cfg.TELEGRAM_BOT_TOKEN, cfg.TELEGRAM_CHAT_ID)
                     if cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID else NullSender())
    accounts = account_source or (lambda symbol, bot: dx.account(symbol, bot.realised_pnl_today))
    app.state.guard, app.state.sender = g, out

    def admin(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(authorization, f"Bearer {cfg.ADMIN_TOKEN}"):
            raise HTTPException(401, "admin token required")

    def bot_or_404(bot_id: str) -> Bot:
        if bot_id not in g.bots:
            raise HTTPException(404, f"no bot {bot_id}")
        return g.bots[bot_id]

    def handle(bot: Bot, a: AlertIn, source: str) -> dict:
        g.expire_holds()  # the API runs on real time; the pipeline itself takes the clock from its callers
        try:
            sig = Signal(bot.bot_id, a.delta_symbol(), a.side, a.contracts(), a.kind(), a.price, _epoch(a.time),
                         time.time(), a.reduce_only, a.leverage, source, a.model_dump(exclude={"secret"}))
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        try:
            acct: Account = accounts(sig.symbol, bot)
        except DeltaError as e:
            raise HTTPException(502, f"could not read the account from Delta, nothing was sent: {e}") from None
        d = g.process(sig, acct)
        view = _view(d)
        if d.outcome is Outcome.PASS:
            view["sent"] = _send(sig)
        elif d.outcome is Outcome.HOLD:
            if source == "api":  # the trader's own code may prompt them; TradingView only logs the reply
                view["confirm_token"] = g.held[d.hold_id].token
            out.send(f"⏸ HOLD on {bot.bot_id}: {d.signal.side} {d.signal.size} {d.signal.symbol}\n{d.explanation}\n"
                     f"Confirm or reject within {cfg.HOLD_SECONDS} s.", buttons=hold_buttons(d.hold_id))
        else:
            out.send(f"⛔ BLOCK on {bot.bot_id}: {d.explanation}")
        return view

    def _send(sig: Signal) -> dict:
        if cfg.DRY_RUN:
            return {"dry_run": True}
        try:
            return {"dry_run": False, "exchange": dx.place_order(sig)}
        except DeltaError as e:
            return {"dry_run": False, "error": str(e)}

    @app.get("/health")
    def health():
        return {"ok": True, "dry_run": cfg.DRY_RUN, "bots": len(g.bots), "kill_switch": g.kill_switch}

    @app.post("/webhook/{bot_id}")
    def webhook(bot_id: str, a: AlertIn, bg: BackgroundTasks, key: str | None = None):
        bot = bot_or_404(bot_id)
        bg.add_task(g.maintain, bot_id)
        given = a.secret or key or ""
        if not given or not hmac.compare_digest(given, bot.secret):
            raise HTTPException(401, "wrong or missing secret")
        return handle(bot, a, "tradingview")

    @app.post("/orders")
    def api_order(a: AlertIn, bot_id: str, bg: BackgroundTasks, x_algoguard_key: str = Header(default="")):
        bot = bot_or_404(bot_id)
        bg.add_task(g.maintain, bot_id)
        if not hmac.compare_digest(x_algoguard_key, bot.secret):
            raise HTTPException(401, "wrong or missing X-AlgoGuard-Key")
        return handle(bot, a, "api")

    @app.get("/holds", dependencies=[Depends(admin)])
    def holds():
        g.expire_holds()
        return [{**_view(h.decision), "confirm_token": h.token, "expires_at": h.created + cfg.HOLD_SECONDS}
                for h in g.held.values()]

    @app.post("/holds/{hold_id}/confirm")
    def confirm(hold_id: str, ans: HoldAnswer):
        try:
            d = g.resolve_hold(hold_id, ans.token, ans.approve)
        except KeyError as e:
            raise HTTPException(404, str(e)) from None
        except ConfirmError as e:
            raise HTTPException(400, str(e)) from None
        view = _view(d)
        if d.outcome is Outcome.PASS:
            view["sent"] = _send(d.signal)
        return view

    def answer_tap(update: dict) -> dict:
        """One Confirm / Reject tap from Telegram. Returns what happened, for logs and tests."""
        cq = update.get("callback_query") or {}
        data, msg = cq.get("data") or "", cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        if not cq or ":" not in data:
            return {"handled": False}
        if cfg.TELEGRAM_CHAT_ID is None or str(chat) != str(cfg.TELEGRAM_CHAT_ID):
            out.answer(cq.get("id", ""), "Not allowed from this chat.")
            return {"handled": False, "reason": "wrong chat"}
        kind, hold_id = data.split(":", 1)
        h = g.held.get(hold_id)
        if kind not in ("c", "r") or h is None:
            out.answer(cq.get("id", ""), "Already answered or expired.")
            return {"handled": False, "reason": "no such hold"}
        try:
            d = g.resolve_hold(hold_id, h.token, approve=(kind == "c"))
        except ConfirmError as e:
            out.answer(cq.get("id", ""), str(e))
            out.edit(chat, msg.get("message_id", 0), f"{msg.get('text', '')}\n\n⌛ {e}")
            return {"handled": True, "outcome": "expired", "reason": str(e)}
        sent = _send(d.signal) if d.outcome is Outcome.PASS else None
        verdict = ("✅ Confirmed and sent" if sent and not sent.get("error") else "✅ Confirmed")
        if d.outcome is not Outcome.PASS:
            verdict = "❌ Rejected, never sent" if kind == "r" else f"⛔ Blocked by your rules: {d.explanation}"
        if sent and sent.get("error"):
            verdict += f" (Delta error: {sent['error']})"
        out.answer(cq.get("id", ""), verdict[:200])
        out.edit(chat, msg.get("message_id", 0), f"{msg.get('text', '')}\n\n{verdict}")
        return {"handled": True, "outcome": d.outcome.value, "sent": sent}

    app.state.answer_tap = answer_tap

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request,
                               x_telegram_bot_api_secret_token: str = Header(default="")):
        if not cfg.TELEGRAM_WEBHOOK_SECRET or not hmac.compare_digest(x_telegram_bot_api_secret_token,
                                                                     cfg.TELEGRAM_WEBHOOK_SECRET):
            raise HTTPException(403, "bad or missing Telegram secret")
        return answer_tap(await request.json())

    @app.get("/bots", dependencies=[Depends(admin)])
    def bots():
        rows = []
        for b in g.bots.values():
            prof = g.anomaly.profile(b.bot_id)
            drift = b.drift.report() if b.drift else None
            rows.append({
                "bot_id": b.bot_id, "limits": asdict(b.limits), "pnl_today": round(b.realised_pnl_today, 2),
                "drift": drift, "drift_history": b.drift.state.history[-200:] if b.drift else [],
                "drift_action": b.drift_config.action,
                "orders_learned": len(prof.history), "model_ready": prof.model is not None,
                "hold_threshold": prof.hold_score, "confirmed": prof.confirmed, "rejected": prof.rejected,
            })
        return {"kill_switch": g.kill_switch, "dry_run": cfg.DRY_RUN, "bots": rows}

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return (Path(__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")

    @app.post("/bots", dependencies=[Depends(admin)])
    def add_bot(b: BotIn):
        try:
            bot = Bot(b.bot_id, secrets.token_urlsafe(18), Limits(**b.limits), DriftConfig(**b.drift))
        except TypeError as e:
            raise HTTPException(422, str(e)) from None
        g.add_bot(bot)
        return {"bot_id": bot.bot_id, "secret": bot.secret, "limits": asdict(bot.limits)}

    @app.post("/bots/{bot_id}/backtest", dependencies=[Depends(admin)])
    def backtest(bot_id: str, b: BacktestIn):
        bot = bot_or_404(bot_id)
        try:
            rets = b.returns if b.returns is not None else parse_tradingview_csv(b.csv or "")
            bot.set_baseline(rets, b.market_vol)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        base = bot.drift.base
        return {"trades": len(base.returns), "mean": round(base.mean, 4), "win_rate": round(base.win_rate, 3)}

    @app.post("/bots/{bot_id}/trades", dependencies=[Depends(admin)])
    def trade(bot_id: str, t: TradeIn):
        bot_or_404(bot_id)
        r = g.record_trade(bot_id, LiveTrade(t.return_pct, t.slippage_bps, t.market_vol), t.pnl)
        if r and r["level"] != "ok":
            out.send(f"Drift {r['score']}/100 on {bot_id}: {r['cause']}. Action: {r['action']}.")
        return r or {"note": "no backtest baseline yet; upload one to /bots/{id}/backtest"}

    @app.get("/bots/{bot_id}/drift", dependencies=[Depends(admin)])
    def drift(bot_id: str):
        bot = bot_or_404(bot_id)
        return bot.drift.report() if bot.drift else {"note": "no backtest baseline yet"}

    @app.post("/kill-switch", dependencies=[Depends(admin)])
    def kill(on: bool = Body(embed=True)):
        g.kill_switch = on
        return {"kill_switch": on}

    @app.get("/decisions", dependencies=[Depends(admin)])
    def decisions(bot_id: str | None = None, limit: int = 50):
        rows = [d for d in g.log if bot_id is None or d.signal.bot_id == bot_id]
        return [_view(d) for d in rows[-limit:]]

    return app


app = create_app()
