from algoguard.models import Account, Position, Signal

T0 = 1_790_000_000.0  # a fixed clock for tests


def acct(size=0, mark=60_000.0, equity=10_000.0, avail=8_000.0, cv=0.001, entry=None, lev=5.0, pnl=0.0):
    return Account(equity, avail, mark, cv, Position("BTCUSD", size, entry, None, lev), pnl)


def sig(side="buy", size=10, t=T0, bot="b1", **kw):
    kw.setdefault("price", 60_000.0)
    kw.setdefault("sent_at", t - 1)
    return Signal(bot, "BTCUSD", side, size, received_at=t, **kw)
