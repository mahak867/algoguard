"""Shared data types: an incoming signal, the account as it is now, and AlgoGuard's decision."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Outcome(str, Enum):
    PASS = "PASS"    # sent to the exchange
    HOLD = "HOLD"    # waits for the trader to confirm
    BLOCK = "BLOCK"  # a hard rule failed; never sent


@dataclass
class Signal:
    """One order request, from a TradingView webhook alert or from an API trader's code."""
    bot_id: str
    symbol: str                      # Delta product symbol, e.g. BTCUSD
    side: str                        # buy | sell
    size: int                        # contracts
    order_type: str = "market"       # market | limit
    price: float | None = None       # limit price, or the price the alert saw ({{close}})
    sent_at: float | None = None     # epoch seconds the alert says it was sent ({{timenow}})
    received_at: float = 0.0         # epoch seconds AlgoGuard received it
    reduce_only: bool = False
    leverage: float | None = None    # leverage the order asks for; None = keep the account's setting
    source: str = "tradingview"      # tradingview | api
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.side = self.side.lower()
        self.symbol = self.symbol.strip().upper()
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if self.size <= 0:
            raise ValueError("size must be a positive number of contracts")
        if self.order_type not in ("market", "limit"):
            raise ValueError("order_type must be market or limit")
        if self.order_type == "limit" and not self.price:
            raise ValueError("a limit order needs a price")


@dataclass
class Position:
    symbol: str
    size: int = 0                    # contracts; positive long, negative short, 0 flat
    entry_price: float | None = None
    liquidation_price: float | None = None
    leverage: float = 1.0


@dataclass
class Account:
    """What the exchange says about the trader right now (read from Delta's API before each check)."""
    equity: float                    # wallet balance plus unrealised P&L, in the settlement currency
    available_margin: float
    mark_price: float                # of the signal's symbol
    contract_value: float            # underlying per contract, e.g. 0.001 BTC
    position: Position
    realised_pnl_today: float = 0.0  # since the start of the trader's day; negative is a loss


@dataclass
class Finding:
    """One reason behind a decision. `code` is stable for the explainer; `facts` are the numbers it may quote."""
    code: str
    source: str                      # rules | anomaly | drift | system
    severity: str                    # block | hold | info
    message: str
    facts: dict = field(default_factory=dict)


@dataclass
class Decision:
    outcome: Outcome
    signal: Signal
    findings: list[Finding]
    anomaly_score: float | None      # 0 (normal) to 1 (very unusual); None if the model did not run
    explanation: str = ""
    latency_ms: float = 0.0
    hold_id: str | None = None

    @property
    def reasons(self) -> list[str]:
        return [f.code for f in self.findings if f.severity != "info"]
