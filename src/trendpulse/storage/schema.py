"""Warehouse schema (SQLAlchemy Core).

Tables are declared once, without a schema name, and work on both
PostgreSQL (production, Supabase) and SQLite (local fallback / tests).
On PostgreSQL, the engine maps them into the ``trendpulse`` schema via
``schema_translate_map`` — that schema is *not* exposed by Supabase's
auto-generated REST API, so the anon key cannot read or write it.

Layers
------
* Operational metadata: pipeline_runs, pipeline_stage_runs, pipeline_lock,
  api_request_log, discovery_state, tracked_videos, dq_results,
  quarantine_records, gold_publications, schema_migrations
* Bronze: bronze_api_responses (gzip-compressed raw JSON, replayable)
* Silver: silver_video_observations (conformed, short retention),
  bridge_video_topics, topic_aliases
* Gold: dims, facts (append-only history) and serving tables
  (atomically replaced per published snapshot)
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
)

PG_SCHEMA = "trendpulse"

metadata = MetaData()


def ts(name: str, nullable: bool = True, **kw) -> Column:
    return Column(name, DateTime(timezone=True), nullable=nullable, **kw)


# ----------------------------------------------------------------- operational
schema_migrations = Table(
    "schema_migrations", metadata,
    Column("version", Integer, primary_key=True),
    Column("description", String(200), nullable=False),
    ts("applied_at", nullable=False),
)

pipeline_runs = Table(
    "pipeline_runs", metadata,
    Column("run_id", String(36), primary_key=True),
    Column("trigger", String(32), nullable=False),
    Column("data_source", String(32), nullable=False),
    Column("status", String(24), nullable=False),
    ts("started_at", nullable=False),
    ts("finished_at"),
    Column("duration_seconds", Float),
    Column("records_bronze", Integer, default=0),
    Column("records_silver", Integer, default=0),
    Column("records_quarantined", Integer, default=0),
    Column("records_gold", Integer, default=0),
    Column("videos_observed", Integer, default=0),
    Column("topics_observed", Integer, default=0),
    Column("quota_used", Integer, default=0),
    Column("gold_published", Boolean, default=False, nullable=False),
    Column("dq_status", String(16)),
    Column("error_message", Text),
    Column("git_sha", String(64)),
    Column("stage_summary", Text),
)
Index("ix_pipeline_runs_started", pipeline_runs.c.started_at)
Index("ix_pipeline_runs_status", pipeline_runs.c.status, pipeline_runs.c.started_at)

pipeline_stage_runs = Table(
    "pipeline_stage_runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(36), nullable=False),
    Column("stage", String(48), nullable=False),
    Column("stage_order", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    ts("started_at", nullable=False),
    ts("finished_at"),
    Column("duration_ms", Integer),
    Column("records_in", Integer),
    Column("records_out", Integer),
    Column("attempts", Integer, default=1),
    Column("error", Text),
    UniqueConstraint("run_id", "stage", name="uq_stage_run"),
)
Index("ix_stage_runs_run", pipeline_stage_runs.c.run_id)

pipeline_lock = Table(
    "pipeline_lock", metadata,
    Column("lock_name", String(64), primary_key=True),
    Column("owner_run_id", String(36), nullable=False),
    ts("acquired_at", nullable=False),
    ts("expires_at", nullable=False),
)

api_request_log = Table(
    "api_request_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(36)),
    Column("endpoint", String(48), nullable=False),
    Column("region_code", String(8)),
    Column("discovery_method", String(24)),
    Column("params_json", Text),
    Column("http_status", Integer),
    Column("quota_cost", Integer, nullable=False, default=0),
    Column("items_returned", Integer),
    Column("duration_ms", Integer),
    Column("attempt", Integer, default=1),
    ts("requested_at", nullable=False),
    Column("error", Text),
)
Index("ix_api_log_requested", api_request_log.c.requested_at)
Index("ix_api_log_run", api_request_log.c.run_id)

discovery_state = Table(
    "discovery_state", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),
    ts("updated_at", nullable=False),
)

tracked_videos = Table(
    "tracked_videos", metadata,
    Column("video_id", String(32), nullable=False),
    Column("region_code", String(8), nullable=False),
    Column("discovery_query", String(64), nullable=False),
    ts("first_discovered_at", nullable=False),
    ts("last_discovered_at", nullable=False),
    ts("track_until", nullable=False),
    PrimaryKeyConstraint("video_id", "region_code", "discovery_query", name="pk_tracked_videos"),
)
Index("ix_tracked_until", tracked_videos.c.track_until)

dq_results = Table(
    "dq_results", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(36), nullable=False),
    Column("check_name", String(64), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("total_checked", Integer, nullable=False),
    Column("passed", Integer, nullable=False),
    Column("failed", Integer, nullable=False),
    Column("warnings", Integer, nullable=False),
    Column("details", Text),
    ts("executed_at", nullable=False),
    UniqueConstraint("run_id", "check_name", name="uq_dq_run_check"),
)
Index("ix_dq_executed", dq_results.c.executed_at)

quarantine_records = Table(
    "quarantine_records", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(36), nullable=False),
    Column("stage", String(32), nullable=False),
    Column("check_name", String(64), nullable=False),
    Column("record_key", String(200)),
    Column("reason", Text, nullable=False),
    Column("payload", Text),
    ts("quarantined_at", nullable=False),
)
Index("ix_quarantine_run", quarantine_records.c.run_id)
Index("ix_quarantine_at", quarantine_records.c.quarantined_at)

gold_publications = Table(
    "gold_publications", metadata,
    Column("snapshot_run_id", String(36), primary_key=True),
    ts("snapshot_at", nullable=False),
    ts("published_at", nullable=False),
    Column("data_source", String(32), nullable=False),
    Column("row_counts", Text),
)
Index("ix_gold_pub_at", gold_publications.c.published_at)

# ----------------------------------------------------------------- bronze
bronze_api_responses = Table(
    "bronze_api_responses", metadata,
    Column("response_id", String(36), primary_key=True),
    Column("run_id", String(36), nullable=False),
    Column("endpoint", String(48), nullable=False),
    Column("discovery_method", String(24), nullable=False),
    Column("discovery_query", String(64), nullable=False),
    Column("region_code", String(8)),
    Column("category_id", Integer),
    Column("page_index", Integer, nullable=False, default=0),
    Column("request_params", Text, nullable=False),
    Column("http_status", Integer, nullable=False),
    Column("item_count", Integer, nullable=False),
    Column("payload_gz", LargeBinary, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    ts("fetched_at", nullable=False),
)
Index("ix_bronze_run", bronze_api_responses.c.run_id)
Index("ix_bronze_fetched", bronze_api_responses.c.fetched_at)

# ----------------------------------------------------------------- silver
silver_video_observations = Table(
    "silver_video_observations", metadata,
    Column("run_id", String(36), nullable=False),
    Column("video_id", String(32), nullable=False),
    Column("region_code", String(8), nullable=False),
    Column("discovery_method", String(24), nullable=False),
    Column("discovery_query", String(64), nullable=False),
    Column("chart_category_id", Integer),
    Column("chart_rank", Integer),
    ts("observed_at", nullable=False),
    # Slim on purpose: descriptive metadata (title, tags, description...) lives in
    # dim_videos and the raw payload in Bronze, so Silver stays cheap to retain.
    Column("channel_id", String(64), nullable=False),
    Column("category_id", Integer),
    ts("published_at"),
    Column("duration_seconds", Integer),
    Column("view_count", BigInteger),
    Column("like_count", BigInteger),
    Column("comment_count", BigInteger),
    Column("metadata_hash", String(16)),
    PrimaryKeyConstraint("run_id", "video_id", "region_code", "discovery_method", "discovery_query",
                         name="pk_silver_obs"),
)
Index("ix_silver_observed", silver_video_observations.c.observed_at)

topic_aliases = Table(
    "topic_aliases", metadata,
    Column("alias_key", String(200), primary_key=True),
    Column("topic_id", String(64), nullable=False),
    Column("surface_form", Text),
    ts("created_at", nullable=False),
)
Index("ix_alias_topic", topic_aliases.c.topic_id)

bridge_video_topics = Table(
    "bridge_video_topics", metadata,
    Column("video_id", String(32), nullable=False),
    Column("topic_id", String(64), nullable=False),
    Column("confidence", Float, nullable=False),
    Column("assignment_method", String(24), nullable=False),
    Column("review_status", String(16), nullable=False),
    Column("matched_terms", Text),
    Column("assigned_run_id", String(36), nullable=False),
    ts("updated_at", nullable=False),
    PrimaryKeyConstraint("video_id", "topic_id", name="pk_bridge_video_topics"),
)
Index("ix_bridge_topic", bridge_video_topics.c.topic_id)
Index("ix_bridge_review", bridge_video_topics.c.review_status)

# ----------------------------------------------------------------- gold: dims
dim_regions = Table(
    "dim_regions", metadata,
    Column("region_code", String(8), primary_key=True),
    Column("country_name", String(100), nullable=False),
    Column("is_active", Boolean, nullable=False),
    ts("first_ingested_at"),
    ts("last_ingested_at"),
)

dim_channels = Table(
    "dim_channels", metadata,
    Column("channel_id", String(64), primary_key=True),
    Column("channel_title", Text),
    ts("first_observed_at", nullable=False),
    ts("last_observed_at", nullable=False),
)

dim_videos = Table(
    "dim_videos", metadata,
    Column("video_id", String(32), primary_key=True),
    Column("title", Text, nullable=False),
    Column("channel_id", String(64), nullable=False),
    Column("category_id", Integer),
    Column("category_name", String(64)),
    ts("published_at"),
    Column("duration_seconds", Integer),
    Column("tags", Text),
    Column("description_snippet", Text),
    Column("thumbnail_url", Text),
    Column("default_language", String(16)),
    ts("first_observed_at", nullable=False),
    ts("last_observed_at", nullable=False),
    Column("latest_view_count", BigInteger),
    Column("latest_like_count", BigInteger),
    Column("latest_comment_count", BigInteger),
)
Index("ix_dim_videos_channel", dim_videos.c.channel_id)
Index("ix_dim_videos_last_obs", dim_videos.c.last_observed_at)

dim_topics = Table(
    "dim_topics", metadata,
    Column("topic_id", String(64), primary_key=True),
    Column("canonical_name", String(200), nullable=False),
    Column("canonical_key", String(200), nullable=False),
    Column("topic_kind", String(16), nullable=False),  # taxonomy | discovered | unclassified
    Column("category", String(64), nullable=False),
    Column("parent_topic_id", String(64)),
    Column("keywords", Text),
    ts("first_observed_at"),
    ts("last_observed_at"),
    Column("created_run_id", String(36)),
    Column("is_active", Boolean, nullable=False, default=True),
)
Index("ix_dim_topics_kind", dim_topics.c.topic_kind)

# ----------------------------------------------------------------- gold: facts (append-only history)
fact_video_observations = Table(
    "fact_video_observations", metadata,
    Column("run_id", String(36), nullable=False),
    ts("observed_at", nullable=False),
    Column("video_id", String(32), nullable=False),
    Column("region_code", String(8), nullable=False),
    Column("discovery_method", String(24), nullable=False),
    Column("discovery_query", String(64), nullable=False),
    Column("chart_category_id", Integer),
    Column("chart_rank", Integer),
    Column("view_count", BigInteger),
    Column("like_count", BigInteger),
    Column("comment_count", BigInteger),
    Column("engagement_rate", Float),
    Column("views_delta", BigInteger),
    Column("hours_since_prev", Float),
    Column("views_per_hour", Float),
    PrimaryKeyConstraint("run_id", "video_id", "region_code", "discovery_method", "discovery_query",
                         name="pk_fact_video_obs"),
)
Index("ix_fvo_video_time", fact_video_observations.c.video_id, fact_video_observations.c.observed_at)
Index("ix_fvo_region_time", fact_video_observations.c.region_code, fact_video_observations.c.observed_at)
Index("ix_fvo_observed", fact_video_observations.c.observed_at)

fact_topic_observations = Table(
    "fact_topic_observations", metadata,
    Column("run_id", String(36), nullable=False),
    ts("observed_at", nullable=False),
    Column("topic_id", String(64), nullable=False),
    Column("region_code", String(8), nullable=False),  # ISO code or GLOBAL
    Column("video_count", Integer, nullable=False),
    Column("channel_count", Integer, nullable=False),
    Column("total_views", BigInteger),
    Column("avg_views", Float),
    Column("total_likes", BigInteger),
    Column("total_comments", BigInteger),
    Column("avg_engagement_rate", Float),
    Column("views_gained", BigInteger),
    Column("views_per_hour", Float),
    Column("popularity_score", Float, nullable=False),
    Column("popularity_rank", Integer, nullable=False),
    PrimaryKeyConstraint("run_id", "topic_id", "region_code", name="pk_fact_topic_obs"),
)
Index("ix_fto_topic_region_time", fact_topic_observations.c.topic_id, fact_topic_observations.c.region_code,
      fact_topic_observations.c.observed_at)
Index("ix_fto_region_time", fact_topic_observations.c.region_code, fact_topic_observations.c.observed_at)
Index("ix_fto_observed", fact_topic_observations.c.observed_at)

# ----------------------------------------------------------------- gold: serving (latest published snapshot)
gold_trending_topics = Table(
    "gold_trending_topics", metadata,
    Column("snapshot_run_id", String(36), nullable=False),
    ts("snapshot_at", nullable=False),
    Column("region_code", String(8), nullable=False),
    Column("topic_id", String(64), nullable=False),
    Column("topic_name", String(200), nullable=False),
    Column("category", String(64), nullable=False),
    Column("topic_kind", String(16), nullable=False),
    Column("popularity_rank", Integer, nullable=False),
    Column("rank_change_1h", Integer),
    Column("rank_change_24h", Integer),
    Column("popularity_score", Float, nullable=False),
    Column("video_count", Integer, nullable=False),
    Column("channel_count", Integer, nullable=False),
    Column("total_views", BigInteger),
    Column("views_per_hour", Float),
    Column("avg_engagement_rate", Float),
    Column("growth_24h_pct", Float),
    Column("lifecycle_stage", String(16), nullable=False),
    ts("first_observed_at"),
    ts("last_observed_at"),
    Column("consecutive_observations", Integer, nullable=False),
    PrimaryKeyConstraint("region_code", "topic_id", name="pk_gold_trending"),
)

gold_topic_growth = Table(
    "gold_topic_growth", metadata,
    Column("snapshot_run_id", String(36), nullable=False),
    ts("snapshot_at", nullable=False),
    Column("region_code", String(8), nullable=False),
    Column("topic_id", String(64), nullable=False),
    Column("window", String(8), nullable=False),
    Column("window_minutes", Integer, nullable=False),
    Column("compare_run_id", String(36)),
    ts("compare_observed_at"),
    Column("score_now", Float),
    Column("score_prev", Float),
    Column("score_growth_pct", Float),
    Column("video_count_now", Integer),
    Column("video_count_prev", Integer),
    Column("video_count_growth_pct", Float),
    Column("vph_now", Float),
    Column("vph_prev", Float),
    Column("vph_growth_pct", Float),
    Column("velocity", Float),
    Column("acceleration", Float),
    Column("prior_growth_pct", Float),  # growth over the preceding window (2w ago -> w ago)
    Column("growth_status", String(24), nullable=False),
    PrimaryKeyConstraint("region_code", "topic_id", "window", name="pk_gold_growth"),
)

gold_regional_comparison = Table(
    "gold_regional_comparison", metadata,
    Column("snapshot_run_id", String(36), nullable=False),
    Column("topic_id", String(64), nullable=False),
    Column("region_code", String(8), nullable=False),
    Column("popularity_score", Float, nullable=False),
    Column("popularity_rank", Integer, nullable=False),
    Column("regions_present", Integer, nullable=False),
    Column("is_shared", Boolean, nullable=False),
    Column("is_country_specific", Boolean, nullable=False),
    PrimaryKeyConstraint("topic_id", "region_code", name="pk_gold_regional"),
)

gold_channel_performance = Table(
    "gold_channel_performance", metadata,
    Column("snapshot_run_id", String(36), nullable=False),
    Column("channel_id", String(64), nullable=False),
    Column("channel_title", Text),
    Column("region_code", String(8), nullable=False),
    Column("appearances_24h", Integer, nullable=False),
    Column("videos_24h", Integer, nullable=False),
    Column("total_views", BigInteger),
    Column("avg_engagement_rate", Float),
    Column("views_per_hour", Float),
    Column("top_topic_id", String(64)),
    Column("top_topic_name", String(200)),
    Column("topics_count", Integer),
    Column("regions_count", Integer),
    ts("latest_observed_at"),
    PrimaryKeyConstraint("channel_id", "region_code", name="pk_gold_channel"),
)

gold_category_summary = Table(
    "gold_category_summary", metadata,
    Column("snapshot_run_id", String(36), nullable=False),
    Column("region_code", String(8), nullable=False),
    Column("category", String(64), nullable=False),
    Column("video_count", Integer, nullable=False),
    Column("topic_count", Integer, nullable=False),
    Column("total_views", BigInteger),
    Column("avg_engagement_rate", Float),
    PrimaryKeyConstraint("region_code", "category", name="pk_gold_category"),
)

SERVING_TABLES = [
    gold_trending_topics,
    gold_topic_growth,
    gold_regional_comparison,
    gold_channel_performance,
    gold_category_summary,
]
