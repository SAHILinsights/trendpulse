"""TrendPulse command-line interface.

    trendpulse run [--sample] [--trigger schedule|manual] [--force-search]
    trendpulse simulate --cycles 96 --interval 30      # SAMPLE data, local only
    trendpulse replay --run-id <uuid>
    trendpulse migrate | validate-config | status | retention | quota-plan
    trendpulse export-parquet [--out data/lake/gold]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trendpulse.logging_utils import configure_logging, get_logger
from trendpulse.settings import load_settings

log = get_logger("cli")

EXIT_OK, EXIT_FAILED, EXIT_DQ, EXIT_USAGE = 0, 1, 2, 64


def _warehouse(settings, read_only: bool = False):
    from trendpulse.storage.db import Warehouse

    return Warehouse(settings.secrets.database_url, data_dir=settings.data_dir, read_only=read_only)


def _sample_guard(wh, allow: bool) -> None:
    if wh.is_postgres and not allow:
        raise SystemExit("refusing to write SAMPLE data into a PostgreSQL warehouse "
                         "(pass --allow-sample-on-postgres if this is a disposable database)")


def cmd_run(args, settings) -> int:
    from trendpulse.ingestion.sample import SampleYouTubeSession
    from trendpulse.orchestration.pipeline import Pipeline

    wh = _warehouse(settings)
    lake = settings.data_dir / "lake"
    factory = None
    source = "youtube_api"
    if args.sample:
        _sample_guard(wh, args.allow_sample_on_postgres)
        source = "sample_fixture"
        factory = lambda st: SampleYouTubeSession(lambda: st.now, epoch=st.now)  # noqa: E731
    res = Pipeline(settings, wh, session_factory=factory, lake_dir=lake).run(
        trigger=args.trigger, data_source=source, force_search=args.force_search)
    print(json.dumps({"run_id": res.run_id, "status": res.status, "gold_published": res.gold_published,
                      "dq_status": res.dq_status, "counts": res.counts, "error": res.error}, indent=2, default=str))
    if res.status in {"success", "partial", "skipped", "skipped_locked"}:
        return EXIT_OK
    return EXIT_DQ if res.status == "dq_failed" else EXIT_FAILED


def cmd_simulate(args, settings) -> int:
    """Backfill N cycles of SAMPLE data on a simulated clock (local demo / load test)."""
    from trendpulse.ingestion.sample import SampleYouTubeSession
    from trendpulse.orchestration.pipeline import Pipeline

    wh = _warehouse(settings)
    _sample_guard(wh, args.allow_sample_on_postgres)
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=args.interval * (args.cycles - 1))
    clock = {"t": start}
    session = SampleYouTubeSession(lambda: clock["t"], epoch=end, fault_rate=args.fault_rate)
    pipe = Pipeline(settings, wh, session_factory=lambda st: session, clock=lambda: clock["t"],
                    lake_dir=settings.data_dir / "lake" if args.lake else None, notify=False)
    statuses: dict[str, int] = {}
    for i in range(args.cycles):
        clock["t"] = start + timedelta(minutes=args.interval * i)
        res = pipe.run(trigger="simulation", data_source="sample_fixture")
        statuses[res.status] = statuses.get(res.status, 0) + 1
        print(f"[{i + 1:>3}/{args.cycles}] {clock['t']:%Y-%m-%d %H:%M} UTC  {res.status:<10} "
              f"videos={res.counts['videos']:<4} topics={res.counts['topics']:<4} quota={res.counts['quota_used_run']}")
    print(json.dumps(statuses))
    return EXIT_OK if not statuses.get("failed") else EXIT_FAILED


def cmd_replay(args, settings) -> int:
    from trendpulse.orchestration.pipeline import Pipeline

    res = Pipeline(settings, _warehouse(settings), lake_dir=settings.data_dir / "lake").replay(args.run_id)
    print(json.dumps({"run_id": res.run_id, "status": res.status, "counts": res.counts, "error": res.error},
                     indent=2, default=str))
    return EXIT_OK if res.status in {"success", "partial"} else EXIT_FAILED


def cmd_migrate(args, settings) -> int:
    from trendpulse.storage.migrations import current_version, migrate

    wh = _warehouse(settings)
    applied = migrate(wh)
    print(f"warehouse={wh.describe()} applied={applied} version={current_version(wh)}")
    return EXIT_OK


def cmd_validate_config(args, settings) -> int:
    from trendpulse.ingestion.discovery import rotation_pairs
    from trendpulse.topics.classifier import TaxonomyClassifier

    clf = TaxonomyClassifier(settings.taxonomy, settings.pipeline.topics)
    print(f"OK: {len(settings.region_codes)} enabled regions, {len(settings.seeds.seeds)} search seeds "
          f"({len(rotation_pairs(settings))} rotation pairs), {len(clf.topics)} taxonomy topics, "
          f"{len(clf.all_keywords)} keywords, windows={list(settings.pipeline.comparison_windows)}")
    plan = quota_plan(settings)
    if plan["total"] > settings.pipeline.quota.daily_budget:
        print(f"WARNING: planned daily usage {plan['total']} exceeds budget {settings.pipeline.quota.daily_budget}; "
              "the quota guard will stop calls early each day")
    return EXIT_OK


def quota_plan(settings) -> dict:
    p = settings.pipeline
    runs_per_day = 1440 // p.schedule.interval_minutes
    charts = sum((1 + len(settings.countries.categories_for(c))) * p.ingestion.chart_pages
                 for c in settings.countries.enabled)
    search_cycles = 1440 // max(p.ingestion.search_interval_minutes, p.schedule.interval_minutes) \
        if p.ingestion.search_enabled else 0
    search = search_cycles * p.ingestion.searches_per_cycle * (100 + 1)
    tracked = min(p.ingestion.max_tracked_videos,
                  p.ingestion.searches_per_cycle * p.ingestion.search_max_results
                  * max(1, p.ingestion.track_search_videos_hours * 60 // max(p.ingestion.search_interval_minutes, 1)))
    refresh = runs_per_day * -(-tracked // 50)
    return {"runs_per_day": runs_per_day, "chart_units_per_run": charts, "charts": charts * runs_per_day,
            "search_cycles_per_day": search_cycles, "search": search, "max_tracked_videos": tracked,
            "refresh": refresh, "total": charts * runs_per_day + search + refresh,
            "daily_budget": p.quota.daily_budget, "daily_limit": p.quota.daily_limit}


def cmd_quota_plan(args, settings) -> int:
    print(json.dumps(quota_plan(settings), indent=2))
    return EXIT_OK


def cmd_status(args, settings) -> int:
    from trendpulse.dashboard import data as d

    wh = _warehouse(settings, read_only=False)
    runs = d.recent_runs(wh, 10)
    print(runs[["run_id", "status", "trigger", "data_source", "started_at", "duration_seconds",
                "records_silver", "quota_used", "gold_published"]].to_string(index=False) if not runs.empty
          else "no runs yet")
    print(json.dumps(d.platform_health(wh, settings), indent=2, default=str))
    return EXIT_OK


def cmd_retention(args, settings) -> int:
    from trendpulse.storage.retention import apply_retention

    print(json.dumps(apply_retention(_warehouse(settings), settings.pipeline.retention), indent=2))
    return EXIT_OK


def cmd_export_parquet(args, settings) -> int:
    """Export Gold models to Parquet and build a DuckDB file over them for local analysis."""
    import duckdb
    from sqlalchemy import select

    from trendpulse.storage import schema as s

    wh = _warehouse(settings)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tables = [s.dim_regions, s.dim_channels, s.dim_videos, s.dim_topics, s.fact_video_observations,
              s.fact_topic_observations, *s.SERVING_TABLES]
    con = duckdb.connect(str(out / "trendpulse_gold.duckdb"))
    for t in tables:
        df = wh.read_df(select(t))
        path = out / f"{t.name}.parquet"
        df.to_parquet(path, index=False)
        con.execute(f"CREATE OR REPLACE VIEW {t.name} AS SELECT * FROM read_parquet('{path.as_posix()}')")
        print(f"{t.name:<28} {len(df):>8} rows -> {path}")
    con.close()
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trendpulse", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="execute one pipeline run")
    r.add_argument("--sample", action="store_true", help="use the offline SAMPLE API simulator (no key needed)")
    r.add_argument("--trigger", default="manual", choices=["manual", "schedule", "workflow_dispatch", "local"])
    r.add_argument("--force-search", action="store_true", help="run a search discovery cycle even if not due")
    r.add_argument("--allow-sample-on-postgres", action="store_true")
    r.set_defaults(fn=cmd_run)
    sm = sub.add_parser("simulate", help="backfill SAMPLE cycles on a simulated clock (local demo)")
    sm.add_argument("--cycles", type=int, default=96)
    sm.add_argument("--interval", type=int, default=30)
    sm.add_argument("--fault-rate", type=float, default=0.002, help="fraction of malformed sample items")
    sm.add_argument("--lake", action="store_true", help="also write the local Parquet/JSON lake")
    sm.add_argument("--allow-sample-on-postgres", action="store_true")
    sm.set_defaults(fn=cmd_simulate)
    rp = sub.add_parser("replay", help="rebuild Silver/Gold for a run from its Bronze data")
    rp.add_argument("--run-id", required=True)
    rp.set_defaults(fn=cmd_replay)
    for name, fn, h in (("migrate", cmd_migrate, "apply database migrations"),
                        ("validate-config", cmd_validate_config, "validate YAML configuration"),
                        ("status", cmd_status, "show recent runs and platform health"),
                        ("retention", cmd_retention, "apply retention/compaction now"),
                        ("quota-plan", cmd_quota_plan, "estimate daily API quota usage from config")):
        sp = sub.add_parser(name, help=h)
        sp.set_defaults(fn=fn)
    ex = sub.add_parser("export-parquet", help="export Gold to Parquet + DuckDB views")
    ex.add_argument("--out", default="data/lake/gold")
    ex.set_defaults(fn=cmd_export_parquet)
    return p


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return int(args.fn(args, settings) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
