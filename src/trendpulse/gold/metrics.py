"""Pure metric definitions (unit-tested in isolation).

Naming is explicit on purpose:

* ``total_views`` — sum of the *cumulative lifetime* view counts of the
  videos observed for a topic right now. It is a size measure, NOT growth.
* ``views_gained`` / ``views_per_hour`` — views accumulated *since each
  video's previous observation*, summed over the topic. This is the
  topic-level view growth signal.
* ``popularity_score`` — transparent composite (see :func:`popularity_score`).
* ``growth_pct`` — percentage change of a metric between two snapshots.
* ``velocity`` — change in popularity score per hour between two snapshots.
* ``acceleration`` — change in velocity per hour across successive windows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from trendpulse.settings import LifecycleCfg, ScoringCfg


def _missing(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def growth_pct(current: float | None, previous: float | None) -> float | None:
    """((current - previous) / previous) * 100 with explicit edge cases.

    * previous missing          -> None   (no baseline: "new")
    * previous == 0, current > 0 -> None   (undefined: growth from zero)
    * previous == 0, current == 0 -> 0.0
    * current missing           -> treated as 0 (topic no longer observed => -100%)
    """
    if _missing(previous):
        return None
    cur = 0.0 if _missing(current) else float(current)
    prev = float(previous)
    if prev == 0:
        return 0.0 if cur == 0 else None
    return (cur - prev) / abs(prev) * 100.0


def velocity(score_now: float | None, score_prev: float | None, hours: float | None) -> float | None:
    """Popularity-score change per hour."""
    if _missing(score_prev) or _missing(hours) or hours <= 0:
        return None
    return ((0.0 if _missing(score_now) else score_now) - score_prev) / hours


def acceleration(v_now: float | None, v_prev: float | None, hours: float | None) -> float | None:
    """Velocity change per hour (score/hour²) between two consecutive windows."""
    if _missing(v_now) or _missing(v_prev) or _missing(hours) or hours <= 0:
        return None
    return (v_now - v_prev) / hours


def engagement_rate(views, likes, comments) -> float | None:
    """(likes + comments) / views. Hidden counts are treated as 0; None when views <= 0."""
    if _missing(views) or views is None or views <= 0:
        return None
    if _missing(likes) and _missing(comments):
        return None
    return ((0 if _missing(likes) else likes) + (0 if _missing(comments) else comments)) / views


def popularity_score(video_count: int, channel_count: int, views_per_hour: float | None,
                     avg_engagement_rate: float | None, w: ScoringCfg) -> float:
    """score = w_v*ln(1+videos) + w_c*ln(1+channels) + w_vph*log10(1+views/hour) + w_e*(engagement%).

    Log terms damp mega-hits so breadth (many videos, many channels) matters.
    Absolute (not run-relative) so scores are comparable across time.
    """
    vph = 0.0 if _missing(views_per_hour) else max(0.0, float(views_per_hour))
    eng = 0.0 if _missing(avg_engagement_rate) else max(0.0, float(avg_engagement_rate))
    return round(w.w_videos * math.log1p(video_count) + w.w_channels * math.log1p(channel_count)
                 + w.w_vph * math.log10(1 + vph) + w.w_engagement * eng * 100, 4)


def growth_status(pct: float | None, has_baseline: bool, present_now: bool, present_before: bool,
                  threshold: float) -> str:
    if not has_baseline:
        return "no_history"
    if not present_now and present_before:
        return "vanished"
    if not present_before:
        return "new"
    if pct is None:
        return "new"
    if pct >= threshold:
        return "rising"
    if pct <= -threshold:
        return "falling"
    return "stable"


@dataclass
class LifecycleInput:
    present_now: bool
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    score_now: float
    max_score_7d: float
    growth_pct: float | None
    platform_history_hours: float
    prior_growth_pct: float | None = None   # growth over the preceding window of the same length


LIFECYCLE_STAGES = ["baseline", "emerging", "growing", "peaking", "stable", "declining", "dormant"]


def lifecycle_stage(x: LifecycleInput, now: datetime, cfg: LifecycleCfg) -> str:
    """Transparent, ordered rules (thresholds in pipeline.yaml -> lifecycle).

    1. baseline  — platform has < min_platform_history_hours of history; nothing can be "new" yet.
    2. dormant   — not observed for >= dormant_hours.  (declining if absent for less.)
    3. emerging  — first observed < emerging_hours ago.
    4. growing   — lookback growth >= growth_threshold_pct.
    5. declining — growth <= -decline_threshold_pct, or score < decline_from_peak_ratio * 7-day max.
    6. peaking   — growth has flattened (|growth| < threshold) right after a rise (the preceding
                   window grew >= growth_threshold_pct) and score >= peak_ratio * 7-day max.
    7. stable    — everything else (flat, no preceding rise). Added so flat topics are never mislabelled.
    """
    if x.platform_history_hours < cfg.min_platform_history_hours:
        return "baseline"
    if not x.present_now:
        if x.last_observed_at is None or (now - x.last_observed_at).total_seconds() / 3600 >= cfg.dormant_hours:
            return "dormant"
        return "declining"
    if x.first_observed_at is not None and (now - x.first_observed_at).total_seconds() / 3600 < cfg.emerging_hours:
        return "emerging"
    g = x.growth_pct
    if g is not None and g >= cfg.growth_threshold_pct:
        return "growing"
    if (g is not None and g <= -cfg.decline_threshold_pct) or (
            x.max_score_7d > 0 and x.score_now < cfg.decline_from_peak_ratio * x.max_score_7d):
        return "declining"
    rose = x.prior_growth_pct is not None and x.prior_growth_pct >= cfg.growth_threshold_pct
    if x.max_score_7d > 0 and x.score_now >= cfg.peak_ratio * x.max_score_7d and rose:
        return "peaking"
    return "stable"
