"""Retention and compaction policies.

Two constraints shape retention: free-tier Postgres (Supabase: 500 MB) and
the YouTube API Developer Policies (API metadata such as titles and channel
names must be refreshed or deleted within 30 days; longer storage of
statistics/derived metrics needs the "Analytics & Reporting" amendment).
History is tiered instead of silently dropped:

* dim_videos / dim_channels / bridge rows not refreshed for
  ``api_metadata_max_days`` (30) are deleted.

* Bronze raw payloads and Silver observations: kept ``bronze_days`` /
  ``silver_days`` (replay window).
* fact_video_observations: full 30-minute granularity for
  ``video_obs_full_days``; older history keeps only the LAST run of each
  UTC day.
* fact_topic_observations: full granularity for ``topic_obs_full_days``,
  then the last run of each UTC hour until ``topic_obs_hourly_days``, then
  the last run of each UTC day (so 7-day comparisons resolve hourly and
  30-day comparisons daily).
  30-day comparisons therefore resolve against hourly snapshots.
* API request log, DQ results, quarantine, run history: time-based deletes.

Compaction keeps real observations only — it never interpolates or invents.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, select

from trendpulse.logging_utils import get_logger
from trendpulse.settings import RetentionCfg
from trendpulse.storage import schema as s
from trendpulse.storage.db import Warehouse, utcnow

log = get_logger("retention")


def _compact(wh: Warehouse, table, bucket: str, older_than) -> int:
    """Keep only the LAST run per time bucket for rows older than ``older_than``.

    Compaction works on whole runs, so every surviving snapshot is complete
    (a 30-day comparison never reads a half-deleted snapshot).
    """
    runs = wh.read_df(select(table.c.run_id, func.max(table.c.observed_at).label("observed_at"))
                      .where(table.c.observed_at < older_than).group_by(table.c.run_id))
    if runs.empty:
        return 0
    runs["bucket"] = runs["observed_at"].dt.floor(bucket)
    runs = runs.sort_values("observed_at")
    keep = set(runs.groupby("bucket").tail(1)["run_id"])
    drop = [r for r in runs["run_id"] if r not in keep]
    deleted = 0
    with wh.begin() as conn:
        for i in range(0, len(drop), 200):
            deleted += conn.execute(delete(table).where(table.c.run_id.in_(drop[i:i + 200]))).rowcount or 0
    return deleted


def apply_retention(wh: Warehouse, cfg: RetentionCfg, now: datetime | None = None) -> dict[str, int]:
    """``now`` defaults to wall-clock; the pipeline passes its run clock."""
    now = now or utcnow()
    out: dict[str, int] = {}
    with wh.begin() as conn:
        def _del(name, table, col, days):
            out[name] = conn.execute(delete(table).where(col < now - timedelta(days=days))).rowcount or 0

        _del("bronze_api_responses", s.bronze_api_responses, s.bronze_api_responses.c.fetched_at, cfg.bronze_days)
        _del("silver_video_observations", s.silver_video_observations,
             s.silver_video_observations.c.observed_at, cfg.silver_days)
        _del("api_request_log", s.api_request_log, s.api_request_log.c.requested_at, cfg.api_log_days)
        _del("pipeline_log_records", s.pipeline_log_records, s.pipeline_log_records.c.logged_at, cfg.api_log_days)
        _del("dq_results", s.dq_results, s.dq_results.c.executed_at, cfg.pipeline_history_days)
        _del("quarantine_records", s.quarantine_records, s.quarantine_records.c.quarantined_at,
             cfg.quarantine_days)
        # YouTube Developer Policies: API metadata not refreshed within 30 days must be deleted.
        stale_cut = now - timedelta(days=cfg.api_metadata_max_days)
        stale_videos = select(s.dim_videos.c.video_id).where(s.dim_videos.c.last_observed_at < stale_cut)
        out["bridge_video_topics"] = conn.execute(delete(s.bridge_video_topics).where(
            s.bridge_video_topics.c.video_id.in_(stale_videos))).rowcount or 0
        _del("dim_videos", s.dim_videos, s.dim_videos.c.last_observed_at, cfg.api_metadata_max_days)
        _del("dim_channels", s.dim_channels, s.dim_channels.c.last_observed_at, cfg.api_metadata_max_days)
        old_runs = select(s.pipeline_runs.c.run_id).where(
            s.pipeline_runs.c.started_at < now - timedelta(days=cfg.pipeline_history_days))
        out["pipeline_stage_runs"] = conn.execute(
            delete(s.pipeline_stage_runs).where(s.pipeline_stage_runs.c.run_id.in_(old_runs))).rowcount or 0
        out["pipeline_runs"] = conn.execute(
            delete(s.pipeline_runs).where(and_(
                s.pipeline_runs.c.started_at < now - timedelta(days=cfg.pipeline_history_days),
                s.pipeline_runs.c.status != "running"))).rowcount or 0
        _del("fact_video_observations", s.fact_video_observations, s.fact_video_observations.c.observed_at,
             cfg.video_obs_max_days)
        _del("fact_topic_observations", s.fact_topic_observations, s.fact_topic_observations.c.observed_at,
             cfg.topic_obs_max_days)
        out["tracked_videos"] = conn.execute(
            delete(s.tracked_videos).where(s.tracked_videos.c.track_until < now - timedelta(days=1))).rowcount or 0

    out["fact_video_observations_compacted"] = _compact(
        wh, s.fact_video_observations, "1D", now - timedelta(days=cfg.video_obs_full_days))
    out["fact_topic_observations_compacted"] = _compact(
        wh, s.fact_topic_observations, "1h", now - timedelta(days=cfg.topic_obs_full_days))
    out["fact_topic_observations_compacted"] += _compact(
        wh, s.fact_topic_observations, "1D", now - timedelta(days=cfg.topic_obs_hourly_days))
    log.info("retention applied: %s", {k: v for k, v in out.items() if v})
    return out
