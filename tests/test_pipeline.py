import numpy as np
import pytest

from algoguard.confirm import ConfirmError
from algoguard.drift import DriftConfig, LiveTrade
from algoguard.models import Outcome
from algoguard.pipeline import Bot, Guard
from algoguard.rules import Limits
from tests.helpers import T0, acct, sig


def guard(**limits):
    g = Guard("test-secret")
    g.add_bot(Bot("b1", "s", Limits(**limits)))
    return g


def test_normal_order_passes_with_explanation():
    d = guard().process(sig(), acct())
    assert d.outcome is Outcome.PASS and d.explanation.startswith("Sent")
    assert d.latency_ms < 100


def test_rules_block_even_when_model_crashes(monkeypatch):
    g = guard()
    monkeypatch.setattr(g.anomaly, "check", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    d = g.process(sig(size=5000), acct())
    assert d.outcome is Outcome.BLOCK
    assert "model_unavailable" in {f.code for f in d.findings}


def test_ai_cannot_release_a_rule_block(monkeypatch):
    g = guard()
    monkeypatch.setattr(g.anomaly, "check", lambda *a: ([], 0.0))  # the model says "perfectly normal"
    g.kill_switch = True
    assert g.process(sig(), acct()).outcome is Outcome.BLOCK


def test_hold_confirm_rechecks_rules_and_is_single_use():
    g = guard()
    d = g.process(sig(t=T0, sent_at=T0 - 600), acct())  # stale alert -> HOLD
    assert d.outcome is Outcome.HOLD and d.hold_id
    tok = g.held[d.hold_id].token
    out = g.resolve_hold(d.hold_id, tok, True, now=T0 + 5)
    assert out.outcome is Outcome.PASS
    with pytest.raises(KeyError):
        g.resolve_hold(d.hold_id, tok, True, now=T0 + 6)


def test_confirmed_hold_still_blocked_if_kill_switch_turned_on():
    g = guard()
    d = g.process(sig(t=T0, sent_at=T0 - 600), acct())
    g.kill_switch = True
    assert g.resolve_hold(d.hold_id, g.held[d.hold_id].token, True, now=T0 + 5).outcome is Outcome.BLOCK


def test_expired_hold():
    g = guard()
    d = g.process(sig(t=T0, sent_at=T0 - 600), acct())
    with pytest.raises(ConfirmError):
        g.resolve_hold(d.hold_id, g.held[d.hold_id].token, True, now=T0 + 500)


def test_daily_loss_from_recorded_trades():
    g = guard(max_daily_loss=100)
    g.record_trade("b1", LiveTrade(-1.0), pnl=-120, now=T0 - 60)
    d = g.process(sig(), acct())
    assert d.outcome is Outcome.BLOCK and "daily_loss_limit" in d.reasons


def test_drift_halves_opening_orders():
    g = Guard("k")
    bot = g.add_bot(Bot("b1", "s", Limits(), DriftConfig(action="halve_size")))
    bot.set_baseline(np.random.default_rng(1).normal(0.4, 1.5, 200), market_vol=2.0)
    for r in np.random.default_rng(2).normal(-1.5, 2.5, 12):
        g.record_trade("b1", LiveTrade(float(r), 5.0, 4.0))
    d = g.process(sig(size=10), acct())
    assert d.signal.size == 5 and "drift_halve" in {f.code for f in d.findings}


def test_hold_answer_feeds_back_to_the_model():
    g = guard()
    d = g.process(sig(t=T0, sent_at=T0 - 600), acct())
    g.resolve_hold(d.hold_id, g.held[d.hold_id].token, False, now=T0 + 5)
    assert g.anomaly.profile("b1").rejected == 1


def test_unanswered_holds_expire_and_are_logged_as_never_sent():
    from algoguard.models import Outcome
    from algoguard.pipeline import Bot, Guard
    from algoguard.rules import Limits
    from tests.helpers import T0, acct, sig
    g = Guard("k", hold_seconds=120)
    g.add_bot(Bot("b1", "s", Limits()))
    d = g.process(sig(t=T0, sent_at=T0 - 600), acct(), now=T0)  # a stale alert: held
    assert d.outcome is Outcome.HOLD and d.hold_id in g.held
    assert g.expire_holds(now=T0 + 60) == []
    gone = g.expire_holds(now=T0 + 121)
    assert [x.hold_id for x in gone] == [d.hold_id] and d.hold_id not in g.held
    assert gone[0].outcome is Outcome.BLOCK and "never sent" in gone[0].explanation
