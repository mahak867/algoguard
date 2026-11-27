"""One-tap confirmation for HELD orders.

Ported from IntelliStock's broker order confirmation (backend/services/broker_service.py): the token is an
HMAC of the exact order plus an expiry, so a confirmation cannot be reused for a changed order, expires after
HOLD_SECONDS, and each token can send at most one order.
"""
from __future__ import annotations

import hashlib
import hmac
import time

from algoguard.models import Signal


class ConfirmError(Exception):
    pass


def _order_key(hold_id: str, s: Signal) -> str:
    return f"{hold_id}|{s.bot_id}|{s.symbol}|{s.side}|{s.size}|{s.order_type}|{(s.price or 0):.2f}|{s.reduce_only}"


class Confirmations:
    def __init__(self, secret: str, seconds: int = 120):
        self.secret, self.seconds = secret.encode(), seconds
        self.used: set[str] = set()

    def _sig(self, hold_id: str, s: Signal, exp: int) -> str:
        return hmac.new(self.secret, f"{_order_key(hold_id, s)}|{exp}".encode(), hashlib.sha256).hexdigest()

    def token(self, hold_id: str, s: Signal, now: float | None = None) -> str:
        exp = int((now or time.time()) + self.seconds)
        return f"{exp}.{self._sig(hold_id, s, exp)}"

    def check(self, hold_id: str, s: Signal, token: str, now: float | None = None) -> None:
        try:
            exp_s, sig = token.split(".", 1)
            exp = int(exp_s)
        except (ValueError, AttributeError):
            raise ConfirmError("That confirmation is not valid.") from None
        if not hmac.compare_digest(self._sig(hold_id, s, exp), sig):
            raise ConfirmError("The order changed after it was held. It was not sent.")
        if (now or time.time()) > exp:
            raise ConfirmError(f"The hold expired ({self.seconds} s). The order was not sent.")
        if sig in self.used:
            raise ConfirmError("This confirmation was already used; it can send only one order.")
        self.used.add(sig)
