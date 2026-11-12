"""Silver -> Gold: dimensions, append-only facts and serving models.

Everything in :func:`publish_gold` happens in ONE database transaction:
dims are upserted, facts appended, serving tables recomputed and swapped,
and the publication recorded. If any step raises, the transaction rolls
back and the previously published Gold snapshot stays exactly as it was.

Aggregations over the in-memory batch run in DuckDB (columnar, SQL,
zero-copy over pandas); history lookups run in the warehouse with indexed
range queries so the pipeline never pulls full history over the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import duckdb
import numpy as np
import pandas as pd
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.engine import Connection

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
from trendpulse.logging_utils import get_logger
from trendpulse.settings import Settings
from trendpulse.storage import schema as s
from trendpulse.storage.db import Warehouse, as_utc, records_from_df
from trendpulse.topics.classifier import UNCLASSIFIED_ID
from trendpulse.topics.engine import TopicOutput, taxonomy_dim_rows
from trendpulse.youtube_categories import category_name

log = get_logger("gold")

GLOBAL = "GLOBAL"
SHARED_MIN_REGIONS = 3
CHANNELS_PER_REGION = 300

FVO_COLS = [c.name for c in s.fact_video_observations.columns]
FTO_COLS = [c.name for c in s.fact_topic_observations.columns]


@dataclass
class GoldInputs:
    run_id: str
    now: datetime
    data_source: str
    valid: pd.DataFrame
    topics: TopicOutput


# ------------------------------------------------------------------ facts
def build_video_facts(valid: pd.DataFrame, prev: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """One row per (run, video, region, discovery context) + delta vs the video's previous observation."""
    df = valid.copy()
    df = df.merge(prev.rename(columns={"latest_view_count": "prev_views", "last_observed_at": "prev_at"}),
                  on="video_id", how="left")
    df["engagement_rate"] = [engagement_rate(v, lk, c) for v, lk, c in
                             zip(df["view_count"].astype("float"), df["like_count"].astype("float"),
                                 df["comment_count"].astype("float"), strict=True)]
    prev_at = pd.to_datetime(df["prev_at"], utc=True)
    hours = (pd.Timestamp(now) - prev_at).dt.total_seconds() / 3600
    hours = hours.where(hours > 0)
    delta = df["view_count"].astype("float") - df["prev_views"].astype("float")
    df["views_delta"] = delta.where(hours.notna())
    df["hours_since_prev"] = hours
    df["views_per_hour"] = (df["views_delta"] / hours).where(df["views_delta"] >= 0)
    df["observed_at"] = pd.Timestamp(now)
    return df[FVO_COLS]


def build_topic_facts(fvo: pd.DataFrame, valid: pd.DataFrame, assignments: pd.DataFrame,
                      settings: Settings, run_id: str, now: datetime) -> pd.DataFrame:
    obs = (fvo.merge(valid[["video_id", "channel_id"]].drop_duplicates("video_id"), on="video_id", how="left")
           .drop_duplicates(["video_id", "region_code"]))
    for c in ("view_count", "like_count", "comment_count", "views_delta"):
        obs[c] = obs[c].astype("float")
    assign = assignments.loc[assignments.review_status.isin(["auto", "unclassified"]), ["video_id", "topic_id"]]
    con = duckdb.connect()
    try:
        con.register("obs", obs[["video_id", "region_code", "channel_id", "view_count", "like_count",
                                 "comment_count", "engagement_rate", "views_delta", "views_per_hour"]])
        con.register("assign", assign)
        sql = """
        WITH j AS (SELECT o.*, a.topic_id FROM obs o JOIN assign a USING (video_id)),
        per_video AS (   -- a video counts once globally even if charted in many regions
            SELECT topic_id, video_id, any_value(channel_id) AS channel_id, max(view_count) AS view_count,
                   max(like_count) AS like_count, max(comment_count) AS comment_count,
                   max(engagement_rate) AS engagement_rate, max(views_delta) AS views_delta,
                   max(views_per_hour) AS views_per_hour
            FROM j GROUP BY topic_id, video_id),
        unioned AS (
            SELECT topic_id, region_code, video_id, channel_id, view_count, like_count, comment_count,
                   engagement_rate, views_delta, views_per_hour FROM j
            UNION ALL
            SELECT topic_id, 'GLOBAL', video_id, channel_id, view_count, like_count, comment_count,
                   engagement_rate, views_delta, views_per_hour FROM per_video)
        SELECT topic_id, region_code,
               count(DISTINCT video_id)          AS video_count,
               count(DISTINCT channel_id)        AS channel_count,
               sum(view_count)                   AS total_views,
               avg(view_count)                   AS avg_views,
               sum(like_count)                   AS total_likes,
               sum(comment_count)                AS total_comments,
               avg(engagement_rate)              AS avg_engagement_rate,
               sum(greatest(views_delta, 0))     AS views_gained,
               sum(views_per_hour)               AS views_per_hour
        FROM unioned GROUP BY topic_id, region_code
        """
        agg = con.execute(sql).df()
    finally:
        con.close()
    if agg.empty:
        return pd.DataFrame(columns=FTO_COLS)
    w = settings.pipeline.scoring
    agg["popularity_score"] = [
        popularity_score(int(vc), int(cc), None if pd.isna(vph) else float(vph), None if pd.isna(er) else float(er), w)
        for vc, cc, vph, er in zip(agg.video_count, agg.channel_count, agg.views_per_hour,
                                   agg.avg_engagement_rate, strict=True)]
    ranked = agg[agg.topic_id != UNCLASSIFIED_ID].copy()
    ranked = ranked.sort_values(["region_code", "popularity_score", "topic_id"], ascending=[True, False, True])
    ranked["popularity_rank"] = ranked.groupby("region_code").cumcount() + 1
    agg = agg.merge(ranked[["topic_id", "region_code", "popularity_rank"]], on=["topic_id", "region_code"],
                    how="left")
    agg["popularity_rank"] = agg["popularity_rank"].fillna(0).astype(int)  # 0 = not ranked (Unclassified)
    agg["run_id"] = run_id
    agg["observed_at"] = pd.Timestamp(now)
    for c in ("total_views", "total_likes", "total_comments", "views_gained"):
        agg[c] = agg[c].round().astype("Int64")
    return agg[FTO_COLS]


# ------------------------------------------------------------------ history helpers
def find_snapshot(conn: Connection, wh: Warehouse, target: datetime, tolerance: timedelta,
                  exclude_run: str) -> tuple[str, datetime] | None:
    """Closest published topic snapshot to ``target`` within ``tolerance``."""
    t = s.fact_topic_observations
    df = wh.read_df(select(t.c.run_id, func.max(t.c.observed_at).label("observed_at"))
                    .where(and_(t.c.observed_at >= target - tolerance, t.c.observed_at <= target + tolerance,
                                t.c.run_id != exclude_run))
                    .group_by(t.c.run_id), conn)
    if df.empty:
        return None
    df["dist"] = (df["observed_at"] - pd.Timestamp(target)).abs()
    row = df.sort_values("dist").iloc[0]
    return row["run_id"], as_utc(row["observed_at"])


def load_snapshot(conn: Connection, wh: Warehouse, run_id: str) -> pd.DataFrame:
    t = s.fact_topic_observations
    return wh.read_df(select(t.c.topic_id, t.c.region_code, t.c.popularity_score, t.c.video_count,
                             t.c.views_per_hour, t.c.popularity_rank, t.c.observed_at)
                      .where(t.c.run_id == run_id), conn)


def compute_growth(conn: Connection, wh: Warehouse, fto: pd.DataFrame, settings: Settings, run_id: str,
                   now: datetime) -> pd.DataFrame:
    p = settings.pipeline
    tol = p.window_tolerance
    cur = fto[fto.topic_id != UNCLASSIFIED_ID][["topic_id", "region_code", "popularity_score", "video_count",
                                                "views_per_hour", "popularity_rank"]]
    out = []
    cache: dict[str, pd.DataFrame] = {}

    def snap(target: datetime, minutes: int):
        found = find_snapshot(conn, wh, target, timedelta(minutes=minutes * tol), run_id)
        if not found:
            return None, None, None
        rid, at = found
        if rid not in cache:
            cache[rid] = load_snapshot(conn, wh, rid)
        return rid, at, cache[rid]

    for label, minutes in p.comparison_windows.items():
        rid1, at1, prev = snap(now - timedelta(minutes=minutes), minutes)
        _, at2, prev2 = snap(now - timedelta(minutes=2 * minutes), minutes) if prev is not None else (None, None, None)
        if prev is None:
            g = cur.copy()
            for c in ("score_prev", "video_count_prev", "vph_prev"):
                g[c] = np.nan
            g["present_before"] = False
        else:
            pv = prev[prev.topic_id != UNCLASSIFIED_ID].rename(columns={
                "popularity_score": "score_prev", "video_count": "video_count_prev", "views_per_hour": "vph_prev",
                "popularity_rank": "rank_prev"})[["topic_id", "region_code", "score_prev", "video_count_prev",
                                                   "vph_prev"]]
            g = cur.merge(pv, on=["topic_id", "region_code"], how="outer", indicator=True)
            g["present_before"] = g["_merge"] != "left_only"
            g = g.drop(columns="_merge")
        g["present_now"] = g["popularity_score"].notna()
        h1 = (now - at1).total_seconds() / 3600 if at1 else None
        h2 = (at1 - at2).total_seconds() / 3600 if (at1 and at2) else None
        p2 = None
        if prev2 is not None:
            p2 = prev2.rename(columns={"popularity_score": "score_prev2"})[["topic_id", "region_code", "score_prev2"]]
            g = g.merge(p2, on=["topic_id", "region_code"], how="left")
        rows = []
        for r in g.itertuples(index=False):
            s_now = None if pd.isna(r.popularity_score) else float(r.popularity_score)
            s_prev = None if pd.isna(r.score_prev) else float(r.score_prev)
            v_now = velocity(s_now if s_now is not None else 0.0, s_prev, h1)
            v_prev = None
            prior = None
            if p2 is not None and h2:
                sp2 = getattr(r, "score_prev2", np.nan)
                sp2 = None if pd.isna(sp2) else float(sp2)
                v_prev = velocity(s_prev, sp2, h2)
                prior = growth_pct(s_prev, sp2) if s_prev is not None else None
            pct = growth_pct(s_now, s_prev) if prev is not None else None
            rows.append({
                "snapshot_run_id": run_id, "snapshot_at": now, "region_code": r.region_code,
                "topic_id": r.topic_id, "window": label, "window_minutes": minutes,
                "compare_run_id": rid1, "compare_observed_at": at1,
                "score_now": s_now, "score_prev": s_prev, "score_growth_pct": pct,
                "video_count_now": None if pd.isna(r.video_count) else int(r.video_count),
                "video_count_prev": None if pd.isna(r.video_count_prev) else int(r.video_count_prev),
                "video_count_growth_pct": growth_pct(r.video_count, r.video_count_prev) if prev is not None else None,
                "vph_now": None if pd.isna(r.views_per_hour) else float(r.views_per_hour),
                "vph_prev": None if pd.isna(r.vph_prev) else float(r.vph_prev),
                "vph_growth_pct": growth_pct(r.views_per_hour, r.vph_prev) if prev is not None else None,
                "velocity": v_now, "acceleration": acceleration(v_now, v_prev, h1), "prior_growth_pct": prior,
                "growth_status": growth_status(pct, prev is not None, bool(r.present_now), bool(r.present_before),
                                               p.lifecycle.growth_threshold_pct),
            })
        out.append(pd.DataFrame(rows))
    if not out:
        return pd.DataFrame(columns=[c.name for c in s.gold_topic_growth.columns])
    return pd.concat(out, ignore_index=True)


def _rank_lookup(conn, wh, settings: Settings, now: datetime, run_id: str, label: str) -> pd.DataFrame:
    minutes = settings.pipeline.comparison_windows.get(label)
    if not minutes:
        return pd.DataFrame(columns=["topic_id", "region_code", "rank_prev"])
    found = find_snapshot(conn, wh, now - timedelta(minutes=minutes),
                          timedelta(minutes=minutes * settings.pipeline.window_tolerance), run_id)
    if not found:
        return pd.DataFrame(columns=["topic_id", "region_code", "rank_prev"])
    snap = load_snapshot(conn, wh, found[0])
    return snap.rename(columns={"popularity_rank": "rank_prev"})[["topic_id", "region_code", "rank_prev"]]


# ------------------------------------------------------------------ publication
def _dims(conn: Connection, wh: Warehouse, inp: GoldInputs, settings: Settings) -> None:
    now, valid = inp.now, inp.valid
    # regions
    present = set(valid["region_code"])
    regions = [{"region_code": c.code, "country_name": c.name, "is_active": c.enabled}
               for c in settings.countries.countries]
    wh.upsert(conn, s.dim_regions, regions, ["region_code"], ["country_name", "is_active"])
    if present:
        conn.execute(update(s.dim_regions).where(and_(s.dim_regions.c.region_code.in_(present),
                                                      s.dim_regions.c.first_ingested_at.is_(None)))
                     .values(first_ingested_at=now))
        conn.execute(update(s.dim_regions).where(s.dim_regions.c.region_code.in_(present))
                     .values(last_ingested_at=now))
    # channels
    ch = valid.drop_duplicates("channel_id")[["channel_id", "channel_title"]].copy()
    ch["first_observed_at"] = now
    ch["last_observed_at"] = now
    wh.upsert(conn, s.dim_channels, records_from_df(ch), ["channel_id"], ["channel_title", "last_observed_at"])
    # videos (latest metadata + metrics; first_observed_at is insert-only)
    v = valid.sort_values("observed_at").drop_duplicates("video_id", keep="last").copy()
    v["category_name"] = v["category_id"].map(lambda c: None if pd.isna(c) else category_name(int(c)))
    v["first_observed_at"] = now
    v["last_observed_at"] = now
    v = v.rename(columns={"view_count": "latest_view_count", "like_count": "latest_like_count",
                          "comment_count": "latest_comment_count"})
    cols = [c.name for c in s.dim_videos.columns]
    wh.upsert(conn, s.dim_videos, records_from_df(v, cols), ["video_id"],
              [c for c in cols if c not in ("video_id", "first_observed_at")])
    # topics: taxonomy (from config), new discovered, observation timestamps
    wh.upsert(conn, s.dim_topics, taxonomy_dim_rows(settings), ["topic_id"],
              ["canonical_name", "category", "keywords", "is_active"])
    if not inp.topics.topics.empty:
        wh.upsert(conn, s.dim_topics, records_from_df(inp.topics.topics), ["topic_id"], [])
    if inp.topics.aliases:
        wh.upsert(conn, s.topic_aliases, inp.topics.aliases, ["alias_key"], [])
    # bridge: replace assignments for the videos seen in this run
    a = inp.topics.assignments.copy()
    vids = list(set(a["video_id"]))
    for i in range(0, len(vids), 500):
        conn.execute(delete(s.bridge_video_topics).where(s.bridge_video_topics.c.video_id.in_(vids[i:i + 500])))
    a["assigned_run_id"] = inp.run_id
    a["updated_at"] = now
    wh.insert_many(conn, s.bridge_video_topics, records_from_df(a))


def _touch_topics(conn: Connection, topic_ids: list[str], now: datetime) -> None:
    t = s.dim_topics
    for i in range(0, len(topic_ids), 500):
        chunk = topic_ids[i:i + 500]
        conn.execute(update(t).where(and_(t.c.topic_id.in_(chunk), t.c.first_observed_at.is_(None)))
                     .values(first_observed_at=now))
        conn.execute(update(t).where(t.c.topic_id.in_(chunk)).values(last_observed_at=now))


def publish_gold(wh: Warehouse, inp: GoldInputs, settings: Settings) -> dict[str, int]:
    now, run_id = inp.now, inp.run_id
    with wh.begin() as conn:
        dv = s.dim_videos
        ids = list(inp.valid["video_id"].unique())
        prev_frames = []
        for i in range(0, len(ids), 500):
            prev_frames.append(wh.read_df(select(dv.c.video_id, dv.c.latest_view_count, dv.c.last_observed_at)
                                          .where(dv.c.video_id.in_(ids[i:i + 500])), conn))
        prev = pd.concat(prev_frames) if prev_frames else pd.DataFrame(
            columns=["video_id", "latest_view_count", "last_observed_at"])
        prev = prev[pd.to_datetime(prev["last_observed_at"], utc=True) < pd.Timestamp(now)]

        fvo = build_video_facts(inp.valid, prev, now)
        fto = build_topic_facts(fvo, inp.valid, inp.topics.assignments, settings, run_id, now)

        _dims(conn, wh, inp, settings)
        n_fvo = wh.upsert(conn, s.fact_video_observations, records_from_df(fvo),
                          ["run_id", "video_id", "region_code", "discovery_method", "discovery_query"], [])
        n_fto = wh.upsert(conn, s.fact_topic_observations, records_from_df(fto),
                          ["run_id", "topic_id", "region_code"], [])
        _touch_topics(conn, sorted(set(fto["topic_id"])), now)

        serving = build_serving(conn, wh, fto, fvo, inp, settings)
        for table in s.SERVING_TABLES:
            conn.execute(delete(table))
            rows = serving[table.name]
            if not rows.empty:
                wh.insert_many(conn, table, records_from_df(rows, [c.name for c in table.columns]))
        counts = {"fact_video_observations": n_fvo, "fact_topic_observations": n_fto,
                  **{k: len(v) for k, v in serving.items()}}
        wh.upsert(conn, s.gold_publications, [{
            "snapshot_run_id": run_id, "snapshot_at": now, "published_at": now,
            "data_source": inp.data_source, "row_counts": json.dumps(counts)}], ["snapshot_run_id"])
    log.info("gold published: %s", counts)
    return counts


def build_serving(conn: Connection, wh: Warehouse, fto: pd.DataFrame, fvo: pd.DataFrame, inp: GoldInputs,
                  settings: Settings) -> dict[str, pd.DataFrame]:
    now, run_id = inp.now, inp.run_id
    p = settings.pipeline
    t = s.fact_topic_observations

    growth = compute_growth(conn, wh, fto, settings, run_id, now)

    # ---- history aggregates (facts already include this run inside the transaction)
    hist = wh.read_df(select(t.c.topic_id, t.c.region_code, func.min(t.c.observed_at).label("first_observed_at"),
                             func.max(t.c.observed_at).label("last_observed_at"))
                      .group_by(t.c.topic_id, t.c.region_code), conn)
    max7 = wh.read_df(select(t.c.topic_id, t.c.region_code, func.max(t.c.popularity_score).label("max_score_7d"))
                      .where(t.c.observed_at >= now - timedelta(days=7))
                      .group_by(t.c.topic_id, t.c.region_code), conn)
    first_run = wh.scalar(select(func.min(s.gold_publications.c.snapshot_at)), conn)
    first_run = as_utc(first_run) if first_run else now
    platform_hours = (now - first_run).total_seconds() / 3600

    prev_pub = wh.read_df(select(s.gold_publications.c.snapshot_run_id)
                          .where(s.gold_publications.c.snapshot_run_id != run_id)
                          .order_by(s.gold_publications.c.snapshot_at.desc()).limit(1), conn)
    prev_run = prev_pub["snapshot_run_id"].iloc[0] if not prev_pub.empty else None
    gt = s.gold_trending_topics
    prev_serv = wh.read_df(select(gt.c.topic_id, gt.c.region_code, gt.c.consecutive_observations,
                                  gt.c.snapshot_run_id), conn)

    topics = wh.read_df(select(s.dim_topics.c.topic_id, s.dim_topics.c.canonical_name, s.dim_topics.c.category,
                               s.dim_topics.c.topic_kind), conn)

    cur = fto[(fto.topic_id != UNCLASSIFIED_ID)].copy()
    cur = cur.merge(topics, on="topic_id", how="left").merge(hist, on=["topic_id", "region_code"], how="left") \
             .merge(max7, on=["topic_id", "region_code"], how="left")

    lookback = p.lifecycle.lookback_window
    order = sorted(p.comparison_windows, key=lambda k: p.comparison_windows[k])
    fallbacks = [w for w in order if p.comparison_windows[w] <= p.comparison_windows[lookback]][::-1]
    gidx = growth.set_index(["topic_id", "region_code", "window"]) if not growth.empty else None

    def lookup(tid, reg, window, col):
        if gidx is None:
            return None
        try:
            v = gidx.at[(tid, reg, window), col]
        except KeyError:
            return None
        return None if pd.isna(v) else float(v)

    r1 = _rank_lookup(conn, wh, settings, now, run_id, "1h").rename(columns={"rank_prev": "rank_1h"})
    r24 = _rank_lookup(conn, wh, settings, now, run_id, "24h").rename(columns={"rank_prev": "rank_24h"})
    cur = cur.merge(r1, on=["topic_id", "region_code"], how="left").merge(r24, on=["topic_id", "region_code"],
                                                                          how="left")
    if prev_run is not None and not prev_serv.empty:
        ps = prev_serv[prev_serv.snapshot_run_id == prev_run][["topic_id", "region_code", "consecutive_observations"]]
        cur = cur.merge(ps.rename(columns={"consecutive_observations": "prev_consec"}),
                        on=["topic_id", "region_code"], how="left")
    else:
        cur["prev_consec"] = np.nan

    trend_rows = []
    for r in cur.itertuples(index=False):
        g, prior = None, None
        for w in fallbacks:
            g = lookup(r.topic_id, r.region_code, w, "score_growth_pct")
            if g is not None:
                prior = lookup(r.topic_id, r.region_code, w, "prior_growth_pct")
                break
        stage = lifecycle_stage(LifecycleInput(
            present_now=True, first_observed_at=as_utc(r.first_observed_at) if not pd.isna(r.first_observed_at) else now,
            last_observed_at=now, score_now=float(r.popularity_score),
            max_score_7d=float(r.max_score_7d) if not pd.isna(r.max_score_7d) else float(r.popularity_score),
            growth_pct=g, platform_history_hours=platform_hours, prior_growth_pct=prior), now, p.lifecycle)
        trend_rows.append({
            "snapshot_run_id": run_id, "snapshot_at": now, "region_code": r.region_code, "topic_id": r.topic_id,
            "topic_name": r.canonical_name if isinstance(r.canonical_name, str) else r.topic_id,
            "category": r.category if isinstance(r.category, str) else "Unclassified",
            "topic_kind": r.topic_kind if isinstance(r.topic_kind, str) else "discovered",
            "popularity_rank": int(r.popularity_rank),
            "rank_change_1h": None if pd.isna(r.rank_1h) or r.rank_1h == 0 else int(r.rank_1h - r.popularity_rank),
            "rank_change_24h": None if pd.isna(r.rank_24h) or r.rank_24h == 0 else int(r.rank_24h - r.popularity_rank),
            "popularity_score": float(r.popularity_score), "video_count": int(r.video_count),
            "channel_count": int(r.channel_count), "total_views": r.total_views,
            "views_per_hour": r.views_per_hour, "avg_engagement_rate": r.avg_engagement_rate,
            "growth_24h_pct": lookup(r.topic_id, r.region_code, "24h", "score_growth_pct"),
            "lifecycle_stage": stage,
            "first_observed_at": r.first_observed_at, "last_observed_at": now,
            "consecutive_observations": 1 if pd.isna(r.prev_consec) else int(r.prev_consec) + 1,
        })
    trending = pd.DataFrame(trend_rows)

    # lifecycle for topics no longer observed (declining/dormant) is carried in the growth table
    regional = _regional(cur)
    channels = _channels(conn, wh, fvo, inp, now)
    categories = _categories(inp, fvo, fto, topics)
    return {"gold_trending_topics": trending, "gold_topic_growth": growth, "gold_regional_comparison": regional,
            "gold_channel_performance": channels, "gold_category_summary": categories}


def _regional(cur: pd.DataFrame) -> pd.DataFrame:
    reg = cur[cur.region_code != GLOBAL][["snapshot_run_id" if "snapshot_run_id" in cur else "run_id", "topic_id",
                                          "region_code", "popularity_score", "popularity_rank"]].copy()
    reg = reg.rename(columns={"run_id": "snapshot_run_id"})
    counts = reg.groupby("topic_id")["region_code"].nunique().rename("regions_present")
    reg = reg.merge(counts, on="topic_id")
    reg["is_shared"] = reg["regions_present"] >= SHARED_MIN_REGIONS
    reg["is_country_specific"] = reg["regions_present"] == 1
    return reg


def _channels(conn: Connection, wh: Warehouse, fvo: pd.DataFrame, inp: GoldInputs, now: datetime) -> pd.DataFrame:
    f, v = s.fact_video_observations, s.dim_videos
    since = now - timedelta(hours=24)
    base = (select(v.c.channel_id, f.c.region_code, func.count().label("appearances_24h"),
                   func.count(func.distinct(f.c.video_id)).label("videos_24h"),
                   func.max(f.c.observed_at).label("latest_observed_at"))
            .select_from(f.join(v, f.c.video_id == v.c.video_id)).where(f.c.observed_at >= since))
    regional = wh.read_df(base.group_by(v.c.channel_id, f.c.region_code), conn)
    glob = wh.read_df(select(v.c.channel_id, func.count().label("appearances_24h"),
                             func.count(func.distinct(f.c.video_id)).label("videos_24h"),
                             func.max(f.c.observed_at).label("latest_observed_at"),
                             func.count(func.distinct(f.c.region_code)).label("regions_count"))
                      .select_from(f.join(v, f.c.video_id == v.c.video_id)).where(f.c.observed_at >= since)
                      .group_by(v.c.channel_id), conn)
    if glob.empty:
        return pd.DataFrame(columns=[c.name for c in s.gold_channel_performance.columns])
    glob["region_code"] = GLOBAL
    agg = pd.concat([regional, glob], ignore_index=True)

    # performance from latest observation of each video in the window
    latest = wh.read_df(select(v.c.video_id, v.c.channel_id, v.c.latest_view_count, v.c.latest_like_count,
                               v.c.latest_comment_count).where(v.c.last_observed_at >= since), conn)
    latest["er"] = [engagement_rate(a, b, c) for a, b, c in
                    zip(latest.latest_view_count.astype(float), latest.latest_like_count.astype(float),
                        latest.latest_comment_count.astype(float), strict=True)]
    vph = fvo.drop_duplicates("video_id")[["video_id", "views_per_hour"]]
    latest = latest.merge(vph, on="video_id", how="left")
    perf = latest.groupby("channel_id").agg(total_views=("latest_view_count", "sum"),
                                            avg_engagement_rate=("er", "mean"),
                                            views_per_hour=("views_per_hour", "sum")).reset_index()
    titles = wh.read_df(select(s.dim_channels.c.channel_id, s.dim_channels.c.channel_title), conn)

    b, dt = s.bridge_video_topics, s.dim_topics
    bt = wh.read_df(select(v.c.channel_id, b.c.topic_id, dt.c.canonical_name)
                    .select_from(b.join(v, b.c.video_id == v.c.video_id).join(dt, b.c.topic_id == dt.c.topic_id))
                    .where(and_(v.c.last_observed_at >= since, b.c.review_status == "auto")), conn)
    if not bt.empty:
        top = (bt.groupby(["channel_id", "topic_id", "canonical_name"]).size().rename("n").reset_index()
               .sort_values(["channel_id", "n", "topic_id"], ascending=[True, False, True])
               .drop_duplicates("channel_id")
               .rename(columns={"topic_id": "top_topic_id", "canonical_name": "top_topic_name"})
               [["channel_id", "top_topic_id", "top_topic_name"]])
        ntop = bt.groupby("channel_id")["topic_id"].nunique().rename("topics_count").reset_index()
    else:
        top = pd.DataFrame(columns=["channel_id", "top_topic_id", "top_topic_name"])
        ntop = pd.DataFrame(columns=["channel_id", "topics_count"])
    rc = glob[["channel_id", "regions_count"]]
    agg = agg.drop(columns=["regions_count"], errors="ignore")
    out = (agg.merge(perf, on="channel_id", how="left").merge(titles, on="channel_id", how="left")
           .merge(top, on="channel_id", how="left").merge(ntop, on="channel_id", how="left")
           .merge(rc, on="channel_id", how="left"))
    out["snapshot_run_id"] = inp.run_id
    out = (out.sort_values(["region_code", "appearances_24h", "total_views"], ascending=[True, False, False])
           .groupby("region_code").head(CHANNELS_PER_REGION))
    out["total_views"] = out["total_views"].round().astype("Int64")
    return out


def _categories(inp: GoldInputs, fvo: pd.DataFrame, fto: pd.DataFrame, topics: pd.DataFrame) -> pd.DataFrame:
    vc = pd.DataFrame(list(inp.topics.video_category.items()), columns=["video_id", "category"])
    obs = fvo.drop_duplicates(["video_id", "region_code"]).merge(vc, on="video_id", how="left")
    obs["category"] = obs["category"].fillna("Unclassified")
    obs["view_count"] = obs["view_count"].astype(float)
    glob = obs.drop_duplicates("video_id").assign(region_code=GLOBAL)
    allobs = pd.concat([obs, glob], ignore_index=True)
    agg = allobs.groupby(["region_code", "category"]).agg(
        video_count=("video_id", "nunique"), total_views=("view_count", "sum"),
        avg_engagement_rate=("engagement_rate", "mean")).reset_index()
    tc = (fto[(fto.topic_id != UNCLASSIFIED_ID) & (fto.video_count > 0)]
          .merge(topics[["topic_id", "category"]], on="topic_id", how="left")
          .groupby(["region_code", "category"])["topic_id"].nunique().rename("topic_count").reset_index())
    agg = agg.merge(tc, on=["region_code", "category"], how="left")
    agg["topic_count"] = agg["topic_count"].fillna(0).astype(int)
    agg["snapshot_run_id"] = inp.run_id
    agg["total_views"] = agg["total_views"].round().astype("Int64")
    return agg
