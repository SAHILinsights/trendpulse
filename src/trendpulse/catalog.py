"""Dataset catalog metadata shared by the data dictionary generator and the control plane.

Purely descriptive: layer, purpose, column notes, freshness column and logical partitioning
for every warehouse table. Structure (columns, types, keys) always comes from
``storage.schema`` so the two can never drift.
"""

from __future__ import annotations

from trendpulse.storage import schema as s

LAYER = {
    "bronze_api_responses": "Bronze", "silver_video_observations": "Silver", "bridge_video_topics": "Silver",
    "topic_aliases": "Silver", "dim_regions": "Gold · dimension", "dim_channels": "Gold · dimension",
    "dim_videos": "Gold · dimension", "dim_topics": "Gold · dimension",
    "fact_video_observations": "Gold · fact (append-only)", "fact_topic_observations": "Gold · fact (append-only)",
    "gold_trending_topics": "Gold · serving", "gold_topic_growth": "Gold · serving",
    "gold_regional_comparison": "Gold · serving", "gold_channel_performance": "Gold · serving",
    "gold_category_summary": "Gold · serving",
}
TABLE_DOC = {
    "bronze_api_responses": "One row per API response, gzip-compressed raw JSON with lineage (endpoint, discovery method/query, region, category, page, request params, SHA-256). Replay source.",
    "silver_video_observations": "Conformed, validated observation of a video in one discovery context of one run. Slim by design (metadata lives in dim_videos).",
    "bridge_video_topics": "Current topic assignments per video with confidence, method (taxonomy/discovered/fallback) and review status.",
    "topic_aliases": "Phrase variants mapped to a stable discovered-topic ID.",
    "dim_regions": "Monitored countries (from config/countries.yaml).",
    "dim_channels": "Channels ever observed.",
    "dim_videos": "Latest metadata and lifetime metrics per video; first_observed_at is insert-only.",
    "dim_topics": "Taxonomy, discovered and unclassified topics with stable IDs.",
    "fact_video_observations": "Historical metric snapshots per video per run and discovery context, with growth since the video's previous observation.",
    "fact_topic_observations": "Historical topic metrics per run and region (plus GLOBAL). The core time-series asset.",
    "gold_trending_topics": "Latest published ranking per region with lifecycle stage and rank changes.",
    "gold_topic_growth": "Growth, velocity and acceleration per topic, region and comparison window for the latest snapshot.",
    "gold_regional_comparison": "Topic presence across regions (shared vs country-specific).",
    "gold_channel_performance": "Channel activity over the last 24 h, per region and GLOBAL.",
    "gold_category_summary": "Videos, topics, views and engagement per TrendPulse category and region.",
    "pipeline_runs": "One row per execution: status, trigger, data source, counts, quota, DQ status, error.",
    "pipeline_stage_runs": "Per-stage timing, status, attempts and record counts.",
    "pipeline_log_records": "Run-scoped structured log records (INFO+), searchable in the control plane.",
    "pipeline_lock": "TTL lock preventing overlapping executions.",
    "api_request_log": "Every API attempt (key never stored): endpoint, params, status, quota cost, latency, error.",
    "discovery_state": "Small key/value state (search rotation cursor, last search time).",
    "tracked_videos": "Search-discovered videos re-measured each cycle until track_until.",
    "dq_results": "Data-quality audit: one row per check per run.",
    "quarantine_records": "Rejected records with reasons and raw payload.",
    "gold_publications": "Log of published Gold snapshots.",
    "schema_migrations": "Applied migration versions.",
}
COL_DOC = {
    "run_id": "Pipeline execution UUID", "observed_at": "Snapshot time (UTC) = run start",
    "discovery_method": "regional_chart | search (Silver/Gold); Bronze also search_videos | search_refresh",
    "discovery_query": "chart:<all|categoryId> or search seed id", "chart_rank": "Position in the regional chart (1-based)",
    "views_delta": "Views since this video's previous observation (may be negative after YouTube count corrections)",
    "views_per_hour": "views_delta / hours_since_prev; NULL on first observation or negative delta",
    "engagement_rate": "(likes + comments) / views", "total_views": "Sum of LIFETIME view counts (size, not growth)",
    "views_gained": "Sum of positive per-video view deltas since previous observation",
    "popularity_score": "20·ln(1+videos)+10·ln(1+channels)+8·log10(1+views/h)+engagement%",
    "popularity_rank": "Rank by score within region (0 = not ranked, e.g. Unclassified)",
    "region_code": "ISO-3166 alpha-2, or GLOBAL", "lifecycle_stage": "baseline|emerging|growing|peaking|stable|declining|dormant",
    "growth_status": "no_history|new|rising|stable|falling|vanished", "velocity": "Score points per hour vs comparison snapshot",
    "acceleration": "Change in velocity per hour (points/hour²)", "prior_growth_pct": "Growth over the preceding window",
    "confidence": "Assignment confidence 0–1", "review_status": "auto | needs_review | unclassified",
    "topic_kind": "taxonomy | discovered | unclassified", "payload_gz": "gzip(JSON) raw API response",
    "quota_cost": "YouTube quota units billed for this attempt", "rank_change_1h": "Previous rank − current rank (positive = up)",
    "consecutive_observations": "Consecutive published snapshots in which the topic was ranked",
}



# Column used as "last updated" for each table.
FRESHNESS_COLUMN = {
    "bronze_api_responses": "fetched_at", "silver_video_observations": "observed_at",
    "bridge_video_topics": "updated_at", "topic_aliases": "created_at", "dim_regions": "last_ingested_at",
    "dim_channels": "last_observed_at", "dim_videos": "last_observed_at", "dim_topics": "last_observed_at",
    "fact_video_observations": "observed_at", "fact_topic_observations": "observed_at",
    "gold_trending_topics": "snapshot_at", "gold_topic_growth": "snapshot_at", "pipeline_runs": "started_at",
    "pipeline_stage_runs": "finished_at", "pipeline_lock": "acquired_at", "api_request_log": "requested_at",
    "discovery_state": "updated_at", "tracked_videos": "last_discovered_at", "dq_results": "executed_at",
    "quarantine_records": "quarantined_at", "gold_publications": "published_at",
    "schema_migrations": "applied_at", "pipeline_log_records": "logged_at",
}

# Logical partitioning (PostgreSQL tables are not physically partitioned; these are the
# keys every write and retention job is organised by). Lake partitions apply to local runs.
PARTITIONING = {
    "bronze_api_responses": "run_id · fetched_at (lake: bronze/dt=YYYY-MM-DD/run=<id>/<response_id>.json.gz)",
    "silver_video_observations": "run_id · observed_at (lake: silver/dt=YYYY-MM-DD/run=<id>.parquet)",
    "fact_video_observations": "run_id · observed_at (compacted to 1 run/day after 1 day)",
    "fact_topic_observations": "run_id · observed_at (hourly after 3 days, daily after 14)",
    "gold_trending_topics": "single published snapshot (snapshot_run_id)",
    "gold_topic_growth": "single published snapshot × window",
    "gold_regional_comparison": "single published snapshot", "gold_channel_performance": "single published snapshot",
    "gold_category_summary": "single published snapshot", "dq_results": "run_id",
    "quarantine_records": "run_id", "pipeline_log_records": "run_id · logged_at", "api_request_log": "requested_at",
}

# Columns never exposed by the control plane (raw payloads, large blobs).
HIDDEN_COLUMNS = {"bronze_api_responses": {"payload_gz"}}

SKIP_ROWCOUNT: set[str] = set()


def layer_of(table: str) -> str:
    return LAYER.get(table, "Operational")


def layer_key(table: str) -> str:
    return layer_of(table).split(" ")[0]


def tables() -> list:
    return list(s.metadata.sorted_tables)
