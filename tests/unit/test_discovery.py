from datetime import timedelta

import responses
from sqlalchemy import select

from tests.helpers import NOW, item
from trendpulse.ingestion.bronze import BronzeWriter
from trendpulse.ingestion.discovery import (
    STATE_LAST_SEARCH,
    DiscoveryContext,
    discover_regional_charts,
    discover_search,
    refresh_tracked_metrics,
    rotation_pairs,
    search_due,
)
from trendpulse.ingestion.quota import QuotaTracker
from trendpulse.ingestion.youtube_client import BASE_URL, YouTubeClient
from trendpulse.storage import schema as s


def ctx_for(settings, wh, used=0):
    q = QuotaTracker(settings.pipeline.quota, used_before_run=used)
    c = YouTubeClient("k", settings.pipeline.ingestion, q, sleep=lambda x: None)
    return DiscoveryContext(settings, c, BronzeWriter(wh), wh, "run-1", NOW), q


def test_rotation_interleaves_seeds_and_respects_region_lists(settings):
    pairs = rotation_pairs(settings)
    first = [seed.id for seed, _ in pairs[:6]]
    assert len(set(first)) == 6                               # consecutive slots = different seeds
    assert all(r in {"IN", "GB", "AU"} for seed, r in pairs if seed.id == "cricket")
    assert len(pairs) == len(set((sd.id, r) for sd, r in pairs))


@responses.activate
def test_charts_cover_regions_categories_and_tolerate_missing_charts(settings, wh):
    settings.countries.countries = settings.countries.countries[:2]
    def cb(req):
        if "videoCategoryId=20" in req.url and "regionCode=US" in req.url:
            return (404, {}, '{"error":{"errors":[{"reason":"videoChartNotFound"}]}}')
        return (200, {}, '{"items":[{"id":"a"}]}')
    responses.add_callback(responses.GET, f"{BASE_URL}/videos", callback=cb)
    ctx, q = ctx_for(settings, wh)
    res = discover_regional_charts(ctx)
    assert len(res.records) == 5 and not res.errors and not res.partial  # 2 regions x 3 charts - 1 missing
    assert {r.discovery_query for r in res.records} == {"chart:all", "chart:10", "chart:20"}
    assert q.used_this_run == 6


@responses.activate
def test_charts_stop_cleanly_at_quota_budget(settings, wh):
    responses.get(f"{BASE_URL}/videos", json={"items": []})
    ctx, _ = ctx_for(settings, wh, used=settings.pipeline.quota.daily_budget - 4)
    res = discover_regional_charts(ctx)
    assert res.quota_stopped and len(res.records) == 4


@responses.activate
def test_search_cycle_enriches_tracks_and_advances_cursor(settings, wh):
    settings.pipeline.ingestion.searches_per_cycle = 2
    responses.get(f"{BASE_URL}/search", json={"items": [{"id": {"videoId": "v1"}}, {"id": {"videoId": "v2"}}]})
    responses.get(f"{BASE_URL}/videos", json={"items": [item("v1"), item("v2")]})
    ctx, q = ctx_for(settings, wh)
    assert search_due(ctx)
    res, ids = discover_search(ctx)
    assert ids == {"v1", "v2"} and res.search_ran
    assert q.by_endpoint == {"search.list": 200, "videos.list": 2}
    tracked = wh.read_df(select(s.tracked_videos))
    assert len(tracked) == 4 and (tracked.track_until > NOW + timedelta(hours=71)).all()
    assert wh.get_state("search_rotation_cursor") == "2"
    assert not search_due(ctx)                                      # interval not elapsed
    ctx.now = NOW + timedelta(minutes=settings.pipeline.ingestion.search_interval_minutes)
    assert search_due(ctx)
    assert wh.get_state(STATE_LAST_SEARCH) is not None


@responses.activate
def test_search_never_spends_the_refresh_reserve(settings, wh):
    cfg = settings.pipeline.quota
    ctx, _ = ctx_for(settings, wh, used=cfg.daily_budget - cfg.refresh_reserve - 50)
    res, _ = discover_search(ctx)
    assert res.quota_stopped and len(responses.calls) == 0


@responses.activate
def test_refresh_batches_tracked_ids_and_skips_already_fetched(settings, wh):
    rows = [{"video_id": f"v{i:03d}", "region_code": "US", "discovery_query": "ai", "first_discovered_at": NOW,
             "last_discovered_at": NOW, "track_until": NOW + timedelta(hours=1)} for i in range(120)]
    rows.append({**rows[0], "video_id": "expired", "track_until": NOW - timedelta(minutes=1)})
    with wh.begin() as c:
        wh.insert_many(c, s.tracked_videos, rows)
    responses.get(f"{BASE_URL}/videos", json={"items": []})
    ctx, q = ctx_for(settings, wh)
    res = refresh_tracked_metrics(ctx, skip_ids={"v000", "v001"})
    assert q.by_endpoint["videos.list"] == 3            # 118 ids -> 3 batches of <= 50
    sent = ",".join(c.request.url for c in responses.calls)
    assert "expired" not in sent and "v000" not in sent
    assert res.records[0].request_params["tp_contexts"]["v002"] == [["US", "ai"]]
