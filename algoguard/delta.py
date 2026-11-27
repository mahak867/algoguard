"""Thin client for Delta Exchange India's REST API: read the account, place orders.

Signing follows Delta's documented scheme: HMAC-SHA256 of method + timestamp + path + query string + body,
sent with `api-key`, `timestamp` and `signature` headers. Endpoints and field names below were written from
Delta's public docs (docs.delta.exchange) and have NOT been checked against the live testnet yet. Run
`scripts/check_delta.py` against the testnet before the hackathon demo and fix anything that differs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx

from algoguard.models import Account, Position, Signal


class DeltaError(Exception):
    pass


class DeltaClient:
    def __init__(self, base_url: str, api_key: str | None = None, api_secret: str | None = None,
                 client: httpx.Client | None = None, timeout: float = 5.0):
        self.base = base_url.rstrip("/")
        self.key, self.secret = api_key, api_secret
        self.http = client or httpx.Client(timeout=timeout)
        self._products: dict[str, dict] = {}

    # ── signing ──
    def sign(self, method: str, path: str, query: str, body: str, ts: str) -> str:
        if not self.secret:
            raise DeltaError("no API secret configured")
        msg = method.upper() + ts + path + (("?" + query) if query else "") + body
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def _req(self, method: str, path: str, params: dict | None = None, body: dict | None = None, auth: bool = False):
        query = urlencode(params or {})
        data = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"Content-Type": "application/json", "User-Agent": "algoguard"}
        if auth:
            ts = str(int(time.time()))
            headers.update({"api-key": self.key or "", "timestamp": ts,
                            "signature": self.sign(method, path, query, data, ts)})
        url = self.base + path + (("?" + query) if query else "")
        r = self.http.request(method, url, headers=headers, content=data or None)
        try:
            payload = r.json()
        except ValueError:
            raise DeltaError(f"Delta answered {r.status_code} with no JSON") from None
        if r.status_code >= 400 or payload.get("success") is False:
            raise DeltaError(f"Delta answered {r.status_code}: {payload.get('error') or payload}")
        return payload.get("result", payload)

    # ── public data ──
    def product(self, symbol: str) -> dict:
        if symbol not in self._products:
            self._products[symbol] = self._req("GET", f"/v2/products/{symbol}")
        return self._products[symbol]

    def mark_price(self, symbol: str) -> float:
        t = self._req("GET", f"/v2/tickers/{symbol}")
        return float(t.get("mark_price") or t.get("close"))

    # ── account ──
    def position(self, symbol: str) -> Position:
        p = self._req("GET", "/v2/positions", {"product_id": self.product(symbol)["id"]}, auth=True) or {}
        size = int(p.get("size") or 0)
        return Position(symbol, size,
                        float(p["entry_price"]) if p.get("entry_price") else None,
                        float(p["liquidation_price"]) if p.get("liquidation_price") else None,
                        float(p.get("leverage") or 1.0))

    def balance(self, asset: str = "USD") -> tuple[float, float]:
        """(equity, available margin) for the settlement asset."""
        rows = self._req("GET", "/v2/wallet/balances", auth=True) or []
        row = next((b for b in rows if (b.get("asset_symbol") or "").upper() == asset), None)
        if row is None:
            raise DeltaError(f"no {asset} balance on this account")
        equity = float(row.get("balance") or 0) + float(row.get("unrealized_pnl") or 0)
        return equity, float(row.get("available_balance") or 0)

    def account(self, symbol: str, realised_pnl_today: float = 0.0) -> Account:
        prod = self.product(symbol)
        asset = (prod.get("settling_asset") or {}).get("symbol", "USD")
        equity, avail = self.balance(asset)
        mm = prod.get("maintenance_margin")
        return Account(equity, avail, self.mark_price(symbol), float(prod.get("contract_value") or 1.0),
                       self.position(symbol), realised_pnl_today, float(mm) if mm is not None else None)

    # ── orders ──
    def place_order(self, s: Signal) -> dict:
        body = {"product_id": self.product(s.symbol)["id"], "size": s.size, "side": s.side,
                "order_type": "limit_order" if s.order_type == "limit" else "market_order",
                "reduce_only": s.reduce_only}
        if s.order_type == "limit":
            body["limit_price"] = str(s.price)
        return self._req("POST", "/v2/orders", body=body, auth=True)

    def cancel_all(self) -> dict:
        return self._req("DELETE", "/v2/orders/all", body={}, auth=True)
