"""Telegram notifications, with one-tap Confirm / Reject buttons on every HOLD.

Flow: AlgoGuard sends the HOLD with two inline buttons whose callback data is only "c:<hold_id>" or
"r:<hold_id>". Telegram posts the tap to /telegram/webhook (or scripts/telegram.py forwards it when polling).
The server checks Telegram's secret header and that the tap came from the trader's own chat, then resolves the
hold with the confirmation token it kept itself, so the token never leaves the server.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx


def hold_buttons(hold_id: str) -> list[dict]:
    return [{"text": "✅ Confirm", "callback_data": f"c:{hold_id}"},
            {"text": "❌ Reject", "callback_data": f"r:{hold_id}"}]


@dataclass
class TelegramSender:
    token: str
    chat_id: str
    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=10))

    def _call(self, method: str, body: dict) -> dict:
        r = self.client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=body)
        try:
            return r.json()
        except ValueError:
            return {"ok": False, "description": f"HTTP {r.status_code}"}

    def send(self, text: str, buttons: list[dict] | None = None) -> bool:
        """`buttons`: inline keyboard buttons, each {"text", "callback_data"} or {"text", "url"}."""
        body: dict = {"chat_id": self.chat_id, "text": text[:4000]}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [buttons]}
        return bool(self._call("sendMessage", body).get("ok"))

    def answer(self, callback_id: str, text: str) -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    def edit(self, chat_id, message_id: int, text: str) -> None:
        """Replace the HOLD message with its outcome; this also removes the buttons."""
        self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]})

    def set_webhook(self, url: str, secret: str) -> dict:
        return self._call("setWebhook", {"url": url, "secret_token": secret, "allowed_updates": ["callback_query"]})


class NullSender:
    """Used when Telegram is not configured: keeps messages so the dashboard and tests can show them."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.buttons: list[list[dict] | None] = []
        self.answers: list[str] = []
        self.edits: list[str] = []

    def send(self, text: str, buttons: list[dict] | None = None) -> bool:
        self.sent.append(text)
        self.buttons.append(buttons)
        return False

    def answer(self, callback_id: str, text: str) -> None:
        self.answers.append(text)

    def edit(self, chat_id, message_id: int, text: str) -> None:
        self.edits.append(text)
