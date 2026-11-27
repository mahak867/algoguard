"""Telegram notifications for HOLDs, BLOCKs and drift warnings. Ported from IntelliStock's alert sender.

Uses the trader's own bot token. IntelliStock tested this against a mock only; test it with a real bot
before the demo.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx


@dataclass
class TelegramSender:
    token: str
    chat_id: str
    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=10))

    def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> bool:
        """`buttons` is a list of (label, url), e.g. Confirm / Reject links for a HOLD."""
        body: dict = {"chat_id": self.chat_id, "text": text[:4000]}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [[{"text": t, "url": u} for t, u in buttons]]}
        r = self.client.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json=body)
        return r.status_code == 200 and bool(r.json().get("ok"))


class NullSender:
    """Used when Telegram is not configured: keeps messages so the dashboard and tests can show them."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str, buttons=None) -> bool:
        self.sent.append(text)
        return False
