import math
from datetime import timedelta

import pytest

from tests.helpers import NOW
from trendpulse.gold.metrics import (
    LifecycleInput,
    acceleration,
    engagement_rate,
    growth_pct,
    growth_status,
    lifecycle_stage,
    popularity_score,
    velocity,
)
from trendpulse.settings import LifecycleCfg, ScoringCfg


@pytest.mark.parametrize("cur,prev,expected", [
    (150, 100, 50.0), (50, 100, -50.0), (100, 100, 0.0),
    (10, 0, None),          # growth from zero is undefined, not infinite
    (0, 0, 0.0),
    (10, None, None), (10, float("nan"), None),   # no baseline
    (None, 100, -100.0),    # topic disappeared
    (-5, -10, 50.0),
])
def test_growth_pct(cur, prev, expected):
    got = growth_pct(cur, prev)
    assert (got is None and expected is None) or math.isclose(got, expected)


def test_velocity_and_acceleration():
    assert velocity(120, 100, 2) == 10
    assert velocity(None, 100, 4) == -25          # vanished topic
    assert velocity(120, None, 2) is None and velocity(1, 1, 0) is None
    assert acceleration(10, 4, 2) == 3
    assert acceleration(None, 4, 2) is None and acceleration(1, 1, -1) is None


def test_engagement_rate():
    assert engagement_rate(1000, 40, 10) == 0.05
    assert engagement_rate(1000, None, 10) == 0.01      # hidden likes
    assert engagement_rate(0, 1, 1) is None and engagement_rate(100, None, None) is None


def test_popularity_score_is_monotonic_and_documented():
    w = ScoringCfg()
    base = popularity_score(5, 3, 1000, 0.02, w)
    assert popularity_score(6, 3, 1000, 0.02, w) > base
    assert popularity_score(5, 4, 1000, 0.02, w) > base
    assert popularity_score(5, 3, 2000, 0.02, w) > base
    expected = 20 * math.log1p(5) + 10 * math.log1p(3) + 8 * math.log10(1001) + 2.0
    assert math.isclose(base, round(expected, 4))
    assert popularity_score(1, 1, None, None, w) == popularity_score(1, 1, 0, 0, w)


def test_growth_status():
    assert growth_status(None, False, True, False, 15) == "no_history"
    assert growth_status(None, True, True, False, 15) == "new"
    assert growth_status(-100, True, False, True, 15) == "vanished"
    assert growth_status(20, True, True, True, 15) == "rising"
    assert growth_status(-20, True, True, True, 15) == "falling"
    assert growth_status(5, True, True, True, 15) == "stable"


CFG = LifecycleCfg()


def li(**kw):
    base = dict(present_now=True, first_observed_at=NOW - timedelta(days=3), last_observed_at=NOW, score_now=100.0,
                max_score_7d=100.0, growth_pct=0.0, platform_history_hours=200, prior_growth_pct=None)
    base.update(kw)
    return LifecycleInput(**base)


@pytest.mark.parametrize("kw,stage", [
    (dict(platform_history_hours=2), "baseline"),
    (dict(present_now=False, last_observed_at=NOW - timedelta(hours=30)), "dormant"),
    (dict(present_now=False, last_observed_at=NOW - timedelta(hours=3)), "declining"),
    (dict(first_observed_at=NOW - timedelta(hours=5)), "emerging"),
    (dict(growth_pct=40.0), "growing"),
    (dict(growth_pct=-30.0), "declining"),
    (dict(score_now=50.0, max_score_7d=100.0, growth_pct=1.0), "declining"),   # far below 7-day peak
    (dict(growth_pct=2.0, prior_growth_pct=45.0), "peaking"),                  # rose, now flat at the top
    (dict(growth_pct=2.0, prior_growth_pct=1.0), "stable"),                    # flat, never rose
    (dict(growth_pct=None), "stable"),
])
def test_lifecycle_rules(kw, stage):
    assert lifecycle_stage(li(**kw), NOW, CFG) == stage


def test_lifecycle_thresholds_are_configurable():
    cfg = LifecycleCfg(growth_threshold_pct=50)
    assert lifecycle_stage(li(growth_pct=40.0), NOW, cfg) == "stable"
