"""Open the dashboard on the deck's slide-4 week, so there is something to see without Delta or TradingView.

    python scripts/dashboard_demo.py        # then open http://localhost:8000/dashboard, admin token: demo

Runs the week through the real code (scripts/demo_week.py), then serves the API on that state in dry-run mode.
Send more alerts to see them appear live, for example a size typo that gets held:
    curl -X POST "localhost:8000/webhook/btc-trend?key=k" -H "Content-Type: application/json" \
         -d '{"symbol": "BTCUSD.P", "side": "buy", "qty": "600"}'
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algoguard.api import create_app  # noqa: E402
from algoguard.config import Settings  # noqa: E402
from scripts.demo_week import Week  # noqa: E402


def build():
    week = Week()
    for line, _ in week.run():
        print(line)
    cfg = Settings(SECRET_KEY="demo", ADMIN_TOKEN="demo", DRY_RUN=True)
    # The demo's account: flat, so a fresh alert is checked against the week's limits and history.
    return create_app(cfg, account_source=lambda symbol, bot: week.acct(), guard=week.g)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build(), host="127.0.0.1", port=8000)
