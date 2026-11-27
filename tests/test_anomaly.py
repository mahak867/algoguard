import random

from algoguard.anomaly import AnomalyChecker, AnomalyConfig
from tests.helpers import T0, acct, sig


def trained(n=60, seed=1):
    """A long-only bot that buys 8-12 contracts and sells to flat, about every 2 hours."""
    rnd = random.Random(seed)
    ch = AnomalyChecker()
    pos, t = 0, T0
    for i in range(n):
        t += 7200 + rnd.uniform(-600, 600)
        size = rnd.randint(8, 12) if pos == 0 else pos
        s = sig("buy" if pos == 0 else "sell", size, t)
        a = acct(size=pos)
        ch.check(s, a)
        ch.record(s, a)
        ch.refit_if_due("b1")
        pos = size if pos == 0 else 0
    return ch, t


def codes(fs, sev=None):
    return {f.code for f in fs if sev is None or f.severity == sev}


def test_duplicate_blocks():
    ch = AnomalyChecker()
    ch.check(sig(t=T0), acct())
    fs, _ = ch.check(sig(t=T0 + 3), acct())
    assert "duplicate" in codes(fs, "block")
    fs, _ = ch.check(sig(t=T0 + 60), acct())
    assert "duplicate" not in codes(fs)


def test_stale_alert_and_stale_price_hold():
    ch = AnomalyChecker()
    fs, _ = ch.check(sig(t=T0, sent_at=T0 - 300), acct())
    assert "stale_alert" in codes(fs, "hold")
    fs, _ = ch.check(sig(t=T0 + 100, size=11, price=57_000.0), acct(mark=60_000.0))
    assert "stale_price" in codes(fs, "hold")


def test_burst_holds():
    ch = AnomalyChecker(AnomalyConfig(burst_limit=3))
    fs = []
    for i in range(5):
        fs, _ = ch.check(sig(size=10 + i, t=T0 + i), acct())
    assert "burst" in codes(fs, "hold")


def test_cold_start_is_reported():
    fs, score = AnomalyChecker().check(sig(), acct())
    assert "cold_start" in codes(fs) and score is None


def test_size_typo_held():
    ch, t = trained()
    fs, _ = ch.check(sig(size=100, t=t + 7200), acct())
    assert "size_unusual" in codes(fs, "hold")


def test_short_on_long_only_bot_held():
    ch, t = trained()
    fs, _ = ch.check(sig("sell", 10, t + 7200), acct(size=0))
    assert "unexpected_short" in codes(fs, "hold")


def test_normal_alert_not_held_by_model():
    ch, t = trained()
    fs, score = ch.check(sig("buy", 10, t + 7200), acct(size=0))
    assert score is not None and score < 0.97
    assert codes(fs, "hold") == set() and codes(fs, "block") == set()


def test_model_flags_out_of_character_order():
    ch, t = trained()
    fs, score = ch.check(sig("buy", 30, t + 5, leverage=20), acct(size=0))
    assert score is not None and score >= 0.9
