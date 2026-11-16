"""Read-only control-plane queries and operational rules.

Everything the engineering modules show comes from here: pure functions over a
:class:`Warehouse` (or over already-fetched metadata), unit-tested, no Streamlit.
Aggregation happens in the database; every list is paginated or limited.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import and_, desc, func, or_, select, text

from trendpulse import catalog
from trendpulse.orchestration import dag
from trendpulse.orchestration.pipeline import STAGES
from trendpulse.settings import Settings
from trendpulse.storage import schema as s
from trendpulse.storage.db import Warehouse, as_utc, utcnow

RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FAILED_STATUSES = {"failed", "dq_failed"}
OK_STATUSES = {"success", "partial"}


# ====================================================================== health
@dataclass
class Health:
    status: str              # healthy | degraded | failed | stale | not_initialized
    reasons: list[str]


def health_status(health: dict | None, latest_dq: str | None, settings: Settings,
                  now: datetime | None = None) -> Health:
    """Platform health from real run metadata. Rules (first match wins):

    not_initialized — no execution recorded
    failed          — latest attempt failed/dq_failed and nothing has succeeded since
    stale           — latest published snapshot older than 3 × stale_after_minutes
    degraded        — latest attempt partial/skipped/abandoned, DQ WARNING, freshness delayed,
                      7-day success rate < 90 %, or quota ≥ 90 % of budget
    healthy         — otherwise
    """
    now = now or utcnow()
    if not health or not health.get("last_attempt"):
        return Health("not_initialized", ["No pipeline execution has been recorded yet."])
    last = health["last_attempt"]
    ok = health.get("last_success")
    if last["status"] in FAILED_STATUSES and (not ok or as_utc(ok["started_at"]) < as_utc(last["started_at"])):
        return Health("failed", [f"Latest execution {last['run_id'][:8]} ended {last['status']}: "
                                 f"{(last.get('error_message') or '')[:160]}"])
    if health.get("freshness") == "stale":
        return Health("stale", [f"Latest published snapshot is {health['freshness_minutes'] / 60:.1f} h old."])
    reasons = []
    if last["status"] in {"partial", "skipped", "abandoned"}:
        reasons.append(f"Latest execution ended {last['status']}"
                       + (f": {last['error_message'][:140]}" if last.get("error_message") else ""))
    if latest_dq == "WARNING":
        reasons.append("Latest data-quality audit has warnings.")
    if health.get("freshness") == "delayed":
        reasons.append("Data is older than two scheduling intervals.")
    rate = health.get("success_rate_7d")
    if rate is not None and rate < 0.9:
        reasons.append(f"7-day success rate {rate:.0%} (< 90 %).")
    budget = health.get("quota_budget") or 0
    if budget and health.get("quota_used_today", 0) >= 0.9 * budget:
        reasons.append("API quota usage ≥ 90 % of the daily budget.")
    return Health("degraded" if reasons else "healthy", reasons or ["All checks nominal."])


def active_conditions(wh: Warehouse, health: dict | None, settings: Settings,
                      now: datetime | None = None) -> list[dict]:
    """Operational conditions derived from metadata (not notifications that were sent)."""
    now = now or utcnow()
    out: list[dict] = []
    if not health or not health.get("last_attempt"):
        return out
    pr = s.pipeline_runs
    recent = wh.read_df(select(pr.c.run_id, pr.c.status, pr.c.started_at, pr.c.error_message)
                        .where(pr.c.started_at >= now - timedelta(hours=24)).order_by(desc(pr.c.started_at)))
    for status, sev in (("failed", "critical"), ("dq_failed", "critical"), ("abandoned", "serious")):
        n = int((recent.status == status).sum()) if not recent.empty else 0
        if n:
            ex = recent[recent.status == status].iloc[0]
            out.append({"severity": sev, "title": f"{n} {status.replace('_', ' ')} execution(s) in 24 h",
                        "detail": f"latest {ex.run_id[:8]}: {(ex.error_message or '')[:160]}", "run_id": ex.run_id})
    dq = latest_dq_results(wh)
    if not dq.empty:
        bad = dq[dq.status != "PASS"]
        for r in bad.itertuples():
            out.append({"severity": "critical" if r.status == "FAIL" else "warning",
                        "title": f"DQ {r.status}: {r.check_name}", "detail": f"{r.failed} failed / {r.warnings} "
                        f"warnings of {r.total_checked} in run {r.run_id[:8]}", "run_id": r.run_id})
    if health.get("freshness") in {"delayed", "stale"}:
        out.append({"severity": "warning" if health["freshness"] == "delayed" else "critical",
                    "title": f"Data freshness {health['freshness']}",
                    "detail": f"snapshot {health['freshness_minutes']:.0f} min old (target "
                              f"{health['interval_minutes']} min)", "run_id": None})
    budget = health.get("quota_budget") or 0
    if budget and health.get("quota_used_today", 0) >= 0.8 * budget:
        out.append({"severity": "warning", "title": "API quota ≥ 80 % of budget",
                    "detail": f"{health['quota_used_today']:,} / {budget:,} units today", "run_id": None})
    lock = wh.read_df(select(s.pipeline_lock))
    if not lock.empty and as_utc(lock.expires_at.iloc[0]) < now:
        out.append({"severity": "warning", "title": "Expired pipeline lock present",
                    "detail": f"held by {lock.owner_run_id.iloc[0][:8]}; next run takes it over", "run_id": None})
    if not settings.pipeline.sources.youtube.live_ingestion_enabled:
        out.append({"severity": "info", "title": "Live YouTube ingestion disabled",
                    "detail": "pending data-source compliance approval (docs/data_source_compliance.md)",
                    "run_id": None})
    return out


def totals(wh: Warehouse) -> dict[str, int]:
    pr = s.pipeline_runs
    row = wh.read_df(select(func.count().label("runs"), func.coalesce(func.sum(pr.c.records_silver), 0).label("silver"),
                            func.coalesce(func.sum(pr.c.records_quarantined), 0).label("rejected"),
                            func.coalesce(func.sum(pr.c.records_bronze), 0).label("bronze"),
                            func.coalesce(func.sum(pr.c.records_gold), 0).label("gold")))
    return {k: int(v) for k, v in row.iloc[0].items()}


# ====================================================================== executions
def _run_filters(statuses: list[str] | None, since: datetime | None, until: datetime | None):
    pr = s.pipeline_runs
    conds = []
    if statuses:
        st = set(statuses)
        parts = []
        plain = st - {"warning"}
        if plain:
            parts.append(pr.c.status.in_(sorted(plain)))
        if "warning" in st:
            parts.append(or_(pr.c.dq_status == "WARNING", pr.c.status == "partial"))
        conds.append(or_(*parts))
    if since:
        conds.append(pr.c.started_at >= since)
    if until:
        conds.append(pr.c.started_at < until)
    return conds


def runs(wh: Warehouse, statuses: list[str] | None = None, since: datetime | None = None,
         until: datetime | None = None, limit: int = 50, offset: int = 0) -> pd.DataFrame:
    """Execution history page with failed stage and retry count (aggregated in the database)."""
    pr, sr = s.pipeline_runs, s.pipeline_stage_runs
    agg = (select(sr.c.run_id, func.sum(_retries_expr()).label("retries")).group_by(sr.c.run_id).subquery())
    failed = (select(sr.c.run_id, func.min(sr.c.stage_order).label("failed_order"))
              .where(sr.c.status == "failed").group_by(sr.c.run_id).subquery())
    q = (select(pr.c.run_id, pr.c.trigger, pr.c.data_source, pr.c.status, pr.c.dq_status, pr.c.started_at,
                pr.c.finished_at, pr.c.duration_seconds, pr.c.records_bronze, pr.c.records_silver,
                pr.c.records_quarantined, pr.c.records_gold, pr.c.quota_used, pr.c.gold_published,
                pr.c.error_message, func.coalesce(agg.c.retries, 0).label("retries"), failed.c.failed_order)
         .select_from(pr.outerjoin(agg, agg.c.run_id == pr.c.run_id).outerjoin(failed, failed.c.run_id == pr.c.run_id))
         .where(and_(True, *_run_filters(statuses, since, until)))
         .order_by(desc(pr.c.started_at)).limit(limit).offset(offset))
    df = wh.read_df(q)
    if not df.empty:
        df["failed_stage"] = df.failed_order.map(lambda o: STAGES[int(o)] if pd.notna(o) and int(o) < len(STAGES)
                                                 else None)
        df = df.drop(columns=["failed_order"])
    return df


def run_count(wh: Warehouse, statuses: list[str] | None = None, since: datetime | None = None,
              until: datetime | None = None) -> int:
    pr = s.pipeline_runs
    return int(wh.scalar(select(func.count()).select_from(pr).where(and_(True, *_run_filters(statuses, since, until))))
               or 0)


def run_row(wh: Warehouse, run_id: str) -> dict | None:
    df = wh.read_df(select(s.pipeline_runs).where(s.pipeline_runs.c.run_id == run_id))
    return None if df.empty else df.iloc[0].to_dict()


def dag_state(wh: Warehouse, run_id: str) -> pd.DataFrame:
    """One row per declared stage, merged with the run's recorded stage execution."""
    run = run_row(wh, run_id)
    sr = s.pipeline_stage_runs
    rec = wh.read_df(select(sr).where(sr.c.run_id == run_id))
    rec = rec.set_index("stage") if not rec.empty else rec
    rows = []
    running_marked = False
    for i, spec in enumerate(dag.STAGE_SPECS):
        ups, downs = dag.neighbours(spec.name)
        r = rec.loc[spec.name] if (not rec.empty and spec.name in rec.index) else None
        if r is not None:
            status = r.status
        elif run and run["status"] == "running" and not running_marked:
            status, running_marked = "running", True
        elif run and run["status"] == "running":
            status = "pending"
        elif run and run["status"] == "abandoned" and not running_marked:
            # the process died while executing the first stage without a recorded outcome
            status, running_marked = "interrupted", True
        else:
            status = "not_recorded"
        rejected = None
        if spec.name in {"transform_silver", "quarantine_records"} and run:
            rejected = run.get("records_quarantined")
        rows.append({
            "order": i, "stage": spec.name, "label": spec.label, "group": spec.group, "status": status,
            "started_at": None if r is None else r.started_at, "finished_at": None if r is None else r.finished_at,
            "duration_ms": None if r is None else r.duration_ms, "records_in": None if r is None else r.records_in,
            "records_out": None if r is None else r.records_out, "rejected": rejected,
            "attempts": None if r is None else r.attempts, "retries": None if r is None else max(0, int(r.attempts) - 1),
            "detail": None if r is None else r.error, "upstream": ups, "downstream": downs,
            "inputs": list(spec.inputs), "outputs": list(spec.outputs), "condition": spec.condition,
            "description": spec.description,
        })
    return pd.DataFrame(rows)


# ====================================================================== logs
def run_logs(wh: Warehouse, run_id: str | None = None, levels: list[str] | None = None, search: str | None = None,
             stage: str | None = None, since: datetime | None = None, until: datetime | None = None,
             limit: int = 200, offset: int = 0) -> pd.DataFrame:
    lg = s.pipeline_log_records
    conds = []
    if run_id:
        conds.append(lg.c.run_id == run_id)
    if levels:
        conds.append(lg.c.level.in_(levels))
    if stage:
        conds.append(lg.c.stage == stage)
    if search:
        like = f"%{search.lower()}%"
        conds.append(or_(func.lower(lg.c.message).like(like), func.lower(func.coalesce(lg.c.exc_text, "")).like(like)))
    if since:
        conds.append(lg.c.logged_at >= since)
    if until:
        conds.append(lg.c.logged_at < until)
    return wh.read_df(select(lg).where(and_(True, *conds)).order_by(desc(lg.c.logged_at), desc(lg.c.id))
                      .limit(limit).offset(offset))


def log_level_counts(wh: Warehouse, days: int = 7, now: datetime | None = None) -> pd.DataFrame:
    lg = s.pipeline_log_records
    return wh.read_df(select(lg.c.level, func.count().label("n"))
                      .where(lg.c.logged_at >= (now or utcnow()) - timedelta(days=days)).group_by(lg.c.level))


# ====================================================================== catalog / lakehouse
def _pg_stats(wh: Warehouse) -> pd.DataFrame:
    q = text("SELECT c.relname AS table_name, c.reltuples::bigint AS est_rows, "
             "pg_total_relation_size(c.oid) AS size_bytes FROM pg_class c JOIN pg_namespace n "
             "ON n.oid = c.relnamespace WHERE n.nspname = :ns AND c.relkind = 'r'")
    with wh.connect() as conn:
        res = conn.execute(q, {"ns": s.PG_SCHEMA})
        return pd.DataFrame(res.fetchall(), columns=list(res.keys()))


def dataset_catalog(wh: Warehouse, exact_threshold: int = 200_000) -> pd.DataFrame:
    """Every warehouse table with layer, rows, size, last update and partitioning.

    PostgreSQL: size from pg_total_relation_size; rows exact below ``exact_threshold``
    (planner estimate above it, flagged). SQLite: exact counts, size not measurable per table.
    """
    stats = _pg_stats(wh).set_index("table_name") if wh.is_postgres else pd.DataFrame()
    rows = []
    for t in catalog.tables():
        est = int(stats.est_rows.get(t.name, -1)) if not stats.empty and t.name in stats.index else -1
        size = int(stats.size_bytes.get(t.name)) if not stats.empty and t.name in stats.index else None
        exact = True
        try:
            if est > exact_threshold:
                n, exact = est, False
            else:
                n = int(wh.scalar(select(func.count()).select_from(t)) or 0)
        except Exception:  # noqa: BLE001 - e.g. no privilege
            n, exact = None, False
        last = None
        col = catalog.FRESHNESS_COLUMN.get(t.name)
        if col is not None and col in t.c:
            try:
                last = wh.scalar(select(func.max(t.c[col])))
            except Exception:  # noqa: BLE001
                last = None
        rows.append({"table": t.name, "layer": catalog.layer_of(t.name), "layer_key": catalog.layer_key(t.name),
                     "purpose": catalog.TABLE_DOC.get(t.name, ""), "rows": n, "rows_exact": exact,
                     "size_bytes": size, "last_updated": as_utc(last) if last is not None else None,
                     "partitioning": catalog.PARTITIONING.get(t.name, "—"), "columns": len(t.columns)})
    df = pd.DataFrame(rows)
    order = {"Bronze": 0, "Silver": 1, "Gold": 2, "Operational": 3}
    return df.sort_values(["layer_key", "table"], key=lambda c: c.map(order) if c.name == "layer_key" else c)


def table_schema(table_name: str) -> dict[str, Any]:
    t = s.metadata.tables[table_name]
    pk = {c.name for c in t.primary_key.columns}
    hidden = catalog.HIDDEN_COLUMNS.get(table_name, set())
    cols = pd.DataFrame([{"column": c.name, "type": str(c.type).replace("DATETIME", "TIMESTAMPTZ"),
                          "nullable": c.nullable, "primary_key": c.name in pk,
                          "exposed": c.name not in hidden, "description": catalog.COL_DOC.get(c.name, "")}
                         for c in t.columns])
    idx = [f"{i.name} ({', '.join(c.name for c in i.columns)})" for i in sorted(t.indexes, key=lambda i: i.name)]
    uniq = [", ".join(c.name for c in u.columns) for u in t.constraints if u.__class__.__name__ == "UniqueConstraint"]
    return {"table": table_name, "layer": catalog.layer_of(table_name), "columns": cols, "indexes": idx,
            "unique": uniq, "primary_key": [c.name for c in t.primary_key.columns]}


def table_sample(wh: Warehouse, table_name: str, limit: int = 25, offset: int = 0,
                 run_id: str | None = None) -> pd.DataFrame:
    """A bounded page of rows, newest first; hidden columns never selected; long text truncated."""
    t = s.metadata.tables[table_name]
    hidden = catalog.HIDDEN_COLUMNS.get(table_name, set())
    cols = [c for c in t.columns if c.name not in hidden]
    q = select(*cols)
    if run_id and "run_id" in t.c:
        q = q.where(t.c.run_id == run_id)
    fcol = catalog.FRESHNESS_COLUMN.get(table_name)
    if fcol and fcol in t.c:
        q = q.order_by(desc(t.c[fcol]))
    df = wh.read_df(q.limit(min(limit, 200)).offset(offset))
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(lambda v: (v[:300] + "…") if isinstance(v, str) and len(v) > 300 else v)
    return df


def silver_partitions(wh: Warehouse, limit: int = 48) -> pd.DataFrame:
    so = s.silver_video_observations
    return wh.read_df(select(so.c.run_id, func.max(so.c.observed_at).label("observed_at"),
                             func.count().label("rows"), func.count(func.distinct(so.c.video_id)).label("videos"),
                             func.count(func.distinct(so.c.region_code)).label("regions"))
                      .group_by(so.c.run_id).order_by(desc("observed_at")).limit(limit))


def bronze_by_source(wh: Warehouse, run_id: str | None = None) -> pd.DataFrame:
    b = s.bronze_api_responses
    q = select(b.c.endpoint, b.c.discovery_method, func.count().label("responses"),
               func.sum(b.c.item_count).label("items"), func.max(b.c.fetched_at).label("last_fetched"))
    if run_id:
        q = q.where(b.c.run_id == run_id)
    return wh.read_df(q.group_by(b.c.endpoint, b.c.discovery_method))


# ====================================================================== quality
def latest_dq_results(wh: Warehouse) -> pd.DataFrame:
    d = s.dq_results
    last = wh.scalar(select(d.c.run_id).order_by(desc(d.c.executed_at)).limit(1))
    if last is None:
        return pd.DataFrame(columns=[c.name for c in d.columns])
    return wh.read_df(select(d).where(d.c.run_id == last).order_by(d.c.check_name))


def dq_history(wh: Warehouse, days: int = 7, now: datetime | None = None) -> pd.DataFrame:
    d = s.dq_results
    return wh.read_df(select(d.c.run_id, d.c.executed_at, d.c.check_name, d.c.status, d.c.failed, d.c.warnings,
                             d.c.total_checked)
                      .where(d.c.executed_at >= (now or utcnow()) - timedelta(days=days)).order_by(d.c.executed_at))


def dq_status_by_run(wh: Warehouse, days: int = 7, now: datetime | None = None) -> pd.DataFrame:
    d = s.dq_results
    return wh.read_df(select(d.c.run_id, func.max(d.c.executed_at).label("executed_at"), d.c.status,
                             func.count().label("checks"))
                      .where(d.c.executed_at >= (now or utcnow()) - timedelta(days=days))
                      .group_by(d.c.run_id, d.c.status).order_by("executed_at"))


def failed_checks(wh: Warehouse, days: int = 7, limit: int = 200, now: datetime | None = None) -> pd.DataFrame:
    d = s.dq_results
    return wh.read_df(select(d).where(and_(d.c.status != "PASS",
                                           d.c.executed_at >= (now or utcnow()) - timedelta(days=days)))
                      .order_by(desc(d.c.executed_at)).limit(limit))


def quarantine_page(wh: Warehouse, run_id: str | None = None, check: str | None = None, limit: int = 50,
                    offset: int = 0) -> pd.DataFrame:
    q_ = s.quarantine_records
    conds = []
    if run_id:
        conds.append(q_.c.run_id == run_id)
    if check:
        conds.append(q_.c.reason.like(f"%{check}%"))
    return wh.read_df(select(q_.c.id, q_.c.quarantined_at, q_.c.run_id, q_.c.stage, q_.c.check_name, q_.c.reason,
                             q_.c.record_key).where(and_(True, *conds))
                      .order_by(desc(q_.c.quarantined_at), q_.c.id).limit(limit).offset(offset))


def quarantine_count(wh: Warehouse, run_id: str | None = None, check: str | None = None) -> int:
    q_ = s.quarantine_records
    conds = []
    if run_id:
        conds.append(q_.c.run_id == run_id)
    if check:
        conds.append(q_.c.reason.like(f"%{check}%"))
    return int(wh.scalar(select(func.count()).select_from(q_).where(and_(True, *conds))) or 0)


def quarantine_record(wh: Warehouse, record_id: int) -> dict | None:
    df = wh.read_df(select(s.quarantine_records).where(s.quarantine_records.c.id == record_id))
    if df.empty:
        return None
    rec = df.iloc[0].to_dict()
    try:
        rec["payload_json"] = json.loads(rec["payload"]) if rec.get("payload") else None
    except (TypeError, ValueError):
        rec["payload_json"] = None
    return rec


def schema_drift_history(wh: Warehouse, limit: int = 48) -> pd.DataFrame:
    d = s.dq_results
    df = wh.read_df(select(d.c.run_id, d.c.executed_at, d.c.status, d.c.total_checked, d.c.details)
                    .where(d.c.check_name == "schema_drift").order_by(desc(d.c.executed_at)).limit(limit))
    if df.empty:
        return df
    det = df.details.map(lambda x: json.loads(x) if x else {})
    df["missing_required"] = det.map(lambda d: json.dumps(d.get("missing_required") or {}))
    df["unexpected_keys"] = det.map(lambda d: json.dumps(d.get("unexpected_top_level_keys") or {}))
    return df.drop(columns=["details"])


def freshness_by_region(wh: Warehouse, settings: Settings, now: datetime | None = None) -> pd.DataFrame:
    now = now or utcnow()
    r = s.dim_regions
    df = wh.read_df(select(r.c.region_code, r.c.country_name, r.c.is_active, r.c.last_ingested_at))
    if df.empty:
        return df
    df["age_minutes"] = (pd.Timestamp(now) - df.last_ingested_at).dt.total_seconds() / 60
    stale = settings.pipeline.quality.stale_after_minutes
    df["freshness"] = df.age_minutes.map(lambda m: "no data" if pd.isna(m) else "fresh"
                                         if m <= settings.pipeline.schedule.interval_minutes * 2
                                         else "delayed" if m <= stale else "stale")
    return df


# ====================================================================== lineage
def lineage_graph() -> tuple[pd.DataFrame, pd.DataFrame]:
    edges = pd.DataFrame(dag.dataset_edges(), columns=["source", "target", "transformation"])
    names = sorted(set(edges.source) | set(edges.target))
    def layer(n: str) -> str:
        if n.startswith("source:"):
            return "Source"
        if n == "quarantine_records":       # rejected Silver candidates
            return "Silver"
        return catalog.layer_key(n)
    nodes = pd.DataFrame({"dataset": names, "layer": [layer(n) for n in names]})
    return nodes, edges


def lineage_for(dataset: str) -> dict[str, list[str]]:
    _, e = lineage_graph()
    def walk(start: str, forward: bool) -> list[str]:
        seen, frontier = set(), [start]
        while frontier:
            cur = frontier.pop()
            nxt = e[e.source == cur].target if forward else e[e.target == cur].source
            for n in nxt:
                if n not in seen:
                    seen.add(n)
                    frontier.append(n)
        return sorted(seen)
    return {"upstream": walk(dataset, False), "downstream": walk(dataset, True)}


def lineage_run_metrics(wh: Warehouse, run_id: str) -> pd.DataFrame:
    """Per-transformation metrics for one run: the stage that produced each dataset edge."""
    _, e = lineage_graph()
    st = dag_state(wh, run_id).set_index("stage")
    run = run_row(wh, run_id) or {}
    rows = []
    for r in e.itertuples(index=False):
        stage = r.transformation.split("+")[-1]
        m = st.loc[stage] if stage in st.index else None
        rows.append({"source": r.source, "target": r.target, "transformation": r.transformation, "run_id": run_id,
                     "status": None if m is None else m.status, "processed_at": None if m is None else m.finished_at,
                     "records_in": None if m is None else m.records_in,
                     "records_out": None if m is None else m.records_out,
                     "rejected": None if m is None else m.rejected,
                     "duration_ms": None if m is None else m.duration_ms, "dq_status": run.get("dq_status")})
    return pd.DataFrame(rows)


# ====================================================================== observability
def observability(wh: Warehouse, days: int = 7, now: datetime | None = None) -> pd.DataFrame:
    """Per-run operational metrics (throughput, retries, rejection rate)."""
    pr, sr = s.pipeline_runs, s.pipeline_stage_runs
    since = (now or utcnow()) - timedelta(days=days)
    retries = (select(sr.c.run_id, func.sum(_retries_expr()).label("retries"),
                      func.sum(case_failed()).label("failed_stages")).group_by(sr.c.run_id).subquery())
    df = wh.read_df(select(pr.c.run_id, pr.c.started_at, pr.c.status, pr.c.duration_seconds, pr.c.records_silver,
                           pr.c.records_quarantined, pr.c.quota_used, func.coalesce(retries.c.retries, 0).label("retries"),
                           func.coalesce(retries.c.failed_stages, 0).label("failed_stages"))
                    .select_from(pr.outerjoin(retries, retries.c.run_id == pr.c.run_id))
                    .where(pr.c.started_at >= since).order_by(pr.c.started_at))
    if df.empty:
        return df
    cand = df.records_silver.fillna(0) + df.records_quarantined.fillna(0)
    df["rejection_rate"] = (df.records_quarantined / cand.where(cand > 0)).astype(float)
    df["throughput_rps"] = (df.records_silver / df.duration_seconds.where(df.duration_seconds > 0)).astype(float)
    return df


def _retries_expr():
    from sqlalchemy import case
    a = s.pipeline_stage_runs.c.attempts
    return case((a > 1, a - 1), else_=0)


def case_failed():
    from sqlalchemy import case
    return case((s.pipeline_stage_runs.c.status == "failed", 1), else_=0)


def stage_duration_trend(wh: Warehouse, days: int = 2, now: datetime | None = None) -> pd.DataFrame:
    sr = s.pipeline_stage_runs
    return wh.read_df(select(sr.c.run_id, sr.c.stage, sr.c.stage_order, sr.c.started_at, sr.c.duration_ms,
                             sr.c.status, sr.c.attempts)
                      .where(sr.c.started_at >= (now or utcnow()) - timedelta(days=days)).order_by(sr.c.started_at))


# ====================================================================== recovery
@dataclass
class Action:
    key: str
    label: str
    available: bool
    reason: str
    cli: str | None


def bronze_available(wh: Warehouse, run_id: str) -> int:
    b = s.bronze_api_responses
    try:
        return int(wh.scalar(select(func.count()).select_from(b).where(b.c.run_id == run_id)) or 0)
    except Exception:  # noqa: BLE001 - reader without Bronze privilege
        return -1


def recovery_actions(run: dict, bronze_rows: int, operator: bool, dispatch_configured: bool,
                     settings: Settings) -> list[Action]:
    """What can actually be done for this execution, and why not otherwise.

    Only backed operations are offered: replay from Bronze (``trendpulse replay``) and a new run
    (``trendpulse run``), both executed by the authenticated GitHub workflow — never in the
    dashboard process.
    """
    rid = run["run_id"]
    status = run["status"]
    gate = None if operator and dispatch_configured else (
        "operator sign-in required" if dispatch_configured else
        "dispatch not configured on this deployment — use the CLI")
    replayable = status in {"failed", "dq_failed", "abandoned", "partial"}
    replay_reason = ""
    if not replayable:
        replay_reason = f"status '{status}' does not need recovery"
    elif bronze_rows == 0:
        replay_reason = "no Bronze data (skipped run, or replay window of " \
                        f"{settings.pipeline.retention.bronze_days} days has passed)"
    elif bronze_rows < 0:
        replay_reason = "Bronze row count not visible to this role; replay can still be dispatched"
    replay_ok = replayable and bronze_rows != 0 and gate is None
    out = [
        Action("replay", "Replay from Bronze (re-run stages 8–17)", replay_ok,
               replay_reason or (gate or "idempotent: facts keyed by run_id, previous Gold kept on failure"),
               f"trendpulse replay --run-id {rid}"),
        Action("reconcile", "Reconcile interrupted execution", status == "abandoned" and bronze_rows != 0 and gate is None,
               ("only for abandoned runs" if status != "abandoned" else (gate or "replays the abandoned run's Bronze")),
               f"trendpulse replay --run-id {rid}" if status == "abandoned" else None),
        Action("run", "Start a new execution", gate is None,
               gate or ("live ingestion disabled pending compliance approval — run will be recorded as skipped"
                        if not settings.pipeline.sources.youtube.live_ingestion_enabled else "dispatches ingest.yml"),
               "trendpulse run"),
        Action("retry_stage", "Retry a single stage", False,
               "not supported by the orchestrator: stages share in-memory state; replay re-executes "
               "processing stages idempotently instead", None),
        Action("backfill", "Historical backfill", False,
               "not possible: the YouTube Data API exposes current charts/statistics only", None),
    ]
    return out


def valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_RE.match(run_id or ""))


def api_calls_for_run(wh: Warehouse, run_id: str, only_issues: bool = False, limit: int = 200) -> pd.DataFrame:
    """API request log of one run (retries = attempt > 1, errors = status >= 400 or no response)."""
    al = s.api_request_log
    q = select(al.c.requested_at, al.c.endpoint, al.c.region_code, al.c.discovery_method, al.c.http_status,
               al.c.attempt, al.c.quota_cost, al.c.items_returned, al.c.duration_ms, al.c.error
               ).where(al.c.run_id == run_id)
    if only_issues:
        q = q.where(or_(al.c.attempt > 1, al.c.http_status >= 400, al.c.http_status.is_(None)))
    return wh.read_df(q.order_by(al.c.requested_at).limit(limit))


def api_retry_stats(wh: Warehouse, days: int = 7, now: datetime | None = None) -> pd.DataFrame:
    al = s.api_request_log
    from sqlalchemy import case
    return wh.read_df(select(al.c.run_id, func.min(al.c.requested_at).label("requested_at"),
                             func.count().label("calls"),
                             func.sum(case((al.c.attempt > 1, 1), else_=0)).label("retries"),
                             func.sum(case((or_(al.c.http_status >= 400, al.c.http_status.is_(None)), 1),
                                           else_=0)).label("errors"))
                      .where(al.c.requested_at >= (now or utcnow()) - timedelta(days=days))
                      .group_by(al.c.run_id).order_by("requested_at"))


def rows_per_run(wh: Warehouse, table_name: str, limit: int = 24) -> pd.DataFrame:
    t = s.metadata.tables[table_name]
    if "run_id" not in t.c:
        return pd.DataFrame()
    fcol = catalog.FRESHNESS_COLUMN.get(table_name)
    cols = [t.c.run_id, func.count().label("rows")]
    if fcol and fcol in t.c:
        cols.append(func.max(t.c[fcol]).label("latest"))
    q = select(*cols).group_by(t.c.run_id)
    q = q.order_by(desc("latest")) if fcol and fcol in t.c else q
    return wh.read_df(q.limit(limit))
