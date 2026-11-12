"""Dashboard data-access layer, alerting, operator trigger and CLI."""

import json
from datetime import timedelta

import pytest
import responses

from trendpulse import cli
from trendpulse.dashboard import data as d
from trendpulse.dashboard import trigger
from trendpulse.monitoring.alerts import send_alert
from trendpulse.settings import Secrets

pytestmark = pytest.mark.integration


@pytest.fixture
def populated(make_pipeline, wh, clock):
    pipe = make_pipeline()
    for _ in range(4):
        assert pipe.run(data_source="sample_fixture").status == "success"
        clock.advance(30)
    return wh


def test_every_dashboard_query_runs_on_a_populated_warehouse(populated, settings, clock):
    wh, now = populated, clock()
    top = d.trending(wh)
    assert not top.empty and list(top.popularity_rank)[:3] == [1, 2, 3]
    tid = top.topic_id.iloc[0]
    checks = {
        "publication": d.publication(wh), "data_sources": d.data_sources(wh), "recent_runs": d.recent_runs(wh),
        "stage_duration_stats": d.stage_duration_stats(wh), "throughput": d.throughput(wh, now=now),
        "quota_usage": d.quota_usage(wh, settings, now=now), "dq_results": d.dq_results(wh, now=now),
        "regions": d.regions(wh), "growth": d.growth(wh, "GLOBAL", "30m"), "topics": d.topics(wh),
        "topic_history": d.topic_history(wh, tid, "GLOBAL", now=now),
        "top_topics_history": d.top_topics_history(wh, now=now), "topic_videos": d.topic_videos(wh, tid, "US"),
        "topic_regions": d.topic_regions(wh, tid), "regional_comparison": d.regional_comparison(wh),
        "category_summary": d.category_summary(wh), "channels": d.channels(wh), "videos": d.videos(wh, now=now),
        "region_activity": d.region_activity(wh), "growth_all_regions": d.growth_all_regions(wh, "30m"),
    }
    for name, df in checks.items():
        assert df is not None and (not hasattr(df, "empty") or not df.empty), name
    assert len(d.topic_history(wh, tid, "GLOBAL", now=now)) == 4
    assert not d.videos(wh, search="sample", now=now).empty or not d.videos(wh, now=now).empty
    vid = d.videos(wh, now=now).video_id.iloc[0]
    assert len(d.video_history(wh, vid)) >= 1
    ch = d.channels(wh).channel_id.iloc[0]
    assert not d.channel_topics(wh, ch).empty and not d.channel_regions(wh, [ch]).empty
    assert d.related_topics(wh, tid) is not None and d.review_queue(wh) is not None
    assert d.quarantine(wh) is not None and d.api_errors(wh) is not None
    assert d.video_categories(wh)
    c = d.overview_counts(wh, now=now)
    assert c["regions_monitored"] == 10 and c["active_topics"] == len(top)
    counts = d.warehouse_counts(wh)
    assert counts["fact_topic_observations"] > 0


def test_platform_health_distinguishes_attempt_success_and_freshness(populated, settings, clock):
    h = d.platform_health(populated, settings, now=clock())
    assert h["last_attempt"]["run_id"] == h["last_success"]["run_id"]
    assert h["freshness"] == "fresh" and h["data_source"] == "sample_fixture"
    assert h["success_rate_7d"] == 1.0 and h["quota_used_today"] > 0
    stale = d.platform_health(populated, settings, now=clock() + timedelta(days=2))
    assert stale["freshness"] == "stale"


def test_data_mode_mapping():
    pytest.importorskip("streamlit")
    from trendpulse.dashboard.runtime import data_mode
    assert data_mode(None).key == "empty"
    assert data_mode({"data_source": "sample_fixture", "freshness": "fresh"}).key == "sample"
    assert data_mode({"data_source": "youtube_api", "freshness": "fresh"}).key == "live"
    assert data_mode({"data_source": "youtube_api", "freshness": "stale"}).key == "historical"
    assert data_mode(None, "boom").key == "error"


@responses.activate
def test_alerts_webhook_and_disabled():
    assert send_alert(Secrets(), "s", "b") == []
    responses.post("https://hooks.example.com/x", status=200)
    assert send_alert(Secrets(alert_webhook_url="https://hooks.example.com/x"), "Pipeline FAILED", "x") == ["webhook"]
    body = json.loads(responses.calls[0].request.body)
    assert "Pipeline FAILED" in body["text"]


@responses.activate
def test_operator_trigger_requires_all_secrets_and_correct_passphrase():
    assert not trigger.enabled("tok", "me/repo", None)
    assert trigger.check_passphrase("open sesame", "open sesame")
    assert not trigger.check_passphrase("guess", "open sesame")
    responses.post("https://api.github.com/repos/me/repo/actions/workflows/ingest.yml/dispatches", status=204)
    ok, _ = trigger.dispatch("tok", "me/repo")
    assert ok and responses.calls[0].request.headers["Authorization"] == "Bearer tok"


def test_cli_validate_config_and_quota_plan(capsys, settings):
    assert cli.main(["validate-config"]) == 0
    plan = cli.quota_plan(settings)
    assert plan["total"] <= settings.pipeline.quota.daily_budget
    assert plan["chart_units_per_run"] == 30 and plan["runs_per_day"] == 48


def test_cli_sample_run_and_postgres_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("TRENDPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRENDPULSE__INGESTION__MIN_SECONDS_BETWEEN_REQUESTS", "0")
    assert cli.main(["run", "--sample"]) == 0
    assert (tmp_path / "trendpulse.db").exists() and (tmp_path / "lake" / "bronze").exists()
    assert cli.main(["status"]) == 0
    assert cli.main(["export-parquet", "--out", str(tmp_path / "gold")]) == 0
    assert (tmp_path / "gold" / "fact_topic_observations.parquet").exists()
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")
    with pytest.raises(SystemExit, match="refusing to write SAMPLE"):
        cli.main(["run", "--sample"])


def _page_script(module: str):
    import importlib

    import streamlit as st

    from trendpulse.dashboard import components as ui
    from trendpulse.dashboard.runtime import data_mode, q
    ui.inject_css()
    health = q("platform_health")
    st.session_state["health"] = health
    st.session_state["data_mode"] = data_mode(health)
    importlib.import_module(f"trendpulse.dashboard.views.{module}").render()


@pytest.mark.parametrize("module", ["overview", "discovery", "topic", "regional", "channels", "videos",
                                    "control_center"])
def test_streamlit_pages_render_without_exceptions(populated, monkeypatch, tmp_path, module):
    testing = pytest.importorskip("streamlit.testing.v1")
    import shutil

    from trendpulse.settings import get_settings
    if populated.is_postgres:
        pytest.skip("page smoke tests copy the SQLite fixture")
    import os

    import streamlit as st
    src = populated.engine.url.database
    for suffix in ("", "-wal", "-shm"):      # uncheckpointed WAL pages must travel with the copy
        if os.path.exists(f"{src}{suffix}"):
            shutil.copy(f"{src}{suffix}", tmp_path / f"trendpulse.db{suffix}")
    monkeypatch.setenv("TRENDPULSE_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    st.cache_data.clear()
    st.cache_resource.clear()
    try:
        app = testing.AppTest.from_function(_page_script, args=(module,), default_timeout=90)
        app.run()
        assert not app.exception, [e.value for e in app.exception]
        assert any("SAMPLE" in m.value for m in app.markdown)       # sample banner is always shown
    finally:
        get_settings.cache_clear()
