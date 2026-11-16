"""Control-plane queries and pages against a warehouse populated by real (simulated) executions,
including a quality-gate failure, an API outage (partial run) and flaky requests."""

import os
from datetime import timedelta

import pandas as pd
import pytest
from sqlalchemy import select, text

from tests.conftest import T0
from trendpulse.dashboard import data as d
from trendpulse.dashboard import ops
from trendpulse.ingestion.sample import SampleYouTubeSession
from trendpulse.storage import schema as s

pytestmark = pytest.mark.integration


@pytest.fixture
def plane(settings, wh, clock):
    """Five executions: success, flaky (retries), dq_failed (corrupt metrics), partial (JP outage), success."""
    from trendpulse.orchestration.pipeline import Pipeline
    settings.pipeline.ingestion.max_retries = 1
    sess = SampleYouTubeSession(clock, epoch=T0 + timedelta(hours=24))
    pipe = Pipeline(settings, wh, session_factory=lambda st: sess, clock=clock, sleep=lambda x: None, notify=False)
    out = {}
    for mode in (None, "flaky", "corrupt_metrics", "outage", None):
        sess.mode = mode
        res = pipe.run(trigger=f"simulation:{mode}" if mode else "simulation", data_source="sample_fixture")
        out[mode or f"ok{len(out)}"] = res
        clock.advance(30)
    return wh, out


def test_scenario_produced_the_expected_real_outcomes(plane):
    _, out = plane
    assert out["flaky"].status == "success"
    assert out["corrupt_metrics"].status == "dq_failed"
    assert out["outage"].status == "partial"


def test_runs_filters_pagination_and_derived_columns(plane, clock):
    wh, out = plane
    all_runs = ops.runs(wh)
    assert len(all_runs) == 5 == ops.run_count(wh)
    assert (all_runs.retries >= 0).all()
    failed = ops.runs(wh, ["dq_failed"])
    assert list(failed.run_id) == [out["corrupt_metrics"].run_id] and ops.run_count(wh, ["dq_failed"]) == 1
    assert failed.failed_stage.iloc[0] == "data_quality"
    warn = set(ops.runs(wh, ["warning"]).run_id)
    assert out["outage"].run_id in warn                                  # partial counts as warning
    page2 = ops.runs(wh, limit=2, offset=2)
    assert len(page2) == 2 and set(page2.run_id).isdisjoint(set(ops.runs(wh, limit=2).run_id))
    assert ops.run_count(wh, since=clock() + timedelta(days=1)) == 0


def test_dag_state_reflects_recorded_stage_outcomes(plane):
    wh, out = plane
    ok = ops.dag_state(wh, out["ok0"].run_id).set_index("stage")
    assert len(ok) == 17 and (ok.status.isin(["success", "warning", "skipped"])).all()
    assert ok.loc["publish_gold", "status"] == "success" and ok.loc["publish_gold", "records_out"] > 0
    bad = ops.dag_state(wh, out["corrupt_metrics"].run_id).set_index("stage")
    assert bad.loc["data_quality", "status"] == "failed"
    assert bad.loc["quarantine_records", "status"] == "success"           # gate FAIL still quarantines
    assert bad.loc["publish_gold", "status"] == "skipped"
    assert bad.loc["transform_silver", "rejected"] > 0
    assert "critical data-quality gate failed" in bad.loc["data_quality", "detail"]
    partial = ops.dag_state(wh, out["outage"].run_id).set_index("stage")
    assert partial.loc["discover_regional_charts", "status"] == "partial"
    assert "JP" in partial.loc["discover_regional_charts", "detail"]


def test_running_and_abandoned_runs_are_represented_honestly(plane, clock):
    wh, _ = plane
    rid_run, rid_ab = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    with wh.begin() as c:
        for rid, status in ((rid_run, "running"), (rid_ab, "abandoned")):
            c.execute(s.pipeline_runs.insert().values(run_id=rid, trigger="schedule", data_source="youtube_api",
                                                      status=status, started_at=clock(), gold_published=False))
            c.execute(s.pipeline_stage_runs.insert().values(run_id=rid, stage="start_execution", stage_order=0,
                                                            status="success", started_at=clock(), attempts=1))
    r = ops.dag_state(wh, rid_run).set_index("stage")
    assert r.loc["load_configuration", "status"] == "running" and r.loc["publish_gold", "status"] == "pending"
    a = ops.dag_state(wh, rid_ab).set_index("stage")
    assert a.loc["load_configuration", "status"] == "interrupted"
    assert a.loc["publish_gold", "status"] == "not_recorded"


def test_log_search_filters(plane):
    wh, out = plane
    rid = out["corrupt_metrics"].run_id
    all_logs = ops.run_logs(wh, run_id=rid, limit=1000)
    assert not all_logs.empty and set(all_logs.run_id) == {rid}
    errs = ops.run_logs(wh, run_id=rid, levels=["ERROR"])
    assert errs.empty or set(errs.level) == {"ERROR"}
    warn = ops.run_logs(wh, levels=["WARNING"], search="503")
    assert not warn.empty and warn.message.str.contains("503").all()
    stage = ops.run_logs(wh, stage="publish_gold")
    assert set(stage.stage) == {"publish_gold"}
    assert len(ops.run_logs(wh, limit=3)) == 3
    first, second = ops.run_logs(wh, limit=2), ops.run_logs(wh, limit=2, offset=2)
    assert set(first.id).isdisjoint(second.id)
    late = all_logs.logged_at.max() + pd.Timedelta(seconds=1)
    assert ops.run_logs(wh, run_id=rid, since=late.to_pydatetime()).empty
    assert not ops.run_logs(wh, run_id=rid, until=late.to_pydatetime()).empty
    assert not ops.log_level_counts(wh, days=10000).empty


def test_catalog_schema_and_bounded_samples(plane):
    wh, out = plane
    cat = ops.dataset_catalog(wh).set_index("table")
    assert set(cat.index) == set(s.metadata.tables)
    assert cat.loc["fact_topic_observations", "rows"] == wh.scalar(
        select(__import__("sqlalchemy").func.count()).select_from(s.fact_topic_observations))
    assert cat.loc["bronze_api_responses", "layer_key"] == "Bronze"
    assert cat.loc["gold_trending_topics", "layer_key"] == "Gold" and cat.loc["dq_results", "layer_key"] == "Operational"
    assert pd.notna(cat.loc["silver_video_observations", "last_updated"])
    sch = ops.table_schema("bronze_api_responses")
    cols = sch["columns"].set_index("column")
    assert not cols.loc["payload_gz", "exposed"] and cols.loc["response_id", "primary_key"]
    sample = ops.table_sample(wh, "bronze_api_responses", 5)
    assert "payload_gz" not in sample.columns and len(sample) == 5
    assert len(ops.table_sample(wh, "fact_video_observations", 10_000)) <= 200          # hard cap
    rid = out["ok0"].run_id
    assert set(ops.table_sample(wh, "silver_video_observations", 20, run_id=rid).run_id) == {rid}
    parts = ops.silver_partitions(wh)
    assert len(parts) == 5 and (parts.rows > 0).all()                     # every run persisted Silver (incl. dq_failed)
    assert not ops.rows_per_run(wh, "fact_topic_observations").empty
    assert ops.rows_per_run(wh, "dim_videos").empty
    assert set(ops.bronze_by_source(wh).discovery_method) >= {"regional_chart", "search"}


def test_quality_queries(plane):
    wh, out = plane
    latest = ops.latest_dq_results(wh)
    assert len(latest) == 14
    fc = ops.failed_checks(wh, days=10000, now=None)
    bad = fc[fc.run_id == out["corrupt_metrics"].run_id]
    assert "FAIL" in set(bad.status) and "negative_view_count" in set(bad.check_name)
    rid = out["corrupt_metrics"].run_id
    n = ops.quarantine_count(wh, run_id=rid)
    assert n > 0 and n == len(ops.quarantine_page(wh, run_id=rid, limit=10_000))
    assert ops.quarantine_count(wh, run_id=rid, check="negative_view_count") == n
    rec = ops.quarantine_record(wh, int(ops.quarantine_page(wh, run_id=rid, limit=1).id.iloc[0]))
    assert rec["payload_json"]["statistics"]["viewCount"] == "-1"
    assert not ops.schema_drift_history(wh).empty
    hist = ops.dq_history(wh, days=10000)
    assert hist.run_id.nunique() == 5
    gate = ops.dq_status_by_run(wh, days=10000)
    assert "FAIL" in set(gate.status)


def test_lineage_and_observability(plane, settings):
    wh, out = plane
    lin = ops.lineage_for("silver_video_observations")
    assert "bronze_api_responses" in lin["upstream"] and "gold_trending_topics" in lin["downstream"]
    assert "silver_video_observations" not in lin["downstream"]
    m = ops.lineage_run_metrics(wh, out["ok0"].run_id)
    row = m[(m.source == "bronze_api_responses") & (m.target == "silver_video_observations")].iloc[0]
    assert row.status == "success" and row.records_out > 0
    ob = ops.observability(wh, days=10000)
    assert len(ob) == 5 and (ob.retries >= 0).all() and ob.rejection_rate.max() > 0
    api = ops.api_retry_stats(wh, days=10000)
    assert api.retries.sum() > 0 and api.errors.sum() > 0               # flaky + outage really retried
    flaky = ops.api_calls_for_run(wh, out["flaky"].run_id, only_issues=True)
    assert not flaky.empty and (flaky.attempt.max() >= 2)
    assert not ops.stage_duration_trend(wh, days=10000).empty


def test_platform_conditions_and_totals(plane, settings, clock):
    wh, out = plane
    h = d.platform_health(wh, settings, now=clock())
    conds = ops.active_conditions(wh, h, settings, now=clock())
    titles = " | ".join(c["title"] for c in conds)
    assert "dq failed" in titles and "Live YouTube ingestion disabled" in titles
    t = ops.totals(wh)
    assert t["runs"] == 5 and t["rejected"] > 0 and t["silver"] > 0
    assert ops.health_status(h, "PASS", settings, clock()).status in {"healthy", "degraded"}
    fr = ops.freshness_by_region(wh, settings, now=clock())
    assert set(fr.freshness) <= {"fresh", "delayed", "stale", "no data"} and len(fr) == 10
    assert ops.bronze_available(wh, out["corrupt_metrics"].run_id) > 0


def test_empty_warehouse_is_handled(wh, settings):
    assert ops.runs(wh).empty and ops.run_count(wh) == 0
    assert ops.run_logs(wh).empty and ops.latest_dq_results(wh).empty
    assert ops.health_status(d.platform_health(wh, settings), None, settings).status == "not_initialized"
    assert ops.active_conditions(wh, None, settings) == []
    cat = ops.dataset_catalog(wh)
    assert (cat.rows.fillna(0) == cat.rows.fillna(0)).all() and cat.rows.max() <= 2   # only migration rows
    assert ops.observability(wh).empty and ops.quarantine_count(wh) == 0


# ------------------------------------------------------------------ pages
def _engineering_page(module: str):
    import importlib

    import streamlit as st

    from trendpulse.dashboard import components as ui
    from trendpulse.dashboard.runtime import data_mode, q
    ui.inject_css()
    health = q("platform_health")
    st.session_state["health"] = health
    st.session_state["data_mode"] = data_mode(health)
    importlib.import_module(f"trendpulse.dashboard.views.{module}").render()


MODULES = ["platform", "orchestrator", "lakehouse", "quality", "lineage", "executions", "intelligence"]


def _app(tmp_path, monkeypatch, src_db, module):
    testing = pytest.importorskip("streamlit.testing.v1")
    import shutil

    from trendpulse.settings import get_settings
    if src_db is not None:   # copy WAL side files too: uncheckpointed pages live there
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(f"{src_db}{suffix}"):
                shutil.copy(f"{src_db}{suffix}", tmp_path / f"trendpulse.db{suffix}")
    monkeypatch.setenv("TRENDPULSE_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    import streamlit as st
    st.cache_data.clear()
    st.cache_resource.clear()
    app = testing.AppTest.from_function(_engineering_page, args=(module,), default_timeout=120)
    app.run()
    get_settings.cache_clear()
    return app


@pytest.mark.parametrize("module", MODULES)
def test_engineering_modules_render(plane, tmp_path, monkeypatch, module):
    wh, _ = plane
    if wh.is_postgres:
        pytest.skip("page smoke tests copy the SQLite fixture")
    app = _app(tmp_path, monkeypatch, wh.engine.url.database, module)
    assert not app.exception, [e.value for e in app.exception]
    text_ = " ".join(m.value for m in app.markdown)
    assert "SAMPLE" in text_


@pytest.mark.parametrize("module", ["platform", "orchestrator", "executions"])
def test_engineering_modules_empty_state(tmp_path, monkeypatch, module):
    from trendpulse.storage.db import Warehouse
    from trendpulse.storage.migrations import migrate
    db = tmp_path / "src.db"
    migrate(Warehouse(f"sqlite:///{db}"))
    (tmp_path / "x").mkdir()
    app = _app(tmp_path / "x", monkeypatch, db, module)
    assert not app.exception
    assert "not initialized" in " ".join(m.value for m in app.markdown).lower()


def test_engineering_module_error_state(monkeypatch):
    testing = pytest.importorskip("streamlit.testing.v1")

    def script():
        import streamlit as st

        from trendpulse.dashboard.runtime import data_mode
        from trendpulse.dashboard.views import orchestrator
        st.session_state["data_mode"] = data_mode(None, "OperationalError: connection refused")
        st.session_state["health"] = None
        orchestrator.render()
    app = testing.AppTest.from_function(script, default_timeout=60)
    app.run()
    assert not app.exception
    assert "Cannot reach the warehouse" in " ".join(m.value for m in app.markdown)


# ------------------------------------------------------------------ role-based access (PostgreSQL only)
def test_reader_role_is_read_only_and_cannot_see_raw_payloads(plane):
    wh, _ = plane
    if not wh.is_postgres:
        pytest.skip("requires PostgreSQL")
    with wh.connect() as c:
        if not c.execute(text("select rolsuper from pg_roles where rolname = current_user")).scalar():
            pytest.skip("needs a superuser test connection to create roles")
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "migrations", "roles.sql")) as fh:
        roles = fh.read()
    roles = roles.replace("CHANGE_ME_INGEST_PASSWORD", "ingest-test-pw").replace("CHANGE_ME_READER_PASSWORD",
                                                                                 "reader-test-pw")
    with wh.engine.connect() as c:
        raw = c.connection.dbapi_connection
        raw.autocommit = True
        with raw.cursor() as cur:
            cur.execute(roles)
            cur.execute("ALTER ROLE trendpulse_reader PASSWORD 'reader-test-pw'")
    from trendpulse.storage.db import Warehouse
    url = wh.engine.url.set(username="trendpulse_reader", password="reader-test-pw")
    reader = Warehouse(url.render_as_string(hide_password=False), read_only=True)
    cat = ops.dataset_catalog(reader).set_index("table")
    assert cat.loc["bronze_api_responses", "rows"] > 0                    # metadata via column grant
    assert "payload_gz" not in ops.table_sample(reader, "bronze_api_responses", 2).columns
    for sql in ("select payload_gz from trendpulse.bronze_api_responses limit 1",
                "insert into trendpulse.discovery_state values ('x','y',now())",
                "delete from trendpulse.pipeline_runs"):
        with pytest.raises(Exception), reader.engine.begin() as c:  # noqa: B017
            c.execute(text(sql))
    reader.dispose()
