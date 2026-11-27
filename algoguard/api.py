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
Admin endpoints need `Authorization: Bearer <ALGOGUARD_ADMIN_TOKEN>`.
"""
from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import asdict

from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from algoguard.config import Settings, settings as default_settings
from algoguard.confirm import ConfirmError
from algoguard.delta import DeltaClient, DeltaError
from algoguard.drift import DriftConfig, LiveTrade, parse_tradingview_csv
from algoguard.explain import Explainer
from algoguard.models import Account, Decision, Outcome, Signal
from algoguard.notify import NullSender, TelegramSender
from algoguard.pipeline import Bot, Guard
from algoguard.rules import Limits


class AlertIn(BaseModel):
    """What to put in the TradingView alert message (see README)."""
    secret: str | None = None
    symbol: str
    side: str
    size: int = Field(gt=0)
    order_type: str = "market"
    price: float | None = None
    time: float | str | None = None      # {{timenow}} (ISO) or epoch seconds
    reduce_only: bool = False
    leverage: float | None = None


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
            "anomaly_score": d.anomaly_score, "latency_ms": d.latency_ms,
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
        try:
            sig = Signal(bot.bot_id, a.symbol, a.side, a.size, a.order_type, a.price, _epoch(a.time),
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
            out.send(f"{d.explanation} Confirm or reject hold {d.hold_id} within {cfg.HOLD_SECONDS} s.")
        else:
            out.send(d.explanation)
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
    def webhook(bot_id: str, a: AlertIn, bg: BackgroundTasks):
        bot = bot_or_404(bot_id)
        bg.add_task(g.maintain, bot_id)
        if not a.secret or not hmac.compare_digest(a.secret, bot.secret):
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
