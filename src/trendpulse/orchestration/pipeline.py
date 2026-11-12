"""Pipeline orchestrator.

Seventeen stages, each timed, logged, retried where safe and persisted to
``pipeline_stage_runs``. The run itself is recorded in ``pipeline_runs``.

Guarantees
----------
* **No overlap** — a TTL'd database lock (plus a GitHub Actions concurrency
  group). An expired lock from a crashed run is taken over automatically.
* **Idempotent** — facts are keyed by ``run_id`` and written with
  ``ON CONFLICT DO NOTHING``; replaying a run never duplicates history.
* **Safe failure** — Bronze is persisted per response as it arrives; Gold is
  published in a single transaction only after the quality gate passes. A
  failed run leaves the previous Gold snapshot untouched.
* **Recovery** — ``trendpulse replay --run-id`` rebuilds Silver/Gold for a
  run from its Bronze data (e.g. after a crash or a fixed DQ issue);
  stale ``running`` rows from crashed executions are marked ``abandoned``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import OperationalError

from trendpulse.gold.builder import GoldInputs, publish_gold
from trendpulse.ingestion.bronze import BronzeRecord, BronzeWriter, load_bronze
from trendpulse.ingestion.discovery import (
    DiscoveryContext,
    DiscoveryResult,
    discover_regional_charts,
    discover_search,
    refresh_tracked_metrics,
)
from trendpulse.ingestion.quota import QuotaTracker, units_used_today
from trendpulse.ingestion.youtube_client import YouTubeClient
from trendpulse.logging_utils import get_logger, set_context
from trendpulse.monitoring.alerts import send_alert
from trendpulse.quality.framework import (
    FAIL,
    CheckResult,
    check_schema,
    check_stale,
    check_topic_coverage,
    check_volume,
    gate_passed,
    overall_status,
    validate_records,
)
from trendpulse.settings import Settings
from trendpulse.storage import schema as s
from trendpulse.storage.db import Warehouse, as_utc, records_from_df, utcnow
from trendpulse.storage.migrations import migrate
from trendpulse.storage.retention import apply_retention
from trendpulse.topics.engine import TopicOutput, run_topic_engine
from trendpulse.transform.silver import (
    SILVER_COLUMNS,
    SILVER_PERSISTED_COLUMNS,
    SilverBatch,
    bronze_to_silver,
)

log = get_logger("pipeline")

LOCK_NAME = "trendpulse-pipeline"

STAGES = [
    "start_execution", "load_configuration", "check_quota_budget", "discover_regional_charts",
    "discover_topic_search", "fetch_video_metrics", "save_bronze", "validate_schema", "transform_silver",
    "extract_topics", "data_quality", "quarantine_records", "publish_gold", "generate_gold_aggregates",
    "update_execution_metadata", "publish_execution_status", "send_notifications",
]


class StageFailed(RuntimeError):
    pass


class QualityGateFailed(RuntimeError):
    pass


@dataclass
class RunState:
    run_id: str
    now: datetime
    trigger: str
    data_source: str
    replay: bool = False
    bronze: list[BronzeRecord] = field(default_factory=list)
    discovery: DiscoveryResult = field(default_factory=DiscoveryResult)
    silver: SilverBatch | None = None
    valid: pd.DataFrame | None = None
    quarantined: list[dict] = field(default_factory=list)
    dq: list[CheckResult] = field(default_factory=list)
    topics: TopicOutput | None = None
    gold_counts: dict[str, int] = field(default_factory=dict)
    stage_log: list[dict] = field(default_factory=list)
    status: str = "running"
    error: str | None = None
    quota: QuotaTracker | None = None
    api_log_buffer: list[dict] = field(default_factory=list)
    skipped_reason: str | None = None


@dataclass
class PipelineResult:
    run_id: str
    status: str
    gold_published: bool
    dq_status: str | None
    stages: list[dict]
    counts: dict[str, Any]
    error: str | None = None


def _git_sha() -> str | None:
    return os.environ.get("GITHUB_SHA") or None


class Pipeline:
    def __init__(self, settings: Settings, wh: Warehouse, *, session_factory: Callable[[RunState], Any] | None = None,
                 clock: Callable[[], datetime] | None = None, lake_dir: Path | None = None,
                 sleep: Callable[[float], None] = time.sleep, notify: bool = True):
        self.settings = settings
        self.wh = wh
        self.session_factory = session_factory
        self.clock = clock or (lambda: utcnow().replace(microsecond=0))
        self.lake_dir = lake_dir
        self.sleep = sleep
        self.notify = notify

    # ================================================================ public API
    def run(self, trigger: str = "manual", data_source: str = "youtube_api", force_search: bool = False,
            api_key: str | None = None) -> PipelineResult:
        now = self.clock().astimezone(UTC)
        self._real_start = utcnow()
        st = RunState(run_id=str(uuid.uuid4()), now=now, trigger=trigger, data_source=data_source)
        set_context(run_id=st.run_id)
        log.info("pipeline run %s starting (trigger=%s, source=%s, warehouse=%s)", st.run_id, trigger,
                 data_source, self.wh.describe())
        migrate(self.wh)
        self._mark_abandoned()
        if not self.wh.acquire_lock(LOCK_NAME, st.run_id, timedelta(minutes=self.settings.pipeline.lock.ttl_minutes)):
            holder = self.wh.lock_holder(LOCK_NAME)
            log.warning("another run holds the pipeline lock (%s); exiting without work", holder)
            return PipelineResult(st.run_id, "skipped_locked", False, None, [], {},
                                  f"lock held by {holder and holder.get('owner_run_id')}")
        try:
            self._insert_run(st)
            key = api_key if api_key is not None else self.settings.secrets.youtube_api_key
            self._execute(st, key, force_search)
        finally:
            self.wh.release_lock(LOCK_NAME, st.run_id)
            set_context(stage=None)
        return self._result(st)

    def replay(self, run_id: str) -> PipelineResult:
        """Rebuild Silver/Gold for an existing run from its Bronze records."""
        migrate(self.wh)
        bronze = load_bronze(self.wh, run_id)
        if not bronze:
            raise ValueError(f"no Bronze data for run {run_id} (retention window passed?)")
        row = self.wh.read_df(select(s.pipeline_runs).where(s.pipeline_runs.c.run_id == run_id))
        now = as_utc(bronze[0].fetched_at)
        source = row["data_source"].iloc[0] if not row.empty else "youtube_api"
        st = RunState(run_id=run_id, now=now, trigger="replay", data_source=source, replay=True, bronze=bronze)
        self._real_start = utcnow()
        set_context(run_id=run_id)
        if not self.wh.acquire_lock(LOCK_NAME, f"replay-{run_id}"[:36],
                                    timedelta(minutes=self.settings.pipeline.lock.ttl_minutes)):
            return PipelineResult(run_id, "skipped_locked", False, None, [], {}, "lock held")
        try:
            if row.empty:
                self._insert_run(st)
            else:
                with self.wh.begin() as conn:
                    conn.execute(s.pipeline_stage_runs.delete().where(s.pipeline_stage_runs.c.run_id == run_id))
                    conn.execute(update(s.pipeline_runs).where(s.pipeline_runs.c.run_id == run_id)
                                 .values(status="running", trigger="replay", error_message=None))
            self._execute(st, None, False, replay=True)
        finally:
            self.wh.release_lock(LOCK_NAME, f"replay-{run_id}"[:36])
        return self._result(st)

    # ================================================================ engine
    def _execute(self, st: RunState, api_key: str | None, force_search: bool, replay: bool = False) -> None:
        order = {name: i for i, name in enumerate(STAGES)}
        ingest_stages: list[tuple[str, Callable[[], Any], int]] = [
            ("load_configuration", lambda: self._load_configuration(st), 1),
            ("check_quota_budget", lambda: self._check_quota(st, api_key), 0),
            ("discover_regional_charts", lambda: self._discover(st, "charts", force_search), 0),
            ("discover_topic_search", lambda: self._discover(st, "search", force_search), 0),
            ("fetch_video_metrics", lambda: self._discover(st, "refresh", force_search), 0),
            ("save_bronze", lambda: self._save_bronze(st), 0),
        ]
        process_stages: list[tuple[str, Callable[[], Any], int]] = [
            ("validate_schema", lambda: self._validate_schema(st), 0),
            ("transform_silver", lambda: self._transform_silver(st), 2),
            ("extract_topics", lambda: self._extract_topics(st), 0),
            ("data_quality", lambda: self._data_quality(st), 1),
            ("quarantine_records", lambda: self._quarantine(st), 2),
            ("publish_gold", lambda: self._publish_gold(st), 2),
            ("generate_gold_aggregates", lambda: self._verify_gold(st), 0),
        ]
        self._record_stage(st, "start_execution", order, utcnow(), "success", 1, None, None, None)
        plan = ([("load_configuration", ingest_stages[0][1], 1)] if replay else ingest_stages) + process_stages
        failed = False
        for name, fn, retries in plan:
            if failed or (st.skipped_reason and name not in {"load_configuration", "check_quota_budget"}):
                self._record_stage(st, name, order, utcnow(), "skipped", 0, None, None, st.skipped_reason)
                continue
            try:
                self._run_stage(st, name, fn, retries, order)
            except QualityGateFailed as exc:
                st.status, st.error, failed = "dq_failed", str(exc), True
            except StageFailed as exc:
                st.status, st.error, failed = "failed", str(exc), True
        if not failed:
            if st.skipped_reason:
                st.status = "skipped"
            elif st.discovery.partial:
                st.status = "partial"
            else:
                st.status = "success"
        for name, fn in (("update_execution_metadata", lambda: self._finalize(st)),
                         ("publish_execution_status", lambda: self._publish_status(st)),
                         ("send_notifications", lambda: self._notify(st))):
            try:
                self._run_stage(st, name, fn, 1, order)
            except StageFailed as exc:  # finalisation problems must not mask the run outcome
                log.error("%s failed: %s", name, exc)

    def _run_stage(self, st: RunState, name: str, fn: Callable[[], Any], retries: int, order: dict) -> None:
        set_context(stage=name)
        started = utcnow()
        t0 = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            try:
                info = fn() or {}
                break
            except QualityGateFailed as exc:
                self._record_stage(st, name, order, started, "failed", attempts, None, None, str(exc),
                                   int((time.monotonic() - t0) * 1000))
                raise
            except OperationalError as exc:  # transient DB/network errors are retried
                if attempts > retries:
                    self._record_stage(st, name, order, started, "failed", attempts, None, None, str(exc)[:2000],
                                       int((time.monotonic() - t0) * 1000))
                    raise StageFailed(f"{name}: {exc}") from exc
                delay = min(30.0, 2.0 ** attempts)
                log.warning("stage %s attempt %d failed (%s); retrying in %.0fs", name, attempts, exc, delay)
                self.sleep(delay)
            except Exception as exc:  # noqa: BLE001 - recorded and surfaced
                log.exception("stage %s failed", name)
                self._record_stage(st, name, order, started, "failed", attempts, None, None,
                                   f"{type(exc).__name__}: {exc}"[:2000], int((time.monotonic() - t0) * 1000))
                raise StageFailed(f"{name}: {type(exc).__name__}: {exc}") from exc
        status = info.pop("_status", "success")
        self._record_stage(st, name, order, started, status, attempts, info.get("records_in"),
                           info.get("records_out"), info.get("note"), int((time.monotonic() - t0) * 1000))
        set_context(stage=None)

    # ================================================================ stages
    def _load_configuration(self, st: RunState) -> dict:
        p = self.settings.pipeline
        return {"note": f"{len(self.settings.region_codes)} regions, {len(self.settings.seeds.seeds)} seeds, "
                        f"{sum(len(c.topics) for c in self.settings.taxonomy.categories)} taxonomy topics, "
                        f"budget {p.quota.daily_budget}/day"}

    def _check_quota(self, st: RunState, api_key: str | None) -> dict:
        used = units_used_today(self.wh, st.now, self.settings.pipeline.quota.timezone)
        st.quota = QuotaTracker(self.settings.pipeline.quota, used_before_run=used)
        if st.data_source != "sample_fixture" and not api_key:
            raise RuntimeError("YOUTUBE_API_KEY is not set — set the secret, or run with --sample for offline demo data")
        if st.quota.remaining < 1:
            st.skipped_reason = f"daily quota budget exhausted ({used}/{st.quota.cfg.daily_budget} units)"
            return {"_status": "skipped", "note": st.skipped_reason}
        session = self.session_factory(st) if self.session_factory else None
        self._client = YouTubeClient(api_key or "sample-key", self.settings.pipeline.ingestion, st.quota,
                                     session=session, on_request=lambda r: self._log_request(st, r), sleep=self.sleep)
        lake = self.lake_dir
        self._ctx = DiscoveryContext(self.settings, self._client, BronzeWriter(self.wh, lake), self.wh, st.run_id,
                                     st.now)
        return {"records_in": used, "note": f"{used} units used today, {st.quota.remaining} remaining in budget"}

    def _discover(self, st: RunState, which: str, force_search: bool) -> dict:
        try:
            if st.discovery.quota_stopped:
                return {"_status": "skipped", "note": "quota budget reached earlier in this run"}
            if which == "charts":
                res = discover_regional_charts(self._ctx)
            elif which == "search":
                res, self._search_ids = discover_search(self._ctx, force_search)
                if not res.search_ran and not res.quota_stopped:
                    return {"_status": "skipped", "note": "not due (search_interval_minutes)"}
            else:
                res = refresh_tracked_metrics(self._ctx, getattr(self, "_search_ids", set()))
            st.discovery.merge(res)
            st.bronze += res.records
            items = sum(r.item_count for r in res.records)
            status = "partial" if res.partial else "success"
            note = "; ".join(res.errors[:3]) or ("stopped at quota budget" if res.quota_stopped else None)
            return {"_status": status, "records_out": items, "records_in": res.calls, "note": note}
        finally:
            self._flush_api_log(st)

    def _save_bronze(self, st: RunState) -> dict:
        # Bronze is written per response during discovery (crash-safe); this stage verifies it.
        n_db = self.wh.scalar(select(func.count()).select_from(s.bronze_api_responses)
                              .where(s.bronze_api_responses.c.run_id == st.run_id))
        if n_db != len(st.bronze):
            raise RuntimeError(f"bronze verification failed: {n_db} persisted vs {len(st.bronze)} fetched")
        if not st.bronze:
            st.skipped_reason = st.skipped_reason or "no API responses collected"
            return {"_status": "skipped", "note": st.skipped_reason}
        kb = self._ctx.writer.bytes_compressed / 1024 if hasattr(self, "_ctx") else 0
        return {"records_out": len(st.bronze), "note": f"{len(st.bronze)} responses, {kb:.0f} KiB gzip"}

    def _validate_schema(self, st: RunState) -> dict:
        st.silver = bronze_to_silver(st.bronze)
        res = check_schema(st.silver.schema, self.settings)
        st.dq.append(res)
        return {"records_in": st.silver.schema.items, "records_out": res.passed,
                "_status": "success" if res.status != FAIL else "warning", "note": f"schema_drift={res.status}"}

    def _transform_silver(self, st: RunState) -> dict:
        assert st.silver is not None
        rv = validate_records(st.silver.df, self.settings, st.run_id, st.now)
        st.valid, st.quarantined = rv.valid, rv.quarantined
        st.dq += rv.results
        with self.wh.begin() as conn:
            self.wh.upsert(conn, s.silver_video_observations, records_from_df(st.valid, SILVER_PERSISTED_COLUMNS),
                           ["run_id", "video_id", "region_code", "discovery_method", "discovery_query"], [])
        if self.lake_dir is not None and not st.valid.empty:
            d = self.lake_dir / "silver" / f"dt={st.now:%Y-%m-%d}"
            d.mkdir(parents=True, exist_ok=True)
            st.valid[SILVER_COLUMNS].to_parquet(d / f"run={st.run_id}.parquet", index=False)
        return {"records_in": len(st.silver.df), "records_out": len(st.valid),
                "note": f"{len(st.quarantined)} records routed to quarantine"}

    def _extract_topics(self, st: RunState) -> dict:
        assert st.valid is not None
        if st.valid.empty:
            raise QualityGateFailed("no valid Silver records to classify")
        st.topics = run_topic_engine(st.valid, self.settings, self.wh, st.run_id, st.now)
        return {"records_in": st.topics.stats["videos"], "records_out": st.topics.stats["assignments"],
                "note": json.dumps(st.topics.stats)}

    def _data_quality(self, st: RunState) -> dict:
        assert st.valid is not None and st.topics is not None
        pr = s.pipeline_runs
        hist = self.wh.read_df(select(pr.c.records_silver, pr.c.finished_at).where(and_(
            pr.c.status.in_(["success", "partial"]), pr.c.run_id != st.run_id, pr.c.started_at < st.now))
            .order_by(pr.c.started_at.desc()).limit(self.settings.pipeline.quality.volume_lookback_runs))
        last_ok = as_utc(hist["finished_at"].iloc[0]) if not hist.empty else None
        st.dq.append(check_stale(st.valid, st.now, self.settings, last_ok))
        st.dq.append(check_volume(len(st.valid), [int(x) for x in hist["records_silver"].dropna()], self.settings))
        st.dq.append(check_topic_coverage(st.valid, st.topics.assignments))
        with self.wh.begin() as conn:
            conn.execute(s.dq_results.delete().where(s.dq_results.c.run_id == st.run_id))
            self.wh.insert_many(conn, s.dq_results, [r.to_row(st.run_id, st.now) for r in st.dq])
        status = overall_status(st.dq)
        failed = [r.check_name for r in st.dq if r.status == FAIL]
        if not gate_passed(st.dq):
            raise QualityGateFailed(f"critical data-quality gate failed: {', '.join(failed)}")
        return {"records_in": len(st.dq), "records_out": sum(r.status == "PASS" for r in st.dq),
                "_status": "success" if status == "PASS" else "warning", "note": f"overall={status}"}

    def _quarantine(self, st: RunState) -> dict:
        if st.quarantined:
            with self.wh.begin() as conn:
                conn.execute(s.quarantine_records.delete().where(s.quarantine_records.c.run_id == st.run_id))
                self.wh.insert_many(conn, s.quarantine_records, st.quarantined)
        return {"records_out": len(st.quarantined)}

    def _publish_gold(self, st: RunState) -> dict:
        assert st.valid is not None and st.topics is not None
        latest = self.wh.scalar(select(s.gold_publications.c.snapshot_at)
                                .where(s.gold_publications.c.snapshot_run_id != st.run_id)
                                .order_by(s.gold_publications.c.snapshot_at.desc()).limit(1))
        if latest is not None and as_utc(latest) > st.now:
            raise RuntimeError("a newer snapshot is already published; refusing to replace serving tables "
                               "with an older replay")
        st.gold_counts = publish_gold(self.wh, GoldInputs(st.run_id, st.now, st.data_source, st.valid, st.topics),
                                      self.settings)
        return {"records_in": len(st.valid), "records_out": st.gold_counts.get("fact_video_observations", 0)
                + st.gold_counts.get("fact_topic_observations", 0),
                "note": f"history appended: {st.gold_counts.get('fact_video_observations', 0)} video obs, "
                        f"{st.gold_counts.get('fact_topic_observations', 0)} topic obs"}

    def _verify_gold(self, st: RunState) -> dict:
        gt = s.gold_trending_topics
        rid = self.wh.scalar(select(gt.c.snapshot_run_id).limit(1))
        if rid != st.run_id:
            raise RuntimeError("serving tables do not reflect this run after publication")
        serving = {k: v for k, v in st.gold_counts.items() if k.startswith("gold_")}
        return {"records_out": sum(serving.values()), "note": json.dumps(serving)}

    def _finalize(self, st: RunState) -> dict:
        retention = {}
        if st.status in {"success", "partial"} and not st.replay:
            retention = apply_retention(self.wh, self.settings.pipeline.retention, st.now)
        self._update_run(st)
        return {"note": f"status={st.status}; retention={ {k: v for k, v in retention.items() if v} }"}

    def _publish_status(self, st: RunState) -> dict:
        res = self._result(st)
        payload = {"run_id": res.run_id, "status": res.status, "gold_published": res.gold_published,
                   "dq_status": res.dq_status, "started_at": st.now.isoformat(), "counts": res.counts,
                   "error": res.error, "data_source": st.data_source}
        out_dir = self.settings.data_dir / "status"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "last_run.json").write_text(json.dumps(payload, indent=2, default=str))
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(f"## TrendPulse run `{st.run_id[:8]}` — **{st.status.upper()}**\n\n")
                fh.write("| Stage | Status | Duration | In | Out | Note |\n|---|---|---|---|---|---|\n")
                for r in st.stage_log:
                    fh.write(f"| {r['stage']} | {r['status']} | {r.get('duration_ms') or 0} ms | "
                             f"{r.get('records_in') or ''} | {r.get('records_out') or ''} | "
                             f"{(r.get('error') or '').replace('|', '/')[:120]} |\n")
                fh.write(f"\nQuota used this run: {res.counts.get('quota_used_run')} units\n")
        return {"note": str(out_dir / "last_run.json")}

    def _notify(self, st: RunState) -> dict:
        if not self.notify:
            return {"_status": "skipped", "note": "notifications disabled"}
        a = self.settings.pipeline.alerts
        pr = s.pipeline_runs
        prev = self.wh.read_df(select(pr.c.status).where(and_(pr.c.run_id != st.run_id,
                                                              pr.c.status.notin_(["running", "skipped"])))
                               .order_by(pr.c.started_at.desc()).limit(1))
        prev_status = prev["status"].iloc[0] if not prev.empty else None
        subject = None
        if st.status in {"failed", "dq_failed"} and a.on_failure:
            subject = f"Pipeline {st.status.upper()} ({st.run_id[:8]})"
        elif st.status in {"success", "partial"} and prev_status in {"failed", "dq_failed"} and a.on_recovery:
            subject = f"Pipeline recovered ({st.run_id[:8]})"
        elif a.on_dq_warning and any(r.status == "WARNING" for r in st.dq):
            subject = f"Data-quality warnings ({st.run_id[:8]})"
        if not subject:
            return {"_status": "skipped", "note": "no alert condition"}
        body = (f"status={st.status}\nerror={st.error}\nstarted={st.now.isoformat()}\n"
                + "\n".join(f"- {r.check_name}: {r.status} ({r.failed} failed)" for r in st.dq if r.status != "PASS"))
        sent = send_alert(self.settings.secrets, subject, body)
        return {"note": f"sent via {sent}" if sent else "no alert channel configured",
                "_status": "success" if sent else "skipped"}

    # ================================================================ persistence helpers
    def _log_request(self, st: RunState, r: dict) -> None:
        st.api_log_buffer.append({
            "run_id": st.run_id, "endpoint": r["endpoint"], "region_code": r.get("region_code"),
            "discovery_method": r.get("discovery_method"), "params_json": json.dumps(r["params"], sort_keys=True)[:2000],
            "http_status": r["http_status"], "quota_cost": r["quota_cost"], "items_returned": r["items_returned"],
            "duration_ms": r["duration_ms"], "attempt": r["attempt"],
            # run clock + real elapsed time (identical to wall-clock for live runs; keeps simulations coherent)
            "requested_at": st.now + (utcnow() - self._real_start), "error": r["error"]})

    def _flush_api_log(self, st: RunState) -> None:
        if st.api_log_buffer:
            with self.wh.begin() as conn:
                self.wh.insert_many(conn, s.api_request_log, st.api_log_buffer)
            st.api_log_buffer = []

    def _insert_run(self, st: RunState) -> None:
        with self.wh.begin() as conn:
            conn.execute(s.pipeline_runs.insert().values(
                run_id=st.run_id, trigger=st.trigger, data_source=st.data_source, status="running",
                started_at=st.now, gold_published=False, git_sha=_git_sha()))

    def _record_stage(self, st: RunState, name: str, order: dict, started: datetime, status: str, attempts: int,
                      rin, rout, note, duration_ms: int | None = None) -> None:
        row = {"run_id": st.run_id, "stage": name, "stage_order": order.get(name, 99), "status": status,
               "started_at": started, "finished_at": utcnow(), "duration_ms": duration_ms or 0,
               "records_in": rin, "records_out": rout, "attempts": attempts,
               "error": (str(note)[:2000] if note else None)}
        st.stage_log.append(row)
        try:
            with self.wh.begin() as conn:
                self.wh.upsert(conn, s.pipeline_stage_runs, [row], ["run_id", "stage"])
        except Exception:  # noqa: BLE001 - never let bookkeeping kill the run
            log.exception("could not persist stage record %s", name)

    def _update_run(self, st: RunState) -> None:
        res = self._result(st)
        finished = utcnow()
        with self.wh.begin() as conn:
            conn.execute(update(s.pipeline_runs).where(s.pipeline_runs.c.run_id == st.run_id).values(
                status=st.status, finished_at=finished,
                # wall-clock execution time (st.now may be a simulated clock)
                duration_seconds=max(0.0, (finished - self._real_start).total_seconds()),
                records_bronze=res.counts["bronze_responses"], records_silver=res.counts["silver_valid"],
                records_quarantined=res.counts["quarantined"], records_gold=res.counts["gold_rows"],
                videos_observed=res.counts["videos"], topics_observed=res.counts["topics"],
                quota_used=res.counts["quota_used_run"], gold_published=res.gold_published,
                dq_status=res.dq_status, error_message=(st.error or st.skipped_reason),
                stage_summary=json.dumps([{k: r[k] for k in ("stage", "status", "duration_ms")}
                                          for r in st.stage_log], default=str)))

    def _mark_abandoned(self) -> None:
        cutoff = utcnow() - timedelta(minutes=self.settings.pipeline.lock.ttl_minutes)
        with self.wh.begin() as conn:
            n = conn.execute(update(s.pipeline_runs).where(and_(s.pipeline_runs.c.status == "running",
                                                                s.pipeline_runs.c.started_at < cutoff))
                             .values(status="abandoned", finished_at=utcnow(),
                                     error_message="run did not finish (process crashed or was cancelled); "
                                                   "Bronze data can be replayed")).rowcount
        if n:
            log.warning("marked %d stale running execution(s) as abandoned", n)

    def _result(self, st: RunState) -> PipelineResult:
        counts = {
            "bronze_responses": len(st.bronze),
            "silver_candidates": 0 if st.silver is None else len(st.silver.df),
            "silver_valid": 0 if st.valid is None else len(st.valid),
            "quarantined": len(st.quarantined),
            "videos": 0 if st.valid is None else int(st.valid["video_id"].nunique()),
            "topics": 0 if not st.gold_counts else int(st.gold_counts.get("gold_trending_topics", 0)),
            "gold_rows": sum(st.gold_counts.values()) if st.gold_counts else 0,
            "quota_used_run": st.quota.used_this_run if st.quota else 0,
            "quota_used_today": st.quota.used_today if st.quota else None,
            "api_calls": st.discovery.calls,
        }
        return PipelineResult(st.run_id, st.status, bool(st.gold_counts), overall_status(st.dq) if st.dq else None,
                              st.stage_log, counts, st.error)
