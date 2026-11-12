"""Generate docs/data_dictionary.md from the SQLAlchemy schema (single source of truth)."""

from __future__ import annotations

from pathlib import Path

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


def main() -> None:
    out = ["# Data dictionary", "", "Generated from `src/trendpulse/storage/schema.py` by "
           "`scripts/generate_data_dictionary.py`. All timestamps are `TIMESTAMP WITH TIME ZONE` in UTC. "
           "In PostgreSQL every table lives in schema `trendpulse`.", ""]
    order = sorted(s.metadata.tables.values(), key=lambda t: (
        ["Bronze", "Silver", "Gold"].index(LAYER[t.name].split(" ")[0]) if t.name in LAYER else 9, t.name))
    out.append("| Table | Layer | Purpose |\n|---|---|---|")
    for t in order:
        out.append(f"| [`{t.name}`](#{t.name}) | {LAYER.get(t.name, 'Operational')} | {TABLE_DOC.get(t.name, '')} |")
    for t in order:
        pk = [c.name for c in t.primary_key.columns]
        out += ["", f"## {t.name}", "", f"*{LAYER.get(t.name, 'Operational')}* — {TABLE_DOC.get(t.name, '')}", "",
                f"Primary key: `({', '.join(pk)})`", ""]
        uniq = [c for c in t.constraints if c.__class__.__name__ == "UniqueConstraint"]
        for u in uniq:
            out.append(f"Unique: `({', '.join(col.name for col in u.columns)})`  ")
        if t.indexes:
            out.append("Indexes: " + ", ".join(f"`{i.name}({', '.join(c.name for c in i.columns)})`"
                                                for i in sorted(t.indexes, key=lambda i: i.name)))
            out.append("")
        out.append("| Column | Type | Null | Description |\n|---|---|---|---|")
        for c in t.columns:
            out.append(f"| `{c.name}` | {str(c.type).replace('DATETIME', 'TIMESTAMPTZ')} | "
                       f"{'yes' if c.nullable else 'no'} | {COL_DOC.get(c.name, '')} |")
    Path(__file__).resolve().parents[1].joinpath("docs", "data_dictionary.md").write_text("\n".join(out) + "\n")
    print("wrote docs/data_dictionary.md")


if __name__ == "__main__":
    main()
