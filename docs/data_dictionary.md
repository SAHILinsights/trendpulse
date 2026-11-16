# Data dictionary

Generated from `src/trendpulse/storage/schema.py` by `scripts/generate_data_dictionary.py`. All timestamps are `TIMESTAMP WITH TIME ZONE` in UTC. In PostgreSQL every table lives in schema `trendpulse`.

| Table | Layer | Purpose |
|---|---|---|
| [`bronze_api_responses`](#bronze_api_responses) | Bronze | One row per API response, gzip-compressed raw JSON with lineage (endpoint, discovery method/query, region, category, page, request params, SHA-256). Replay source. |
| [`bridge_video_topics`](#bridge_video_topics) | Silver | Current topic assignments per video with confidence, method (taxonomy/discovered/fallback) and review status. |
| [`silver_video_observations`](#silver_video_observations) | Silver | Conformed, validated observation of a video in one discovery context of one run. Slim by design (metadata lives in dim_videos). |
| [`topic_aliases`](#topic_aliases) | Silver | Phrase variants mapped to a stable discovered-topic ID. |
| [`dim_channels`](#dim_channels) | Gold · dimension | Channels ever observed. |
| [`dim_regions`](#dim_regions) | Gold · dimension | Monitored countries (from config/countries.yaml). |
| [`dim_topics`](#dim_topics) | Gold · dimension | Taxonomy, discovered and unclassified topics with stable IDs. |
| [`dim_videos`](#dim_videos) | Gold · dimension | Latest metadata and lifetime metrics per video; first_observed_at is insert-only. |
| [`fact_topic_observations`](#fact_topic_observations) | Gold · fact (append-only) | Historical topic metrics per run and region (plus GLOBAL). The core time-series asset. |
| [`fact_video_observations`](#fact_video_observations) | Gold · fact (append-only) | Historical metric snapshots per video per run and discovery context, with growth since the video's previous observation. |
| [`gold_category_summary`](#gold_category_summary) | Gold · serving | Videos, topics, views and engagement per TrendPulse category and region. |
| [`gold_channel_performance`](#gold_channel_performance) | Gold · serving | Channel activity over the last 24 h, per region and GLOBAL. |
| [`gold_regional_comparison`](#gold_regional_comparison) | Gold · serving | Topic presence across regions (shared vs country-specific). |
| [`gold_topic_growth`](#gold_topic_growth) | Gold · serving | Growth, velocity and acceleration per topic, region and comparison window for the latest snapshot. |
| [`gold_trending_topics`](#gold_trending_topics) | Gold · serving | Latest published ranking per region with lifecycle stage and rank changes. |
| [`api_request_log`](#api_request_log) | Operational | Every API attempt (key never stored): endpoint, params, status, quota cost, latency, error. |
| [`discovery_state`](#discovery_state) | Operational | Small key/value state (search rotation cursor, last search time). |
| [`dq_results`](#dq_results) | Operational | Data-quality audit: one row per check per run. |
| [`gold_publications`](#gold_publications) | Operational | Log of published Gold snapshots. |
| [`pipeline_lock`](#pipeline_lock) | Operational | TTL lock preventing overlapping executions. |
| [`pipeline_log_records`](#pipeline_log_records) | Operational | Run-scoped structured log records (INFO+), searchable in the control plane. |
| [`pipeline_runs`](#pipeline_runs) | Operational | One row per execution: status, trigger, data source, counts, quota, DQ status, error. |
| [`pipeline_stage_runs`](#pipeline_stage_runs) | Operational | Per-stage timing, status, attempts and record counts. |
| [`quarantine_records`](#quarantine_records) | Operational | Rejected records with reasons and raw payload. |
| [`schema_migrations`](#schema_migrations) | Operational | Applied migration versions. |
| [`tracked_videos`](#tracked_videos) | Operational | Search-discovered videos re-measured each cycle until track_until. |

## bronze_api_responses

*Bronze* — One row per API response, gzip-compressed raw JSON with lineage (endpoint, discovery method/query, region, category, page, request params, SHA-256). Replay source.

Primary key: `(response_id)`

Indexes: `ix_bronze_fetched(fetched_at)`, `ix_bronze_run(run_id)`

| Column | Type | Null | Description |
|---|---|---|---|
| `response_id` | VARCHAR(36) | no |  |
| `run_id` | VARCHAR(36) | no | Pipeline execution UUID |
| `endpoint` | VARCHAR(48) | no |  |
| `discovery_method` | VARCHAR(24) | no | regional_chart | search (Silver/Gold); Bronze also search_videos | search_refresh |
| `discovery_query` | VARCHAR(64) | no | chart:<all|categoryId> or search seed id |
| `region_code` | VARCHAR(8) | yes | ISO-3166 alpha-2, or GLOBAL |
| `category_id` | INTEGER | yes |  |
| `page_index` | INTEGER | no |  |
| `request_params` | TEXT | no |  |
| `http_status` | INTEGER | no |  |
| `item_count` | INTEGER | no |  |
| `payload_gz` | BLOB | no | gzip(JSON) raw API response |
| `payload_sha256` | VARCHAR(64) | no |  |
| `fetched_at` | TIMESTAMPTZ | no |  |

## bridge_video_topics

*Silver* — Current topic assignments per video with confidence, method (taxonomy/discovered/fallback) and review status.

Primary key: `(video_id, topic_id)`

Indexes: `ix_bridge_review(review_status)`, `ix_bridge_topic(topic_id)`

| Column | Type | Null | Description |
|---|---|---|---|
| `video_id` | VARCHAR(32) | no |  |
| `topic_id` | VARCHAR(64) | no |  |
| `confidence` | FLOAT | no | Assignment confidence 0–1 |
| `assignment_method` | VARCHAR(24) | no |  |
| `review_status` | VARCHAR(16) | no | auto | needs_review | unclassified |
| `matched_terms` | TEXT | yes |  |
| `assigned_run_id` | VARCHAR(36) | no |  |
| `updated_at` | TIMESTAMPTZ | no |  |

## silver_video_observations

*Silver* — Conformed, validated observation of a video in one discovery context of one run. Slim by design (metadata lives in dim_videos).

Primary key: `(run_id, video_id, region_code, discovery_method, discovery_query)`

Indexes: `ix_silver_observed(observed_at)`

| Column | Type | Null | Description |
|---|---|---|---|
| `run_id` | VARCHAR(36) | no | Pipeline execution UUID |
| `video_id` | VARCHAR(32) | no |  |
| `region_code` | VARCHAR(8) | no | ISO-3166 alpha-2, or GLOBAL |
| `discovery_method` | VARCHAR(24) | no | regional_chart | search (Silver/Gold); Bronze also search_videos | search_refresh |
| `discovery_query` | VARCHAR(64) | no | chart:<all|categoryId> or search seed id |
| `chart_category_id` | INTEGER | yes |  |
| `chart_rank` | INTEGER | yes | Position in the regional chart (1-based) |
| `observed_at` | TIMESTAMPTZ | no | Snapshot time (UTC) = run start |
| `channel_id` | VARCHAR(64) | no |  |
| `category_id` | INTEGER | yes |  |
| `published_at` | TIMESTAMPTZ | yes |  |
| `duration_seconds` | INTEGER | yes |  |
| `view_count` | BIGINT | yes |  |
| `like_count` | BIGINT | yes |  |
| `comment_count` | BIGINT | yes |  |
| `metadata_hash` | VARCHAR(16) | yes |  |

## topic_aliases

*Silver* — Phrase variants mapped to a stable discovered-topic ID.

Primary key: `(alias_key)`

Indexes: `ix_alias_topic(topic_id)`

| Column | Type | Null | Description |
|---|---|---|---|
| `alias_key` | VARCHAR(200) | no |  |
| `topic_id` | VARCHAR(64) | no |  |
| `surface_form` | TEXT | yes |  |
| `created_at` | TIMESTAMPTZ | no |  |

## dim_channels

*Gold · dimension* — Channels ever observed.

Primary key: `(channel_id)`

| Column | Type | Null | Description |
|---|---|---|---|
| `channel_id` | VARCHAR(64) | no |  |
| `channel_title` | TEXT | yes |  |
| `first_observed_at` | TIMESTAMPTZ | no |  |
| `last_observed_at` | TIMESTAMPTZ | no |  |

## dim_regions

*Gold · dimension* — Monitored countries (from config/countries.yaml).

Primary key: `(region_code)`

| Column | Type | Null | Description |
|---|---|---|---|
| `region_code` | VARCHAR(8) | no | ISO-3166 alpha-2, or GLOBAL |
| `country_name` | VARCHAR(100) | no |  |
| `is_active` | BOOLEAN | no |  |
| `first_ingested_at` | TIMESTAMPTZ | yes |  |
| `last_ingested_at` | TIMESTAMPTZ | yes |  |

## dim_topics

*Gold · dimension* — Taxonomy, discovered and unclassified topics with stable IDs.

Primary key: `(topic_id)`

Indexes: `ix_dim_topics_kind(topic_kind)`

| Column | Type | Null | Description |
|---|---|---|---|
| `topic_id` | VARCHAR(64) | no |  |
| `canonical_name` | VARCHAR(200) | no |  |
| `canonical_key` | VARCHAR(200) | no |  |
| `topic_kind` | VARCHAR(16) | no | taxonomy | discovered | unclassified |
| `category` | VARCHAR(64) | no |  |
| `parent_topic_id` | VARCHAR(64) | yes |  |
| `keywords` | TEXT | yes |  |
| `first_observed_at` | TIMESTAMPTZ | yes |  |
| `last_observed_at` | TIMESTAMPTZ | yes |  |
| `created_run_id` | VARCHAR(36) | yes |  |
| `is_active` | BOOLEAN | no |  |

## dim_videos

*Gold · dimension* — Latest metadata and lifetime metrics per video; first_observed_at is insert-only.

Primary key: `(video_id)`

Indexes: `ix_dim_videos_channel(channel_id)`, `ix_dim_videos_last_obs(last_observed_at)`

| Column | Type | Null | Description |
|---|---|---|---|
| `video_id` | VARCHAR(32) | no |  |
| `title` | TEXT | no |  |
| `channel_id` | VARCHAR(64) | no |  |
| `category_id` | INTEGER | yes |  |
| `category_name` | VARCHAR(64) | yes |  |
| `published_at` | TIMESTAMPTZ | yes |  |
| `duration_seconds` | INTEGER | yes |  |
| `tags` | TEXT | yes |  |
| `description_snippet` | TEXT | yes |  |
| `thumbnail_url` | TEXT | yes |  |
| `default_language` | VARCHAR(16) | yes |  |
| `first_observed_at` | TIMESTAMPTZ | no |  |
| `last_observed_at` | TIMESTAMPTZ | no |  |
| `latest_view_count` | BIGINT | yes |  |
| `latest_like_count` | BIGINT | yes |  |
| `latest_comment_count` | BIGINT | yes |  |

## fact_topic_observations

*Gold · fact (append-only)* — Historical topic metrics per run and region (plus GLOBAL). The core time-series asset.

Primary key: `(run_id, topic_id, region_code)`

Indexes: `ix_fto_observed(observed_at)`, `ix_fto_region_time(region_code, observed_at)`, `ix_fto_topic_region_time(topic_id, region_code, observed_at)`

| Column | Type | Null | Description |
|---|---|---|---|
| `run_id` | VARCHAR(36) | no | Pipeline execution UUID |
| `observed_at` | TIMESTAMPTZ | no | Snapshot time (UTC) = run start |
| `topic_id` | VARCHAR(64) | no |  |
| `region_code` | VARCHAR(8) | no | ISO-3166 alpha-2, or GLOBAL |
| `video_count` | INTEGER | no |  |
| `channel_count` | INTEGER | no |  |
| `total_views` | BIGINT | yes | Sum of LIFETIME view counts (size, not growth) |
| `avg_views` | FLOAT | yes |  |
| `total_likes` | BIGINT | yes |  |
| `total_comments` | BIGINT | yes |  |
| `avg_engagement_rate` | FLOAT | yes |  |
| `views_gained` | BIGINT | yes | Sum of positive per-video view deltas since previous observation |
| `views_per_hour` | FLOAT | yes | views_delta / hours_since_prev; NULL on first observation or negative delta |
| `popularity_score` | FLOAT | no | 20·ln(1+videos)+10·ln(1+channels)+8·log10(1+views/h)+engagement% |
| `popularity_rank` | INTEGER | no | Rank by score within region (0 = not ranked, e.g. Unclassified) |

## fact_video_observations

*Gold · fact (append-only)* — Historical metric snapshots per video per run and discovery context, with growth since the video's previous observation.

Primary key: `(run_id, video_id, region_code, discovery_method, discovery_query)`

Indexes: `ix_fvo_observed(observed_at)`, `ix_fvo_region_time(region_code, observed_at)`, `ix_fvo_video_time(video_id, observed_at)`

| Column | Type | Null | Description |
|---|---|---|---|
| `run_id` | VARCHAR(36) | no | Pipeline execution UUID |
| `observed_at` | TIMESTAMPTZ | no | Snapshot time (UTC) = run start |
| `video_id` | VARCHAR(32) | no |  |
| `region_code` | VARCHAR(8) | no | ISO-3166 alpha-2, or GLOBAL |
| `discovery_method` | VARCHAR(24) | no | regional_chart | search (Silver/Gold); Bronze also search_videos | search_refresh |
| `discovery_query` | VARCHAR(64) | no | chart:<all|categoryId> or search seed id |
| `chart_category_id` | INTEGER | yes |  |
| `chart_rank` | INTEGER | yes | Position in the regional chart (1-based) |
| `view_count` | BIGINT | yes |  |
| `like_count` | BIGINT | yes |  |
| `comment_count` | BIGINT | yes |  |
| `engagement_rate` | FLOAT | yes | (likes + comments) / views |
| `views_delta` | BIGINT | yes | Views since this video's previous observation (may be negative after YouTube count corrections) |
| `hours_since_prev` | FLOAT | yes |  |
| `views_per_hour` | FLOAT | yes | views_delta / hours_since_prev; NULL on first observation or negative delta |

## gold_category_summary

*Gold · serving* — Videos, topics, views and engagement per TrendPulse category and region.

Primary key: `(region_code, category)`

| Column | Type | Null | Description |
|---|---|---|---|
| `snapshot_run_id` | VARCHAR(36) | no |  |
| `region_code` | VARCHAR(8) | no | ISO-3166 alpha-2, or GLOBAL |
| `category` | VARCHAR(64) | no |  |
| `video_count` | INTEGER | no |  |
| `topic_count` | INTEGER | no |  |
| `total_views` | BIGINT | yes | Sum of LIFETIME view counts (size, not growth) |
| `avg_engagement_rate` | FLOAT | yes |  |

## gold_channel_performance

*Gold · serving* — Channel activity over the last 24 h, per region and GLOBAL.

Primary key: `(channel_id, region_code)`

| Column | Type | Null | Description |
|---|---|---|---|
| `snapshot_run_id` | VARCHAR(36) | no |  |
| `channel_id` | VARCHAR(64) | no |  |
| `channel_title` | TEXT | yes |  |
| `region_code` | VARCHAR(8) | no | ISO-3166 alpha-2, or GLOBAL |
| `appearances_24h` | INTEGER | no |  |
| `videos_24h` | INTEGER | no |  |
| `total_views` | BIGINT | yes | Sum of LIFETIME view counts (size, not growth) |
| `avg_engagement_rate` | FLOAT | yes |  |
| `views_per_hour` | FLOAT | yes | views_delta / hours_since_prev; NULL on first observation or negative delta |
| `top_topic_id` | VARCHAR(64) | yes |  |
| `top_topic_name` | VARCHAR(200) | yes |  |
| `topics_count` | INTEGER | yes |  |
| `regions_count` | INTEGER | yes |  |
| `latest_observed_at` | TIMESTAMPTZ | yes |  |

## gold_regional_comparison

*Gold · serving* — Topic presence across regions (shared vs country-specific).

Primary key: `(topic_id, region_code)`

| Column | Type | Null | Description |
|---|---|---|---|
| `snapshot_run_id` | VARCHAR(36) | no |  |
| `topic_id` | VARCHAR(64) | no |  |
| `region_code` | VARCHAR(8) | no | ISO-3166 alpha-2, or GLOBAL |
| `popularity_score` | FLOAT | no | 20·ln(1+videos)+10·ln(1+channels)+8·log10(1+views/h)+engagement% |
| `popularity_rank` | INTEGER | no | Rank by score within region (0 = not ranked, e.g. Unclassified) |
| `regions_present` | INTEGER | no |  |
| `is_shared` | BOOLEAN | no |  |
| `is_country_specific` | BOOLEAN | no |  |

## gold_topic_growth

*Gold · serving* — Growth, velocity and acceleration per topic, region and comparison window for the latest snapshot.

Primary key: `(region_code, topic_id, window)`

| Column | Type | Null | Description |
|---|---|---|---|
| `snapshot_run_id` | VARCHAR(36) | no |  |
| `snapshot_at` | TIMESTAMPTZ | no |  |
| `region_code` | VARCHAR(8) | no | ISO-3166 alpha-2, or GLOBAL |
| `topic_id` | VARCHAR(64) | no |  |
| `window` | VARCHAR(8) | no |  |
| `window_minutes` | INTEGER | no |  |
| `compare_run_id` | VARCHAR(36) | yes |  |
| `compare_observed_at` | TIMESTAMPTZ | yes |  |
| `score_now` | FLOAT | yes |  |
| `score_prev` | FLOAT | yes |  |
| `score_growth_pct` | FLOAT | yes |  |
| `video_count_now` | INTEGER | yes |  |
| `video_count_prev` | INTEGER | yes |  |
| `video_count_growth_pct` | FLOAT | yes |  |
| `vph_now` | FLOAT | yes |  |
| `vph_prev` | FLOAT | yes |  |
| `vph_growth_pct` | FLOAT | yes |  |
| `velocity` | FLOAT | yes | Score points per hour vs comparison snapshot |
| `acceleration` | FLOAT | yes | Change in velocity per hour (points/hour²) |
| `prior_growth_pct` | FLOAT | yes | Growth over the preceding window |
| `growth_status` | VARCHAR(24) | no | no_history|new|rising|stable|falling|vanished |

## gold_trending_topics

*Gold · serving* — Latest published ranking per region with lifecycle stage and rank changes.

Primary key: `(region_code, topic_id)`

| Column | Type | Null | Description |
|---|---|---|---|
| `snapshot_run_id` | VARCHAR(36) | no |  |
| `snapshot_at` | TIMESTAMPTZ | no |  |
| `region_code` | VARCHAR(8) | no | ISO-3166 alpha-2, or GLOBAL |
| `topic_id` | VARCHAR(64) | no |  |
| `topic_name` | VARCHAR(200) | no |  |
| `category` | VARCHAR(64) | no |  |
| `topic_kind` | VARCHAR(16) | no | taxonomy | discovered | unclassified |
| `popularity_rank` | INTEGER | no | Rank by score within region (0 = not ranked, e.g. Unclassified) |
| `rank_change_1h` | INTEGER | yes | Previous rank − current rank (positive = up) |
| `rank_change_24h` | INTEGER | yes |  |
| `popularity_score` | FLOAT | no | 20·ln(1+videos)+10·ln(1+channels)+8·log10(1+views/h)+engagement% |
| `video_count` | INTEGER | no |  |
| `channel_count` | INTEGER | no |  |
| `total_views` | BIGINT | yes | Sum of LIFETIME view counts (size, not growth) |
| `views_per_hour` | FLOAT | yes | views_delta / hours_since_prev; NULL on first observation or negative delta |
| `avg_engagement_rate` | FLOAT | yes |  |
| `growth_24h_pct` | FLOAT | yes |  |
| `lifecycle_stage` | VARCHAR(16) | no | baseline|emerging|growing|peaking|stable|declining|dormant |
| `first_observed_at` | TIMESTAMPTZ | yes |  |
| `last_observed_at` | TIMESTAMPTZ | yes |  |
| `consecutive_observations` | INTEGER | no | Consecutive published snapshots in which the topic was ranked |

## api_request_log

*Operational* — Every API attempt (key never stored): endpoint, params, status, quota cost, latency, error.

Primary key: `(id)`

Indexes: `ix_api_log_requested(requested_at)`, `ix_api_log_run(run_id)`

| Column | Type | Null | Description |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `run_id` | VARCHAR(36) | yes | Pipeline execution UUID |
| `endpoint` | VARCHAR(48) | no |  |
| `region_code` | VARCHAR(8) | yes | ISO-3166 alpha-2, or GLOBAL |
| `discovery_method` | VARCHAR(24) | yes | regional_chart | search (Silver/Gold); Bronze also search_videos | search_refresh |
| `params_json` | TEXT | yes |  |
| `http_status` | INTEGER | yes |  |
| `quota_cost` | INTEGER | no | YouTube quota units billed for this attempt |
| `items_returned` | INTEGER | yes |  |
| `duration_ms` | INTEGER | yes |  |
| `attempt` | INTEGER | yes |  |
| `requested_at` | TIMESTAMPTZ | no |  |
| `error` | TEXT | yes |  |

## discovery_state

*Operational* — Small key/value state (search rotation cursor, last search time).

Primary key: `(key)`

| Column | Type | Null | Description |
|---|---|---|---|
| `key` | VARCHAR(64) | no |  |
| `value` | TEXT | no |  |
| `updated_at` | TIMESTAMPTZ | no |  |

## dq_results

*Operational* — Data-quality audit: one row per check per run.

Primary key: `(id)`

Unique: `(run_id, check_name)`  
Indexes: `ix_dq_executed(executed_at)`

| Column | Type | Null | Description |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `run_id` | VARCHAR(36) | no | Pipeline execution UUID |
| `check_name` | VARCHAR(64) | no |  |
| `severity` | VARCHAR(16) | no |  |
| `status` | VARCHAR(16) | no |  |
| `total_checked` | INTEGER | no |  |
| `passed` | INTEGER | no |  |
| `failed` | INTEGER | no |  |
| `warnings` | INTEGER | no |  |
| `details` | TEXT | yes |  |
| `executed_at` | TIMESTAMPTZ | no |  |

## gold_publications

*Operational* — Log of published Gold snapshots.

Primary key: `(snapshot_run_id)`

Indexes: `ix_gold_pub_at(published_at)`

| Column | Type | Null | Description |
|---|---|---|---|
| `snapshot_run_id` | VARCHAR(36) | no |  |
| `snapshot_at` | TIMESTAMPTZ | no |  |
| `published_at` | TIMESTAMPTZ | no |  |
| `data_source` | VARCHAR(32) | no |  |
| `row_counts` | TEXT | yes |  |

## pipeline_lock

*Operational* — TTL lock preventing overlapping executions.

Primary key: `(lock_name)`

| Column | Type | Null | Description |
|---|---|---|---|
| `lock_name` | VARCHAR(64) | no |  |
| `owner_run_id` | VARCHAR(36) | no |  |
| `acquired_at` | TIMESTAMPTZ | no |  |
| `expires_at` | TIMESTAMPTZ | no |  |

## pipeline_log_records

*Operational* — Run-scoped structured log records (INFO+), searchable in the control plane.

Primary key: `(id)`

Indexes: `ix_logs_level(level)`, `ix_logs_logged_at(logged_at)`, `ix_logs_run(run_id, logged_at)`

| Column | Type | Null | Description |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `run_id` | VARCHAR(36) | no | Pipeline execution UUID |
| `stage` | VARCHAR(48) | yes |  |
| `level` | VARCHAR(10) | no |  |
| `logger` | VARCHAR(80) | no |  |
| `message` | TEXT | no |  |
| `exc_text` | TEXT | yes |  |
| `logged_at` | TIMESTAMPTZ | no |  |

## pipeline_runs

*Operational* — One row per execution: status, trigger, data source, counts, quota, DQ status, error.

Primary key: `(run_id)`

Indexes: `ix_pipeline_runs_started(started_at)`, `ix_pipeline_runs_status(status, started_at)`

| Column | Type | Null | Description |
|---|---|---|---|
| `run_id` | VARCHAR(36) | no | Pipeline execution UUID |
| `trigger` | VARCHAR(32) | no |  |
| `data_source` | VARCHAR(32) | no |  |
| `status` | VARCHAR(24) | no |  |
| `started_at` | TIMESTAMPTZ | no |  |
| `finished_at` | TIMESTAMPTZ | yes |  |
| `duration_seconds` | FLOAT | yes |  |
| `records_bronze` | INTEGER | yes |  |
| `records_silver` | INTEGER | yes |  |
| `records_quarantined` | INTEGER | yes |  |
| `records_gold` | INTEGER | yes |  |
| `videos_observed` | INTEGER | yes |  |
| `topics_observed` | INTEGER | yes |  |
| `quota_used` | INTEGER | yes |  |
| `gold_published` | BOOLEAN | no |  |
| `dq_status` | VARCHAR(16) | yes |  |
| `error_message` | TEXT | yes |  |
| `git_sha` | VARCHAR(64) | yes |  |
| `stage_summary` | TEXT | yes |  |

## pipeline_stage_runs

*Operational* — Per-stage timing, status, attempts and record counts.

Primary key: `(id)`

Unique: `(run_id, stage)`  
Indexes: `ix_stage_runs_run(run_id)`

| Column | Type | Null | Description |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `run_id` | VARCHAR(36) | no | Pipeline execution UUID |
| `stage` | VARCHAR(48) | no |  |
| `stage_order` | INTEGER | no |  |
| `status` | VARCHAR(16) | no |  |
| `started_at` | TIMESTAMPTZ | no |  |
| `finished_at` | TIMESTAMPTZ | yes |  |
| `duration_ms` | INTEGER | yes |  |
| `records_in` | INTEGER | yes |  |
| `records_out` | INTEGER | yes |  |
| `attempts` | INTEGER | yes |  |
| `error` | TEXT | yes |  |

## quarantine_records

*Operational* — Rejected records with reasons and raw payload.

Primary key: `(id)`

Indexes: `ix_quarantine_at(quarantined_at)`, `ix_quarantine_run(run_id)`

| Column | Type | Null | Description |
|---|---|---|---|
| `id` | INTEGER | no |  |
| `run_id` | VARCHAR(36) | no | Pipeline execution UUID |
| `stage` | VARCHAR(32) | no |  |
| `check_name` | VARCHAR(64) | no |  |
| `record_key` | VARCHAR(200) | yes |  |
| `reason` | TEXT | no |  |
| `payload` | TEXT | yes |  |
| `quarantined_at` | TIMESTAMPTZ | no |  |

## schema_migrations

*Operational* — Applied migration versions.

Primary key: `(version)`

| Column | Type | Null | Description |
|---|---|---|---|
| `version` | INTEGER | no |  |
| `description` | VARCHAR(200) | no |  |
| `applied_at` | TIMESTAMPTZ | no |  |

## tracked_videos

*Operational* — Search-discovered videos re-measured each cycle until track_until.

Primary key: `(video_id, region_code, discovery_query)`

Indexes: `ix_tracked_until(track_until)`

| Column | Type | Null | Description |
|---|---|---|---|
| `video_id` | VARCHAR(32) | no |  |
| `region_code` | VARCHAR(8) | no | ISO-3166 alpha-2, or GLOBAL |
| `discovery_query` | VARCHAR(64) | no | chart:<all|categoryId> or search seed id |
| `first_discovered_at` | TIMESTAMPTZ | no |  |
| `last_discovered_at` | TIMESTAMPTZ | no |  |
| `track_until` | TIMESTAMPTZ | no |  |
