"""The deck's prototype targets, checked on the simulated replay (not real bots)."""
import pytest

from scripts.replay_demo import main as replay
from scripts.tune_drift import main as tune


@pytest.mark.parametrize("seed", [7, 8])
def test_replay_meets_targets(seed):
    r = replay(seed, quiet=True)
    assert r["faults_caught_pct"] >= 95
    assert r["normal_flagged_pct"] < 5
    assert r["latency_ms_p99"] < 100


def test_drift_detection_targets():
    r = tune(100)
    assert r["false_act_rate"] <= 0.06
    assert r["median_trades_to_act"] < 10
