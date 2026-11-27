"""Check algoguard.delta against Delta Exchange's testnet before the demo.

    ALGOGUARD_DELTA_API_KEY=... ALGOGUARD_DELTA_API_SECRET=... python scripts/check_delta.py [SYMBOL] [--order]

Reads the product, mark price, wallet and position, and prints what AlgoGuard would see. With --order it
also places and then cancels one tiny far-away limit order on the TESTNET only. The endpoint paths and field
names in algoguard/delta.py were written from Delta's docs and have not been run against the live API yet;
fix anything this script shows to be different.
"""
from __future__ import annotations

import sys

from algoguard.config import settings
from algoguard.delta import DeltaClient
from algoguard.models import Signal


def main() -> None:
    symbol = next((a for a in sys.argv[1:] if not a.startswith("--")), "BTCUSD")
    if "testnet" not in settings.DELTA_BASE_URL:
        sys.exit(f"Refusing to run against {settings.DELTA_BASE_URL}: this check is for the testnet only.")
    dc = DeltaClient(settings.DELTA_BASE_URL, settings.DELTA_API_KEY, settings.DELTA_API_SECRET)
    prod = dc.product(symbol)
    print("product:", {k: prod.get(k) for k in ("id", "symbol", "contract_value", "contract_type", "settling_asset")})
    print("mark price:", dc.mark_price(symbol))
    if not settings.DELTA_API_KEY:
        print("No API key set: skipped the account checks.")
        return
    print("account:", dc.account(symbol))
    if "--order" in sys.argv:
        far = round(dc.mark_price(symbol) * 0.5, 0)
        res = dc.place_order(Signal("check", symbol, "buy", 1, "limit", far))
        print("placed:", res)
        print("cancel all:", dc.cancel_all())


if __name__ == "__main__":
    main()
