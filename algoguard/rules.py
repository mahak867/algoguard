"""Hard rules: the trader's own limits, checked as plain code after the AI. Nothing here learns or guesses.

Ported from IntelliStock's pre-trade check (backend/tools/pretrade.py), which was itself adapted from
day-trader-skill (MIT, see THIRD_PARTY_NOTICES.md), and from IntelliStock's broker typo guards
(backend/services/broker_service.py). Changed for crypto perpetual futures on Delta Exchange:
markets never close, so there is no session check; limits are in leverage, distance to liquidation and
daily loss; size is in contracts. As in the original, every limit that caps the size is named, so the
trader can see which rule binds.

The AI can only make things stricter: it may HOLD an order these rules allow, but it can never send an
order these rules BLOCK. `check` takes no input from any model.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from algoguard.liquidation import liquidation_price
from algoguard.models import Account, Finding, Signal


@dataclass
class Limits:
    """Set by the trader, per bot. Defaults are deliberately cautious."""
    max_daily_loss: float = 100.0            # settlement currency; trading stops for the day once lost
    max_leverage: float = 5.0
    min_liq_distance_pct: float = 15.0       # after the fill, mark price must stay this far from liquidation
    max_position_contracts: int = 1000       # absolute size of the position after the fill
    max_order_notional: float | None = None  # typo guard, not a risk limit; None = 50% of equity
    max_price_gap_pct: float = 20.0          # a limit price this far from mark is almost always a typo
    maintenance_margin_pct: float = 0.25     # fallback; Delta's product data is used when available
    blocked_symbols: list[str] = field(default_factory=list)
    paused: bool = False                     # the kill switch


def _new_position(sig: Signal, acct: Account) -> int:
    delta = sig.size if sig.side == "buy" else -sig.size
    return acct.position.size + delta


def check(sig: Signal, acct: Account, limits: Limits, kill_switch: bool = False) -> list[Finding]:
    """Every rule the order breaks, as BLOCK findings, plus INFO findings saying which limit binds."""
    out: list[Finding] = []

    def block(code: str, message: str, **facts) -> None:
        out.append(Finding(code, "rules", "block", message, facts))

    if kill_switch or limits.paused:
        block("kill_switch", "Trading is paused (kill switch). No orders are sent until you resume.")
    if sig.symbol in {s.strip().upper() for s in limits.blocked_symbols}:
        block("blocked_symbol", f"{sig.symbol} is on your blocked list.", symbol=sig.symbol)

    pos = acct.position
    new = _new_position(sig, acct)
    reduces = abs(new) < abs(pos.size) and (new == 0 or (new > 0) == (pos.size > 0))
    if sig.reduce_only and not reduces:
        block("reduce_only_increases", "The order is marked reduce-only but would open or grow a position.",
              position=pos.size, order=sig.size)

    # Daily loss: once hit, only orders that shrink the position may go through.
    if acct.realised_pnl_today <= -abs(limits.max_daily_loss) and not reduces:
        block("daily_loss_limit", f"Today's loss {-acct.realised_pnl_today:g} has reached your limit of "
              f"{limits.max_daily_loss:g}. Only orders that reduce the position are allowed until tomorrow.",
              loss_today=round(-acct.realised_pnl_today, 2), limit=limits.max_daily_loss)

    # Price typo guard (from IntelliStock's broker service); applies to closing orders too.
    ref = sig.price or acct.mark_price
    if sig.price and acct.mark_price:
        gap = abs(sig.price - acct.mark_price) / acct.mark_price * 100
        if gap > limits.max_price_gap_pct:
            block("price_gap", f"The price {sig.price:g} is {gap:.0f}% from the mark price {acct.mark_price:g}; "
                  f"more than {limits.max_price_gap_pct:g}% is almost always a typo.",
                  price=sig.price, mark=acct.mark_price, gap_pct=round(gap, 1), limit_pct=limits.max_price_gap_pct)

    if reduces:
        return out  # closing risk is always allowed past the size, leverage and liquidation limits

    # Order value typo guard (from IntelliStock's broker service).
    ref = sig.price or acct.mark_price
    notional = sig.size * acct.contract_value * ref
    cap = limits.max_order_notional if limits.max_order_notional is not None else 0.5 * acct.equity
    if notional > cap:
        block("order_value", f"The order is worth {notional:,.2f}, over your cap of {cap:,.2f}.",
              notional=round(notional, 2), cap=round(cap, 2))

    # Size: which limit binds (named, as in IntelliStock's sizing).
    caps = {"position cap": limits.max_position_contracts}
    if acct.equity > 0 and acct.contract_value > 0 and ref > 0:
        caps["leverage"] = int(limits.max_leverage * acct.equity / (acct.contract_value * ref))
    bound = min(caps, key=caps.get)
    out.append(Finding("binding_limit", "rules", "info", f"The {bound} limit allows at most {caps[bound]} contracts.",
                       {"limit": bound, "max_contracts": caps[bound]}))
    if abs(new) > caps["position cap"]:
        block("position_cap", f"The position would be {abs(new)} contracts, over your cap of {caps['position cap']}.",
              position_after=abs(new), cap=caps["position cap"])

    eff_lev = abs(new) * acct.contract_value * ref / acct.equity if acct.equity > 0 else float("inf")
    if sig.leverage and sig.leverage > limits.max_leverage:
        block("leverage_setting", f"The order asks for {sig.leverage:g}x leverage; your limit is {limits.max_leverage:g}x.",
              leverage=sig.leverage, limit=limits.max_leverage)
    if eff_lev > limits.max_leverage:
        block("leverage", f"After the fill the position would be {eff_lev:.1f}x your equity; your limit is "
              f"{limits.max_leverage:g}x.", effective_leverage=round(eff_lev, 2), limit=limits.max_leverage)

    if sig.size * acct.contract_value * ref / max(sig.leverage or pos.leverage or 1.0, 1.0) > acct.available_margin:
        block("margin", "There is not enough free margin for this order.", available=round(acct.available_margin, 2))

    # Distance to liquidation after the fill. Prefer the exchange's own number for the current position;
    # for the position after the fill, estimate it (isolated margin, linear contract).
    lev_after = sig.leverage or pos.leverage or 1.0
    if pos.size and (new > 0) == (pos.size > 0) and pos.entry_price:
        entry_after = (abs(pos.size) * pos.entry_price + sig.size * ref) / abs(new)
    else:
        entry_after = ref
    mm = acct.maintenance_margin_pct if acct.maintenance_margin_pct is not None else limits.maintenance_margin_pct
    liq = liquidation_price("long" if new > 0 else "short", entry_after, lev_after, mm)
    dist = abs(acct.mark_price - liq) / acct.mark_price * 100 if acct.mark_price else 0.0
    if dist < limits.min_liq_distance_pct:
        block("liquidation_distance", f"After the fill, liquidation would be about {liq:,.0f}, only {dist:.1f}% from "
              f"the mark price; your minimum is {limits.min_liq_distance_pct:g}%.",
              liquidation=round(liq, 2), distance_pct=round(dist, 1), limit_pct=limits.min_liq_distance_pct)
    return out
