"""Read-only data access for the dashboard, CLI status and monitoring.

Pure functions over a :class:`Warehouse` returning DataFrames — no
Streamlit imports here, so everything is unit-testable. The Streamlit app
wraps these with ``st.cache_data``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import and_, case, desc, func, or_, select

from trendpulse.ingestion.quota import quota_day_bounds
from trendpulse.settings import Settings
from trendpulse.storage import schema as s
from trendpulse.storage.db import Warehouse, as_utc, utcnow

GLOBAL = "GLOBAL"


# ------------------------------------------------------------------ platform / monitoring
def publication(wh: Warehouse) -> dict | None:
    gp = s.gold_publications
    df = wh.read_df(select(gp).order_by(desc(gp.c.snapshot_at)).limit(1))
    return None if df.empty else df.iloc[0].to_dict()


def data_sources(wh: Warehouse) -> pd.DataFrame:
    pr = s.pipeline_runs
    return wh.read_df(select(pr.c.data_source, func.count().label("runs"), func.min(pr.c.started_at).label("first"),
                             func.max(pr.c.started_at).label("last")).group_by(pr.c.data_source))


def recent_runs(wh: Warehouse, limit: int = 50) -> pd.DataFrame:
    pr = s.pipeline_runs
    return wh.read_df(select(pr).order_by(desc(pr.c.started_at)).limit(limit))


def stage_runs(wh: Warehouse, run_id: str) -> pd.DataFrame:
    t = s.pipeline_stage_runs
    return wh.read_df(select(t).where(t.c.run_id == run_id).order_by(t.c.stage_order))


def stage_duration_stats(wh: Warehouse, last_runs: int = 48) -> pd.DataFrame:
    pr, t = s.pipeline_runs, s.pipeline_stage_runs
    ids = select(pr.c.run_id).order_by(desc(pr.c.started_at)).limit(last_runs).scalar_subquery()
    df = wh.read_df(select(t.c.stage, t.c.stage_order, t.c.duration_ms, t.c.status).where(t.c.run_id.in_(ids)))
    if df.empty:
        return df
    return (df.groupby(["stage_order", "stage"]).agg(avg_ms=("duration_ms", "mean"), p95_ms=("duration_ms",
                                                                                              lambda x: x.quantile(.95)),
                                                     runs=("duration_ms", "size"),
                                                     failures=("status", lambda x: int((x == "failed").sum())))
            .reset_index().sort_values("stage_order"))


def platform_health(wh: Warehouse, settings: Settings, now: datetime | None = None) -> dict:
    now = now or utcnow()
    pr = s.pipeline_runs
    last_attempt = wh.read_df(select(pr).order_by(desc(pr.c.started_at)).limit(1))
    last_ok = wh.read_df(select(pr).where(pr.c.gold_published.is_(True)).order_by(desc(pr.c.started_at)).limit(1))
    stats = wh.read_df(select(pr.c.status, func.count().label("n")).where(pr.c.started_at >= now - timedelta(days=7))
                       .group_by(pr.c.status))
    total_7d = int(stats["n"].sum()) if not stats.empty else 0
    ok_7d = int(stats.loc[stats.status.isin(["success", "partial"]), "n"].sum()) if not stats.empty else 0
    pub = publication(wh)
    snapshot_at = as_utc(pub["snapshot_at"]) if pub else None
    fresh_min = (now - snapshot_at).total_seconds() / 60 if snapshot_at else None
    interval = settings.pipeline.schedule.interval_minutes
    start, end = quota_day_bounds(now, settings.pipeline.quota.timezone)
    al = s.api_request_log
    used = wh.scalar(select(func.coalesce(func.sum(al.c.quota_cost), 0))
                     .where(and_(al.c.requested_at >= start, al.c.requested_at < end))) or 0
    if fresh_min is None:
        freshness = "no data"
    elif fresh_min <= interval * 2:
        freshness = "fresh"
    elif fresh_min <= settings.pipeline.quality.stale_after_minutes * 3:
        freshness = "delayed"
    else:
        freshness = "stale"
    return {
        "last_attempt": None if last_attempt.empty else last_attempt.iloc[0].to_dict(),
        "last_success": None if last_ok.empty else last_ok.iloc[0].to_dict(),
        "snapshot_at": snapshot_at, "freshness_minutes": fresh_min, "freshness": freshness,
        "data_source": pub["data_source"] if pub else None,
        "runs_7d": total_7d, "success_7d": ok_7d, "success_rate_7d": (ok_7d / total_7d) if total_7d else None,
        "status_counts_7d": dict(zip(stats.status, stats.n, strict=True)) if not stats.empty else {},
        "quota_used_today": int(used), "quota_budget": settings.pipeline.quota.daily_budget,
        "quota_limit": settings.pipeline.quota.daily_limit, "quota_day_resets_at": end,
        "interval_minutes": interval,
    }


def throughput(wh: Warehouse, days: int = 7, now: datetime | None = None) -> pd.DataFrame:
    now = now or utcnow()
    pr = s.pipeline_runs
    df = wh.read_df(select(pr.c.started_at, pr.c.status, pr.c.duration_seconds, pr.c.records_bronze,
                           pr.c.records_silver, pr.c.records_quarantined, pr.c.records_gold, pr.c.quota_used,
                           pr.c.videos_observed, pr.c.topics_observed)
                    .where(pr.c.started_at >= now - timedelta(days=days)).order_by(pr.c.started_at))
    if not df.empty:
        df["records_per_second"] = df["records_silver"] / df["duration_seconds"].where(df["duration_seconds"] > 0)
    return df


def quota_usage(wh: Warehouse, settings: Settings, days: int = 7, now: datetime | None = None) -> pd.DataFrame:
    now = now or utcnow()
    al = s.api_request_log
    df = wh.read_df(select(al.c.requested_at, al.c.endpoint, al.c.quota_cost, al.c.http_status)
                    .where(al.c.requested_at >= now - timedelta(days=days)))
    if df.empty:
        return df
    df["quota_day"] = df["requested_at"].dt.tz_convert(settings.pipeline.quota.timezone).dt.date
    return (df.groupby(["quota_day", "endpoint"]).agg(units=("quota_cost", "sum"), calls=("quota_cost", "size"),
                                                      errors=("http_status", lambda x: int((x >= 400).sum())))
            .reset_index())


def api_errors(wh: Warehouse, limit: int = 50) -> pd.DataFrame:
    al = s.api_request_log
    return wh.read_df(select(al.c.requested_at, al.c.endpoint, al.c.region_code, al.c.http_status, al.c.attempt,
                             al.c.error).where(or_(al.c.http_status >= 400, al.c.http_status.is_(None)))
                      .order_by(desc(al.c.requested_at)).limit(limit))


def dq_results(wh: Warehouse, run_id: str | None = None, days: int = 2, now: datetime | None = None) -> pd.DataFrame:
    t = s.dq_results
    q = select(t)
    if run_id:
        q = q.where(t.c.run_id == run_id)
    else:
        q = q.where(t.c.executed_at >= (now or utcnow()) - timedelta(days=days))
    return wh.read_df(q.order_by(desc(t.c.executed_at), t.c.check_name))


def quarantine(wh: Warehouse, limit: int = 200) -> pd.DataFrame:
    t = s.quarantine_records
    return wh.read_df(select(t.c.quarantined_at, t.c.run_id, t.c.check_name, t.c.reason, t.c.record_key, t.c.payload)
                      .order_by(desc(t.c.quarantined_at)).limit(limit))


def review_queue(wh: Warehouse, limit: int = 200) -> pd.DataFrame:
    b, v, tp = s.bridge_video_topics, s.dim_videos, s.dim_topics
    return wh.read_df(select(b.c.video_id, v.c.title, b.c.topic_id, tp.c.canonical_name, b.c.confidence,
                             b.c.review_status, b.c.matched_terms, b.c.updated_at)
                      .select_from(b.join(v, b.c.video_id == v.c.video_id, isouter=True)
                                   .join(tp, b.c.topic_id == tp.c.topic_id, isouter=True))
                      .where(b.c.review_status.in_(["needs_review", "unclassified"]))
                      .order_by(desc(b.c.updated_at), desc(b.c.confidence)).limit(limit))


def warehouse_counts(wh: Warehouse) -> dict[str, int | None]:
    out = {}
    for t in (s.bronze_api_responses, s.silver_video_observations, s.fact_video_observations,
              s.fact_topic_observations, s.dim_videos, s.dim_channels, s.dim_topics, s.quarantine_records):
        try:
            out[t.name] = int(wh.scalar(select(func.count()).select_from(t)) or 0)
        except Exception:  # noqa: BLE001 - e.g. the read-only role has no access to raw Bronze
            out[t.name] = None
    return out


# ------------------------------------------------------------------ analytics
def regions(wh: Warehouse) -> pd.DataFrame:
    return wh.read_df(select(s.dim_regions).order_by(s.dim_regions.c.region_code))


def trending(wh: Warehouse, region: str = GLOBAL) -> pd.DataFrame:
    t = s.gold_trending_topics
    return wh.read_df(select(t).where(t.c.region_code == region).order_by(t.c.popularity_rank))


def trending_all(wh: Warehouse) -> pd.DataFrame:
    return wh.read_df(select(s.gold_trending_topics))


def growth(wh: Warehouse, region: str = GLOBAL, window: str | None = None) -> pd.DataFrame:
    g, tp = s.gold_topic_growth, s.dim_topics
    q = (select(g, tp.c.canonical_name.label("topic_name"), tp.c.category, tp.c.topic_kind)
         .select_from(g.join(tp, g.c.topic_id == tp.c.topic_id, isouter=True)).where(g.c.region_code == region))
    if window:
        q = q.where(g.c.window == window)
    return wh.read_df(q)


def topics(wh: Warehouse) -> pd.DataFrame:
    tp = s.dim_topics
    return wh.read_df(select(tp).where(tp.c.first_observed_at.is_not(None)).order_by(desc(tp.c.last_observed_at)))


def topic_history(wh: Warehouse, topic_id: str, region: str | None = None, days: int = 30,
                  now: datetime | None = None) -> pd.DataFrame:
    f = s.fact_topic_observations
    q = select(f).where(and_(f.c.topic_id == topic_id, f.c.observed_at >= (now or utcnow()) - timedelta(days=days)))
    if region:
        q = q.where(f.c.region_code == region)
    return wh.read_df(q.order_by(f.c.observed_at))


def top_topics_history(wh: Warehouse, region: str = GLOBAL, top_n: int = 8, days: int = 2,
                       now: datetime | None = None) -> pd.DataFrame:
    cur = trending(wh, region).head(top_n)
    if cur.empty:
        return cur
    f = s.fact_topic_observations
    df = wh.read_df(select(f.c.observed_at, f.c.topic_id, f.c.popularity_score, f.c.video_count, f.c.popularity_rank)
                    .where(and_(f.c.region_code == region, f.c.topic_id.in_(list(cur.topic_id)),
                                f.c.observed_at >= (now or utcnow()) - timedelta(days=days)))
                    .order_by(f.c.observed_at))
    return df.merge(cur[["topic_id", "topic_name"]], on="topic_id")


def topic_videos(wh: Warehouse, topic_id: str, region: str | None = None, limit: int = 25) -> pd.DataFrame:
    b, v, c = s.bridge_video_topics, s.dim_videos, s.dim_channels
    q = (select(v.c.video_id, v.c.title, c.c.channel_title, v.c.latest_view_count, v.c.latest_like_count,
                v.c.latest_comment_count, v.c.published_at, v.c.first_observed_at, v.c.last_observed_at,
                v.c.thumbnail_url, b.c.confidence, b.c.assignment_method)
         .select_from(b.join(v, b.c.video_id == v.c.video_id).join(c, v.c.channel_id == c.c.channel_id, isouter=True))
         .where(and_(b.c.topic_id == topic_id, b.c.review_status.in_(["auto", "unclassified"]))))
    if region and region != GLOBAL:
        f = s.fact_video_observations
        vids = select(f.c.video_id).where(f.c.region_code == region).distinct().scalar_subquery()
        q = q.where(v.c.video_id.in_(vids))
    return wh.read_df(q.order_by(desc(v.c.last_observed_at), desc(v.c.latest_view_count)).limit(limit))


def topic_regions(wh: Warehouse, topic_id: str) -> pd.DataFrame:
    t = s.gold_trending_topics
    return wh.read_df(select(t.c.region_code, t.c.popularity_score, t.c.popularity_rank, t.c.video_count,
                             t.c.lifecycle_stage).where(and_(t.c.topic_id == topic_id, t.c.region_code != GLOBAL)))


def related_topics(wh: Warehouse, topic_id: str, limit: int = 10) -> pd.DataFrame:
    b, tp = s.bridge_video_topics, s.dim_topics
    b2 = b.alias("b2")
    return wh.read_df(select(b2.c.topic_id, tp.c.canonical_name, tp.c.category, func.count().label("shared_videos"))
                      .select_from(b.join(b2, b.c.video_id == b2.c.video_id).join(tp, b2.c.topic_id == tp.c.topic_id))
                      .where(and_(b.c.topic_id == topic_id, b2.c.topic_id != topic_id,
                                  b.c.review_status == "auto", b2.c.review_status == "auto"))
                      .group_by(b2.c.topic_id, tp.c.canonical_name, tp.c.category)
                      .order_by(desc("shared_videos")).limit(limit))


def regional_comparison(wh: Warehouse) -> pd.DataFrame:
    r, tp = s.gold_regional_comparison, s.dim_topics
    return wh.read_df(select(r, tp.c.canonical_name.label("topic_name"), tp.c.category)
                      .select_from(r.join(tp, r.c.topic_id == tp.c.topic_id, isouter=True)))


def category_summary(wh: Warehouse) -> pd.DataFrame:
    return wh.read_df(select(s.gold_category_summary))


def channels(wh: Warehouse, region: str = GLOBAL, limit: int = 100) -> pd.DataFrame:
    t = s.gold_channel_performance
    return wh.read_df(select(t).where(t.c.region_code == region)
                      .order_by(desc(t.c.appearances_24h), desc(t.c.total_views)).limit(limit))


def channel_regions(wh: Warehouse, channel_ids: list[str]) -> pd.DataFrame:
    t = s.gold_channel_performance
    if not channel_ids:
        return pd.DataFrame()
    return wh.read_df(select(t.c.channel_id, t.c.channel_title, t.c.region_code, t.c.appearances_24h)
                      .where(and_(t.c.channel_id.in_(channel_ids), t.c.region_code != GLOBAL)))


def channel_topics(wh: Warehouse, channel_id: str) -> pd.DataFrame:
    b, v, tp = s.bridge_video_topics, s.dim_videos, s.dim_topics
    return wh.read_df(select(tp.c.canonical_name, tp.c.category, func.count().label("videos"))
                      .select_from(b.join(v, b.c.video_id == v.c.video_id).join(tp, b.c.topic_id == tp.c.topic_id))
                      .where(and_(v.c.channel_id == channel_id, b.c.review_status == "auto"))
                      .group_by(tp.c.canonical_name, tp.c.category).order_by(desc("videos")))


def videos(wh: Warehouse, search: str | None = None, region: str | None = None, category: str | None = None,
           days: int = 7, limit: int = 500, now: datetime | None = None) -> pd.DataFrame:
    v, c, f = s.dim_videos, s.dim_channels, s.fact_video_observations
    since = (now or utcnow()) - timedelta(days=days)
    q = (select(v.c.video_id, v.c.title, c.c.channel_title, v.c.category_name, v.c.latest_view_count,
                v.c.latest_like_count, v.c.latest_comment_count, v.c.published_at, v.c.first_observed_at,
                v.c.last_observed_at, v.c.thumbnail_url)
         .select_from(v.join(c, v.c.channel_id == c.c.channel_id, isouter=True))
         .where(v.c.last_observed_at >= since))
    if search:
        like = f"%{search.lower()}%"
        q = q.where(or_(func.lower(v.c.title).like(like), func.lower(c.c.channel_title).like(like)))
    if category:
        q = q.where(v.c.category_name == category)
    if region and region != GLOBAL:
        q = q.where(v.c.video_id.in_(select(f.c.video_id).where(and_(f.c.region_code == region,
                                                                      f.c.observed_at >= since)).distinct()))
    df = wh.read_df(q.order_by(desc(v.c.last_observed_at), desc(v.c.latest_view_count)).limit(limit))
    if df.empty:
        return df
    reg = wh.read_df(select(f.c.video_id, f.c.region_code, func.min(f.c.chart_rank).label("best_rank"),
                            func.max(case((f.c.discovery_method == "regional_chart", 1), else_=0)).label("charted"))
                     .where(and_(f.c.video_id.in_(list(df.video_id)), f.c.observed_at >= since))
                     .group_by(f.c.video_id, f.c.region_code))
    agg = reg.groupby("video_id").agg(regions=("region_code", lambda x: ", ".join(sorted(set(x)))),
                                      best_chart_rank=("best_rank", "min"),
                                      on_chart=("charted", "max")).reset_index()
    return df.merge(agg, on="video_id", how="left")


def video_history(wh: Warehouse, video_id: str) -> pd.DataFrame:
    f = s.fact_video_observations
    return wh.read_df(select(f).where(f.c.video_id == video_id).order_by(f.c.observed_at))


def overview_counts(wh: Warehouse, now: datetime | None = None) -> dict:
    now = now or utcnow()
    t = s.gold_trending_topics
    g = trending(wh, GLOBAL)
    return {
        "active_topics": len(g),
        "emerging_topics": int((g.lifecycle_stage == "emerging").sum()) if not g.empty else 0,
        "growing_topics": int((g.lifecycle_stage == "growing").sum()) if not g.empty else 0,
        "videos_total": int(wh.scalar(select(func.count()).select_from(s.dim_videos)) or 0),
        "videos_24h": int(wh.scalar(select(func.count()).select_from(s.dim_videos)
                                    .where(s.dim_videos.c.last_observed_at >= now - timedelta(hours=24))) or 0),
        "channels_total": int(wh.scalar(select(func.count()).select_from(s.dim_channels)) or 0),
        "regions_monitored": int(wh.scalar(select(func.count(func.distinct(t.c.region_code)))
                                           .where(t.c.region_code != GLOBAL)) or 0),
        "topics_tracked": int(wh.scalar(select(func.count()).select_from(s.dim_topics)
                                        .where(s.dim_topics.c.first_observed_at.is_not(None))) or 0),
    }


def region_activity(wh: Warehouse) -> pd.DataFrame:
    """Per-region activity in the latest published snapshot."""
    t = s.gold_category_summary
    df = wh.read_df(select(t.c.region_code, func.sum(t.c.video_count).label("videos"),
                           func.sum(t.c.total_views).label("total_views"),
                           func.sum(t.c.topic_count).label("topic_assignments"))
                    .where(t.c.region_code != GLOBAL).group_by(t.c.region_code))
    tt = s.gold_trending_topics
    nt = wh.read_df(select(tt.c.region_code, func.count().label("active_topics"))
                    .where(tt.c.region_code != GLOBAL).group_by(tt.c.region_code))
    return df.merge(nt, on="region_code", how="left")


def growth_all_regions(wh: Warehouse, window: str = "24h") -> pd.DataFrame:
    g, tp = s.gold_topic_growth, s.dim_topics
    return wh.read_df(select(g.c.region_code, g.c.topic_id, g.c.score_growth_pct, g.c.growth_status,
                             tp.c.canonical_name.label("topic_name"))
                      .select_from(g.join(tp, g.c.topic_id == tp.c.topic_id, isouter=True))
                      .where(g.c.window == window))


def video_categories(wh: Warehouse) -> list[str]:
    v = s.dim_videos
    df = wh.read_df(select(v.c.category_name).where(v.c.category_name.is_not(None)).distinct())
    return sorted(df.category_name) if not df.empty else []
