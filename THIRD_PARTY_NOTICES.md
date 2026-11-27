# Third-party notices

## IntelliStock

Parts of AlgoGuard are ported from [IntelliStock](https://github.com/mahak867/intellistock) (MIT, same author):

| AlgoGuard | From IntelliStock | Changed |
|---|---|---|
| `algoguard/rules.py` | `backend/tools/pretrade.py` (pre-trade check, named binding limit) and `backend/services/broker_service.py` (price-gap and order-value typo guards) | Crypto perpetuals: no market hours, limits in leverage, liquidation distance and daily loss, size in contracts |
| `algoguard/confirm.py` | order confirmation tokens in `backend/services/broker_service.py` | Bound to a HOLD id; single use enforced in memory |
| `algoguard/vault.py` | `backend/brokers/vault.py` | Takes its keys as arguments |
| `algoguard/notify.py` | `TelegramSender` in `backend/tools/alerts.py` | Optional confirm/reject buttons |
| `algoguard/drift.py` (`bootstrap_percentile`) | `bootstrap_drawdown` in `backend/tools/backtest.py` | Bootstraps backtest trades instead of daily returns |

## day-trader-skill

IntelliStock's `pretrade.py` was itself adapted from `trader-risk/check.py` and `trader-budget/size.py` in
[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill), so `algoguard/rules.py` carries that
licence too:

```
MIT License

Copyright (c) 2026 Jake Schincariol

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
