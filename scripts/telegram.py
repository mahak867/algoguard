"""Connect Telegram's Confirm / Reject buttons to AlgoGuard.

On a server with a public HTTPS address (what Telegram needs for webhooks):
    python scripts/telegram.py set-webhook https://your-server.example
Locally, with no public address: poll Telegram and forward each tap to the local server.
    python scripts/telegram.py poll http://localhost:8000

Both read ALGOGUARD_TELEGRAM_BOT_TOKEN and ALGOGUARD_TELEGRAM_WEBHOOK_SECRET from the environment or .env.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from algoguard.config import settings  # noqa: E402
from algoguard.notify import TelegramSender  # noqa: E402


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in ("set-webhook", "poll"):
        sys.exit(__doc__)
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_WEBHOOK_SECRET:
        sys.exit("Set ALGOGUARD_TELEGRAM_BOT_TOKEN and ALGOGUARD_TELEGRAM_WEBHOOK_SECRET first.")
    tg = TelegramSender(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID or "")
    base = sys.argv[2].rstrip("/")
    if sys.argv[1] == "set-webhook":
        print(tg.set_webhook(f"{base}/telegram/webhook", settings.TELEGRAM_WEBHOOK_SECRET))
        return
    tg._call("deleteWebhook", {})  # getUpdates only works with no webhook set
    offset, client = 0, httpx.Client(timeout=40)
    print(f"Polling Telegram; forwarding taps to {base}/telegram/webhook. Ctrl+C to stop.")
    while True:
        try:
            r = tg._call("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": ["callback_query"]})
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                res = client.post(f"{base}/telegram/webhook", json=u,
                                  headers={"X-Telegram-Bot-Api-Secret-Token": settings.TELEGRAM_WEBHOOK_SECRET})
                print(res.status_code, res.text[:200])
        except httpx.HTTPError as e:
            print("network error, retrying:", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
