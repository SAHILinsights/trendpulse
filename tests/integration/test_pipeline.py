"""End-to-end pipeline tests: real orchestration, real warehouse, offline API simulator."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from tests.conftest import T0
from trendpulse.gold import builder
from trendpulse.ingestion.sample import SampleYouTubeSession
from trendpulse.orchestration.pipeline import LOCK_NAME, STAGES
from trendpulse.storage import schema as s

pytestmark = pytest.mark.integration


def count(wh, table, *where):
    q = select(func.count()).select_from(table)
    for w in where:
        q = q.where(w)
    return wh.scalar(q)


class MutatingSession:
    """Wraps the simulator and corrupts items to exercise quality gates."""

    def __init__(self, inner, mutate):
        self.inner, self.mutate = inner, mutate

    def get(self, url, params=None, timeout=None):
        resp = self.inner.get(url, params, timeout)
        payload = resp.json()
        for i, it in enumerate(payload.get("items", [])):
            self.mutate(i, it)
        return resp


def test_single_run_executes_all_stages_and_publishes(make_pipeline, wh, settings):
    res = make_pipeline().run(trigger="schedule", data_source="sample_fixture")
    assert res.status == "success" and res.gold_published, res.error
    stages = {r["stage"]: r["status"] for r in res.stages}
    assert list(stages) == STAGES
    assert stages["discover_topic_search"] == "success"           # first run: search cycle due
    assert res.counts["quota_used_run"] > 600
    for t in (s.bronze_api_responses, s.silver_video_observations, s.fact_video_observations,
              s.fact_topic_observations, s.dim_videos, s.dim_channels, s.dim_topics, s.dim_regions,
              s.gold_trending_topics, s.gold_topic_growth, s.gold_regional_comparison,
              s.gold_channel_performance, s.gold_category_summary, s.dq_results, s.api_request_log):
        assert count(wh, t) > 0, t.name
    run = wh.read_df(select(s.pipeline_runs)).iloc[0]
    assert run.status == "success" and run.gold_published and run.dq_status in {"PASS", "WARNING"}
    assert count(wh, s.dq_results) == 14
    lifecycle = set(wh.read_df(select(s.gold_trending_topics.c.lifecycle_stage)).lifecycle_stage)
    assert lifecycle == {"baseline"}                     # never claims "emerging" on day one
    assert (settings.data_dir / "status" / "last_run.json").exists()
    assert wh.lock_holder(LOCK_NAME) is None


def test_history_accumulates_and_growth_windows_resolve(make_pipeline, wh, clock):
    pipe = make_pipeline()
    for _ in range(5):
        r = pipe.run(data_source="sample_fixture")
        assert r.status == "success", r.error
        clock.advance(30)
    runs = wh.read_df(select(s.fact_topic_observations.c.run_id).distinct())
    assert len(runs) == 5                                              # append-only history
    g = wh.read_df(select(s.gold_topic_growth))
    w30 = g[g.window == "30m"]
    assert w30.compare_run_id.notna().all() and w30.score_growth_pct.notna().any()
    assert g[g.window == "24h"].compare_run_id.isna().all()             # not enough history: honest NULL
    assert set(g[g.window == "24h"].growth_status) == {"no_history"}
    assert w30.velocity.notna().any() and w30.acceleration.notna().any()
    tt = wh.read_df(select(s.gold_trending_topics))
    assert tt.consecutive_observations.max() == 5
    # search ran once (interval 180 min); refresh kept those videos observed every cycle
    calls = wh.read_df(select(s.api_request_log.c.endpoint))
    assert (calls.endpoint == "search.list").sum() == settings_searches(pipe)


def settings_searches(pipe):
    return pipe.settings.pipeline.ingestion.searches_per_cycle


def test_replay_is_idempotent(make_pipeline, wh, clock):
    pipe = make_pipeline()
    first = pipe.run(data_source="sample_fixture")
    before = (count(wh, s.fact_video_observations), count(wh, s.fact_topic_observations))
    res = pipe.replay(first.run_id)
    assert res.status == "success", res.error
    assert (count(wh, s.fact_video_observations), count(wh, s.fact_topic_observations)) == before
    assert count(wh, s.pipeline_runs) == 1


def test_failed_quality_gate_preserves_previous_gold(make_pipeline, wh, clock):
    good = make_pipeline().run(data_source="sample_fixture")
    snapshot = wh.scalar(select(s.gold_trending_topics.c.snapshot_run_id).limit(1))
    facts = count(wh, s.fact_video_observations)
    clock.advance(30)

    def corrupt(i, it):
        if i % 2 == 0:
            it["statistics"]["viewCount"] = "-1"
    bad_session = MutatingSession(SampleYouTubeSession(clock, epoch=T0 + timedelta(hours=24)), corrupt)
    res = make_pipeline(session=bad_session).run(data_source="sample_fixture")
    assert res.status == "dq_failed" and not res.gold_published
    assert "negative_view_count" in res.error
    assert wh.scalar(select(s.gold_trending_topics.c.snapshot_run_id).limit(1)) == snapshot == good.run_id
    assert count(wh, s.fact_video_observations) == facts                 # no partial history
    assert count(wh, s.quarantine_records, s.quarantine_records.c.run_id == res.run_id) == 0  # gate stopped first
    assert count(wh, s.dq_results, s.dq_results.c.run_id == res.run_id) > 0  # audit still written
    assert count(wh, s.bronze_api_responses, s.bronze_api_responses.c.run_id == res.run_id) > 0
    stages = {r["stage"]: r["status"] for r in res.stages}
    assert stages["publish_gold"] == "skipped" and stages["update_execution_metadata"] == "success"


def test_crash_during_publication_rolls_back_and_replay_recovers(make_pipeline, wh, clock, monkeypatch):
    make_pipeline().run(data_source="sample_fixture")
    clock.advance(30)
    prev_snapshot = wh.scalar(select(s.gold_trending_topics.c.snapshot_run_id).limit(1))
    facts = count(wh, s.fact_topic_observations)
    real = builder.build_serving

    def boom(*a, **k):
        raise RuntimeError("simulated crash mid-publication")
    monkeypatch.setattr(builder, "build_serving", boom)
    res = make_pipeline().run(data_source="sample_fixture")
    assert res.status == "failed" and "simulated crash" in res.error
    assert wh.scalar(select(s.gold_trending_topics.c.snapshot_run_id).limit(1)) == prev_snapshot
    assert count(wh, s.fact_topic_observations) == facts                 # facts rolled back too
    monkeypatch.setattr(builder, "build_serving", real)
    rec = make_pipeline().replay(res.run_id)
    assert rec.status == "success", rec.error
    assert wh.scalar(select(s.gold_trending_topics.c.snapshot_run_id).limit(1)) == res.run_id
    row = wh.read_df(select(s.pipeline_runs).where(s.pipeline_runs.c.run_id == res.run_id)).iloc[0]
    assert row.status == "success" and row.trigger == "replay"


def test_transient_db_errors_are_retried(make_pipeline, monkeypatch):
    from sqlalchemy.exc import OperationalError

    pipe = make_pipeline()
    calls = {"n": 0}
    orig = pipe._quarantine

    def flaky(st):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("stmt", {}, Exception("connection reset"))
        return orig(st)
    monkeypatch.setattr(pipe, "_quarantine", flaky)
    res = pipe.run(data_source="sample_fixture")
    assert res.status == "success"
    assert next(r for r in res.stages if r["stage"] == "quarantine_records")["attempts"] == 2


def test_overlapping_runs_are_prevented(make_pipeline, wh):
    assert wh.acquire_lock(LOCK_NAME, "someone-else", timedelta(minutes=30))
    res = make_pipeline().run(data_source="sample_fixture")
    assert res.status == "skipped_locked" and count(wh, s.pipeline_runs) == 0


def test_quota_exhaustion_skips_safely(make_pipeline, wh, settings, clock):
    with wh.begin() as c:
        wh.insert_many(c, s.api_request_log, [{"endpoint": "search.list", "quota_cost": settings.pipeline.quota.daily_budget,
                                               "requested_at": clock()}])
    res = make_pipeline().run(data_source="sample_fixture")
    assert res.status == "skipped" and not res.gold_published
    assert count(wh, s.bronze_api_responses) == 0


def test_partial_ingestion_still_publishes(make_pipeline, wh, settings, clock):
    with wh.begin() as c:   # leave room for only a handful of chart calls
        wh.insert_many(c, s.api_request_log, [{"endpoint": "videos.list",
                                               "quota_cost": settings.pipeline.quota.daily_budget - 12,
                                               "requested_at": clock()}])
    res = make_pipeline().run(data_source="sample_fixture")
    assert res.status == "partial" and res.gold_published
    assert res.counts["quota_used_run"] == 12


def test_live_mode_without_key_fails_clearly(make_pipeline):
    res = make_pipeline().run(data_source="youtube_api", api_key="")
    assert res.status == "failed" and "YOUTUBE_API_KEY" in res.error


def test_stale_running_rows_are_marked_abandoned(make_pipeline, wh, clock):
    with wh.begin() as c:
        c.execute(s.pipeline_runs.insert().values(run_id="ghost", trigger="schedule", data_source="youtube_api",
                                                  status="running", started_at=clock() - timedelta(days=1),
                                                  gold_published=False))
    make_pipeline().run(data_source="sample_fixture")
    assert wh.scalar(select(s.pipeline_runs.c.status).where(s.pipeline_runs.c.run_id == "ghost")) == "abandoned"


def test_malformed_items_are_quarantined_not_dropped(make_pipeline, wh, clock):
    def drop_channel(i, it):
        if i == 3:
            it["snippet"].pop("channelId", None)
    sess = MutatingSession(SampleYouTubeSession(clock, epoch=T0 + timedelta(hours=24)), drop_channel)
    res = make_pipeline(session=sess).run(data_source="sample_fixture")
    assert res.status == "success" and res.counts["quarantined"] > 0
    q = wh.read_df(select(s.quarantine_records))
    assert set(q.check_name) == {"missing_channel_id"} and q.payload.str.contains('"id"').all()
