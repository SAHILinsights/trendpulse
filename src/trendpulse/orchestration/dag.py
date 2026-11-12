"""Declarative description of the pipeline DAG.

The orchestrator (``pipeline.py``) executes stages in ``STAGES`` order; this module
documents *how they relate*: group, upstream dependencies, branch conditions and the
datasets each stage reads and writes. It is the single, tested source for the
control-plane DAG view and the dataset lineage graph — no relationship is drawn that
is not declared here (``tests/unit/test_dag.py`` keeps it in sync with ``STAGES``).

Dataset naming: warehouse tables use their table name; external systems use a
``source:`` / ``sink:`` prefix.
"""

from __future__ import annotations

from dataclasses import dataclass

GROUPS = ["Control", "Ingestion", "Bronze", "Silver", "Quality", "Gold", "Operations"]


@dataclass(frozen=True)
class StageSpec:
    name: str
    label: str
    group: str
    upstream: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    description: str
    condition: str = "on success of upstream"


STAGE_SPECS: tuple[StageSpec, ...] = (
    StageSpec("start_execution", "Start execution", "Control", (),
              (), ("pipeline_runs", "pipeline_lock"),
              "Allocate run ID, take the TTL lock, record the run as running, mark crashed runs abandoned."),
    StageSpec("load_configuration", "Load configuration", "Control", ("start_execution",),
              ("config:pipeline.yaml", "config:countries.yaml", "config:search_seeds.yaml", "config:taxonomy.yaml"),
              (), "Validated YAML + env overrides; enforces the data-source compliance switch."),
    StageSpec("check_quota_budget", "Check quota budget", "Control", ("load_configuration",),
              ("api_request_log",), (),
              "Seed the quota tracker with units used this Pacific quota day; skip when the budget is spent."),
    StageSpec("discover_regional_charts", "Regional charts", "Ingestion", ("check_quota_budget",),
              ("source:youtube.videos.list(chart=mostPopular)",), ("bronze_api_responses", "api_request_log"),
              "mostPopular per region × chart category; 1 unit per page; unsupported charts skipped."),
    StageSpec("discover_topic_search", "Topic search", "Ingestion", ("discover_regional_charts",),
              ("source:youtube.search.list", "source:youtube.videos.list(id)", "discovery_state"),
              ("bronze_api_responses", "tracked_videos", "discovery_state", "api_request_log"),
              "Rotating seed×region searches every search_interval_minutes, enriched with videos.list."),
    StageSpec("fetch_video_metrics", "Metric refresh", "Ingestion", ("discover_topic_search",),
              ("tracked_videos", "source:youtube.videos.list(id)"), ("bronze_api_responses", "api_request_log"),
              "Re-measure tracked search-discovered videos (50 ids per unit)."),
    StageSpec("save_bronze", "Bronze raw storage", "Bronze", ("fetch_video_metrics",),
              ("bronze_api_responses",), ("bronze_api_responses",),
              "Verify every fetched response was persisted (gzip JSON + SHA-256, written on arrival)."),
    StageSpec("validate_schema", "Schema validation", "Bronze", ("save_bronze",),
              ("bronze_api_responses",), ("dq_results",),
              "Profile payload structure: required paths and unexpected fields (schema_drift)."),
    StageSpec("transform_silver", "Silver transformation", "Silver", ("validate_schema",),
              ("bronze_api_responses",), ("silver_video_observations",),
              "Tolerant parse, UTC normalisation, 10 record-level checks, dedupe; rejects routed to quarantine."),
    StageSpec("extract_topics", "Topic extraction", "Silver", ("transform_silver",),
              ("silver_video_observations", "dim_topics", "topic_aliases"), (),
              "Taxonomy classification + TF-IDF keyphrase discovery with stable IDs (held in memory until Gold)."),
    StageSpec("data_quality", "Data-quality gate", "Quality", ("extract_topics",),
              ("silver_video_observations", "pipeline_runs"), ("dq_results",),
              "Dataset checks (stale, volume, topic coverage), audit rows, PASS/WARNING/FAIL gate."),
    StageSpec("quarantine_records", "Quarantine", "Quality", ("data_quality",),
              ("bronze_api_responses",), ("quarantine_records",),
              "Persist rejected rows with reasons and raw payload. Runs on gate PASS and on gate FAIL.",
              condition="always after the gate"),
    StageSpec("publish_gold", "Gold aggregation & publication", "Gold", ("quarantine_records",),
              ("silver_video_observations", "dim_videos", "dim_topics", "fact_topic_observations"),
              ("dim_regions", "dim_channels", "dim_videos", "dim_topics", "topic_aliases", "bridge_video_topics",
               "fact_video_observations", "fact_topic_observations", "gold_trending_topics", "gold_topic_growth",
               "gold_regional_comparison", "gold_channel_performance", "gold_category_summary",
               "gold_publications"),
              "One transaction: dims, append-only facts, serving tables, publication record.",
              condition="only if the quality gate passed"),
    StageSpec("generate_gold_aggregates", "Verify publication", "Gold", ("publish_gold",),
              ("gold_trending_topics", "gold_publications"), (),
              "Confirm serving tables reflect this run."),
    StageSpec("update_execution_metadata", "Execution metadata", "Operations", ("generate_gold_aggregates",),
              ("pipeline_stage_runs",), ("pipeline_runs",),
              "Final status and counts; retention/compaction on success.", condition="always"),
    StageSpec("publish_execution_status", "Status publication", "Operations", ("update_execution_metadata",),
              ("pipeline_runs",), ("sink:data/status/last_run.json", "sink:github.job_summary"),
              "Status file and GitHub Actions job summary.", condition="always"),
    StageSpec("send_notifications", "Notifications", "Operations", ("publish_execution_status",),
              ("pipeline_runs", "dq_results"), ("sink:webhook/email",),
              "Failure, recovery and optional DQ-warning alerts.", condition="always"),
)

SPEC_BY_NAME = {s.name: s for s in STAGE_SPECS}

# Edges that only exist on a failure path (drawn dashed in the UI).
FAILURE_EDGES: tuple[tuple[str, str], ...] = (("data_quality", "update_execution_metadata"),)


def edges() -> list[tuple[str, str, str]]:
    """(upstream, downstream, kind) where kind is 'flow' or 'failure'."""
    out = [(u, s.name, "flow") for s in STAGE_SPECS for u in s.upstream]
    out += [(a, b, "failure") for a, b in FAILURE_EDGES]
    return out


def neighbours(stage: str) -> tuple[list[str], list[str]]:
    ups = list(SPEC_BY_NAME[stage].upstream)
    downs = [s.name for s in STAGE_SPECS if stage in s.upstream]
    return ups, downs


def dataset_edges() -> list[tuple[str, str, str]]:
    """Dataset-level lineage (source dataset, target dataset, transforming stage).

    Derived only from the declared stage inputs/outputs. Operational bookkeeping datasets
    (locks, run tables, config files, sinks) are excluded so the graph shows data flow.
    """
    skip = {"pipeline_runs", "pipeline_lock", "api_request_log", "discovery_state", "pipeline_stage_runs",
            "gold_publications", "dq_results"}
    out = []
    for s in STAGE_SPECS:
        ins = [d for d in s.inputs if d not in skip and not d.startswith(("config:", "sink:"))]
        outs = [d for d in s.outputs if d not in skip and not d.startswith("sink:")]
        for i in ins:
            for o in outs:
                if i != o:
                    out.append((i, o, s.name))
    # topic extraction feeds Gold through an in-memory assignment set
    out += [("silver_video_observations", "bridge_video_topics", "extract_topics+publish_gold")]
    # Gold tables read by publish_gold for history (previous metrics, lookback windows) are
    # self-reads within the layer, not lineage of new data — kept out of the graph.
    gold_hist = {"dim_videos", "dim_topics", "fact_topic_observations"}
    out = [e for e in out if not (e[2] == "publish_gold" and e[0] in gold_hist)]
    return sorted(set(out))
