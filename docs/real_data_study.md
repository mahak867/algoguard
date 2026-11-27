# Evidence from real data

Two checks that use real data instead of simulation. Both can be re-run from this repository.

## 1. What Delta's algo traders ask about (Delta's own community forum)

[`docs/evidence/delta_forum_audit.csv`](evidence/delta_forum_audit.csv) lists every topic in the *APIs & Algo
Trading* category of [community.delta.exchange](https://community.delta.exchange/c/apis-algo-trading/6), read on
27 Nov 2026. We set aside 20 announcements, tutorials and promotions; 49 user questions remain (Nov 2024 to Oct 2026).

| Class | Topics |
|---|---|
| An order the trader meant to place did not go through (alert fired, no trade; order rejected; "Order Placed Successfully" logged while Delta returned an error) | 6 |
| The bot could not read or change its own orders or positions (empty active orders, positions calls failing, bracket stop loss not editable, leverage not changeable) | 9 |
| Other: data, authentication setup, how-to, account | 34 |

**15 of 49 user questions (about 1 in 3) are about an order not happening as intended or a bot not knowing its own
order or position state.** That is the gap Delta's TradingView guide warns about: when TradingView and Delta
disagree, *"the first alert can open a larger or opposite position than you expect."* AlgoGuard reads the live
position before every order and holds an alert that does not fit it.

The classification is ours, made from each topic's title and first post; the CSV links every topic so anyone can
check it. The sample is small, and people post problems more than successes, so this is a signal, not a rate.

## 2. Does the drift detector work on real prices?

`python scripts/real_data_study.py` downloads Delta India's public hourly candles for BTCUSD, ETHUSD and SOLUSD
(from Jan-Apr 2024 to Nov 2026, cached in `data/`) and runs a walk-forward test:

- **The bot:** an EMA crossover trend bot, always long or short, 3x leverage, 0.05% fee per side (an assumption).
  Prices are real; fills are simulated at the hourly close.
- **The trader:** every quarter, backtests 8 EMA settings on the previous 6 months, picks the best and runs it live
  for 3 months. Picking the best backtest is how an over-fitted baseline happens.
- **AlgoGuard:** gets that backtest as the baseline and sees every live trade through the real `DriftDetector`.
- **No peeking:** drift settings were tuned on live quarters before 1 Jul 2025 only, choosing the setting that saved
  the most there while wrongly acting in at most 10% of "nothing changed" runs (30 trades resampled from each real
  backtest). The results below are from the held-out period, 1 Jul 2025 to Nov 2026, which includes the
  10 October 2025 crash.

**Held-out results (15 bot-quarters, 12 of them losing):**

| | Default settings | `DriftConfig.delta_calibrated()` |
|---|---|---|
| Quarters where drift reached "act" | 1 | 5 |
| ...of which the bot was losing that quarter | 1 | **5 (no alarm in a winning quarter)** |
| Losing quarters caught | 1 of 12 | **5 of 12** |
| Median trades before "act" | 101 | 22 |
| False "act" when nothing changed | about 3% (simulated) | 9% |

**What it does not show:** halving or pausing the bot *after* the alarm saved little money in this test (average
quarter -32.9% without AlgoGuard, -32.6% with halving, -32.4% with pausing). Pausing helped in 3 of the 5 flagged
quarters and hurt in 2, where the bot recovered after the alarm. Most of the loss came before 22 trades had passed.
So the honest claim is that drift **tells a trader early, without crying wolf on winning quarters, that a live
bot no longer behaves like its backtest**; it is a prompt to review the strategy, not a profit engine. The daily
loss limit and the other hard rules are what cap losses inside a day.

Every simple trend bot here lost money over the period. That is part of the point: a strategy picked on its best
6-month backtest usually does worse live, and 12 of 15 held-out quarters lost.
