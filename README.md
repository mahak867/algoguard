# AlgoGuard

**An AI safety layer between a trading signal and the order**, for Delta Exchange's AlgoHub Trading Bot
(TradingView webhooks) and APIs. Built for the Techfest IIT Bombay × Delta Exchange Algo Trading Innovation
Challenge.

AI models check every order and keep watching a bot after it goes live; the trader's own hard limits always
have the final say.

```
TradingView alert / API order
        │
        ▼
 Alert anomaly checker ──► Drift state ──► Hard-rule veto ──► Delta order API
 (exact + learned)         (live vs        (trader's limits,     (only on PASS)
                            backtest)       plain code)
        │                                        │
        └──────────── Explainer ◄────────────────┘   PASS · HOLD (one-tap confirm) · BLOCK
```

## What it does

| Part | File | How |
|---|---|---|
| Hard-rule veto | `algoguard/rules.py` | Daily loss, leverage, distance to liquidation, position cap, price and order-value typo guards, kill switch. Plain code; no model output feeds it. Names the limit that binds. |
| Alert anomaly checker | `algoguard/anomaly.py` | Exact checks (duplicate, stale alert, price far from mark, bursts), context checks against the real position (unexpected short or flip), and a per-bot Isolation Forest trained on the bot's own orders. Can only HOLD (duplicates BLOCK). |
| Drift detector | `algoguard/drift.py` | One-sided CUSUM tests on trade return, win rate and slippage against the trader's backtest, plus a block bootstrap of the backtest trades. Says whether the market changed, fills got worse or the edge faded, and applies the action the trader chose (alert, halve size, pause). |
| Explainer | `algoguard/explain.py` | A template reason for every decision; optionally an LLM rewrite that is discarded unless every number in it appears in the facts. |
| Guard pipeline | `algoguard/pipeline.py` | Runs the above in order of authority. If a model fails, decides on the rules alone. A confirmed HOLD is re-checked against the rules before it is sent. |
| API | `algoguard/api.py` | Webhook, API orders, holds, bots, backtest upload (including TradingView's "List of trades" CSV), live trades, drift, kill switch. |

The design rule: **the AI can make things stricter, never looser.** It can hold an order the rules allow; it
can never send an order the rules block.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
PYTHONPATH=. python scripts/replay_demo.py      # simulated bot with injected faults
cp .env.example .env                            # then fill it in
uvicorn algoguard.api:app --reload
```

`ALGOGUARD_DRY_RUN=true` (the default) decides and logs but never sends an order.

### Register a bot and point TradingView at it

```bash
curl -X POST localhost:8000/bots -H "Authorization: Bearer $ALGOGUARD_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"bot_id": "btc-trend", "limits": {"max_daily_loss": 100, "max_leverage": 5, "min_liq_distance_pct": 15}}'
```

The answer includes the bot's `secret`. In TradingView, set the alert's webhook URL to
`https://<your-server>/webhook/btc-trend` and the message to:

```json
{"secret": "<secret>", "symbol": "BTCUSD", "side": "{{strategy.order.action}}",
 "size": {{strategy.order.contracts}}, "order_type": "market", "price": {{close}}, "time": "{{timenow}}"}
```

Upload the backtest so drift has a baseline (`returns` as a list of % per trade, or `csv` with the
Strategy Tester export):

```bash
curl -X POST localhost:8000/bots/btc-trend/backtest -H "Authorization: Bearer $ALGOGUARD_ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{"returns": [0.8, -0.4, 1.2, ...], "market_vol": 2.1}'
```

## Measured so far (simulated data only)

`scripts/replay_demo.py` replays a simulated long-only BTC bot (80 alerts to learn, then 96 normal alerts with
96 injected faults: duplicates, 10x size, stale alerts, prices far from mark, shorts the bot never takes, 25x
leverage, limit-price typos, bursts). On seeds 7-9: **100% of faults held or blocked, 0-2.1% of normal alerts
held, 99th-percentile added latency under 6 ms**.

`scripts/tune_drift.py`: with 30 live trades drawn from the backtest itself, 8.5% of runs reach "warn" and 3%
reach "act"; a shift to a losing regime reaches "act" after a median of 8 trades (90th percentile 14).

These show the prototype works as designed on faults we chose. They are not results on real bots or real money.

## Not verified yet

- **Delta API.** `algoguard/delta.py` was written from Delta's public docs and is tested only against mocks.
  Run `scripts/check_delta.py` on the testnet and fix any endpoint or field that differs.
- **Liquidation estimate.** `algoguard/liquidation.py` uses the standard isolated-margin formula for a new
  position; use Delta's reported liquidation price for open positions and check the maintenance margin.
- **TradingView CSV headers** change over time; check `parse_tradingview_csv` against a real export.
- **Telegram** sending is ported from IntelliStock, which tested it only against a mock.
- State is in memory: restarting the server forgets bots, history and holds. A database comes next.

## Licence

MIT. Parts are ported from IntelliStock and, through it, day-trader-skill; see `THIRD_PARTY_NOTICES.md`.
