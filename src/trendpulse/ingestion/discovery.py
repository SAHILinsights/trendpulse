"""Hybrid discovery engine.

A. Regional popular charts — ``videos.list?chart=mostPopular`` per region,
   uncategorised plus the configured categories (Music, Gaming...). 1 unit
   per page. Since July 2025 this chart reflects YouTube's category charts
   rather than the retired general "Trending" page; we label these
   observations ``regional_chart`` and never call them "the trending list".

B. Topic-oriented search — ``search.list`` (100 units!) over configurable
   seeds. A persisted round-robin cursor walks (seed, region) pairs and only
   ``searches_per_cycle`` run every ``search_interval_minutes``. Results are
   enriched with one ``videos.list`` call (1 unit / 50 ids) and registered in
   ``tracked_videos``.

C. Metric refresh — tracked search-discovered videos are re-measured every
   cycle with ``videos.list`` (1 unit per 50 ids) so their history is
   continuous without paying for search again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from trendpulse.ingestion.bronze import BronzeRecord, BronzeWriter
from trendpulse.ingestion.quota import QuotaBudgetExceeded
from trendpulse.ingestion.youtube_client import (
    NonRetryableError,
    QuotaExhaustedError,
    YouTubeApiError,
    YouTubeClient,
)
from trendpulse.logging_utils import get_logger
from trendpulse.settings import SearchSeed, Settings
from trendpulse.storage import schema as s
from trendpulse.storage.db import Warehouse, as_utc

log = get_logger("discovery")

VIDEO_PARTS = "snippet,statistics,contentDetails"
STATE_SEARCH_CURSOR = "search_rotation_cursor"
STATE_LAST_SEARCH = "search_last_cycle_at"


@dataclass
class DiscoveryResult:
    records: list[BronzeRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    quota_stopped: bool = False
    search_ran: bool = False
    calls: int = 0

    @property
    def partial(self) -> bool:
        return self.quota_stopped or bool(self.errors)

    def merge(self, other: DiscoveryResult) -> None:
        self.records += other.records
        self.errors += other.errors
        self.quota_stopped = self.quota_stopped or other.quota_stopped
        self.search_ran = self.search_ran or other.search_ran
        self.calls += other.calls


@dataclass
class DiscoveryContext:
    settings: Settings
    client: YouTubeClient
    writer: BronzeWriter
    wh: Warehouse
    run_id: str
    now: datetime


def _rfc3339(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(ctx: DiscoveryContext, resp, method: str, query: str, region: str | None,
            category: int | None, page: int, extra_params: dict[str, Any] | None = None) -> BronzeRecord:
    rec = BronzeRecord(
        run_id=ctx.run_id, endpoint=resp.endpoint, discovery_method=method, discovery_query=query,
        region_code=region, category_id=category, page_index=page,
        request_params={**resp.params, **(extra_params or {})}, http_status=resp.status,
        payload=resp.payload, fetched_at=ctx.now,
    )
    ctx.writer.write(rec)
    return rec


# ---------------------------------------------------------------- A. regional charts
def discover_regional_charts(ctx: DiscoveryContext) -> DiscoveryResult:
    res = DiscoveryResult()
    ing = ctx.settings.pipeline.ingestion
    for country in ctx.settings.countries.enabled:
        for cat in [None, *ctx.settings.countries.categories_for(country)]:
            query = f"chart:{cat if cat is not None else 'all'}"
            params: dict[str, Any] = {"part": VIDEO_PARTS, "chart": "mostPopular", "regionCode": country.code,
                                      "maxResults": ing.chart_max_results}
            if cat is not None:
                params["videoCategoryId"] = str(cat)
            try:
                for page, resp in ctx.client.paginate("videos.list", params, ing.chart_pages,
                                                      {"region_code": country.code,
                                                       "discovery_method": "regional_chart"}):
                    res.calls += 1
                    res.records.append(_record(ctx, resp, "regional_chart", query, country.code, cat, page,
                                               {"tp_rank_offset": page * ing.chart_max_results}))
            except (QuotaBudgetExceeded, QuotaExhaustedError) as exc:
                log.warning("quota stop during charts: %s", exc)
                res.quota_stopped = True
                return res
            except NonRetryableError as exc:
                # e.g. a category chart that does not exist in this region — expected, not fatal
                log.info("chart unavailable %s %s: %s", country.code, query, exc)
            except YouTubeApiError as exc:
                res.errors.append(f"chart {country.code} {query}: {exc}")
                log.error("chart %s %s failed: %s", country.code, query, exc)
    return res


# ---------------------------------------------------------------- B. search discovery
def rotation_pairs(settings: Settings) -> list[tuple[SearchSeed, str]]:
    """Interleaved rotation: consecutive slots cycle through *different* seeds
    and staggered regions, so every search cycle covers several topics."""
    enabled = settings.region_codes
    per_seed = [(seed, [r for r in (seed.regions or enabled) if r in enabled]) for seed in settings.seeds.seeds]
    width = max((len(rs) for _, rs in per_seed), default=0)
    pairs = []
    for k in range(width):
        for si, (seed, regions) in enumerate(per_seed):
            if k < len(regions):
                pairs.append((seed, regions[(k + si) % len(regions)]))
    return pairs


def search_due(ctx: DiscoveryContext, force: bool = False) -> bool:
    ing = ctx.settings.pipeline.ingestion
    if not ing.search_enabled or ing.searches_per_cycle == 0:
        return False
    if force:
        return True
    last = ctx.wh.get_state(STATE_LAST_SEARCH)
    if last is None:
        return True
    return ctx.now - as_utc(last) >= timedelta(minutes=ing.search_interval_minutes)


def _fetch_videos(ctx: DiscoveryContext, ids: list[str], method: str, query: str, region: str | None,
                  contexts: dict[str, list[list[str]]], res: DiscoveryResult) -> list[str]:
    fetched = []
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        params = {"part": VIDEO_PARTS, "id": ",".join(batch), "maxResults": 50}
        resp = ctx.client.request("videos.list", params, {"region_code": region, "discovery_method": method})
        res.calls += 1
        res.records.append(_record(ctx, resp, method, query, region, None, i // 50,
                                   {"tp_contexts": {v: contexts[v] for v in batch if v in contexts}}))
        fetched += [it.get("id") for it in resp.payload.get("items", [])]
    return fetched


def discover_search(ctx: DiscoveryContext, force: bool = False) -> tuple[DiscoveryResult, set[str]]:
    res = DiscoveryResult()
    fetched_ids: set[str] = set()
    if not search_due(ctx, force):
        log.info("search discovery not due this cycle")
        return res, fetched_ids
    ing = ctx.settings.pipeline.ingestion
    pairs = rotation_pairs(ctx.settings)
    if not pairs:
        return res, fetched_ids
    cursor = int(ctx.wh.get_state(STATE_SEARCH_CURSOR, "0") or 0) % len(pairs)
    planned = [pairs[(cursor + i) % len(pairs)] for i in range(min(ing.searches_per_cycle, len(pairs)))]
    done = 0
    tracked_rows = []
    try:
        for seed, region in planned:
            if not ctx.client.quota.can_spend("search.list"):
                log.warning("search budget exhausted (refresh reserve protected); stopping search cycle")
                res.quota_stopped = True
                break
            params = {"part": "snippet", "type": "video", "q": seed.query, "regionCode": region,
                      "maxResults": ing.search_max_results, "order": "viewCount",
                      "publishedAfter": _rfc3339(ctx.now - timedelta(hours=ing.search_published_within_hours))}
            try:
                resp = ctx.client.request("search.list", params, {"region_code": region,
                                                                   "discovery_method": "search"})
            except NonRetryableError as exc:
                res.errors.append(f"search {seed.id}/{region}: {exc}")
                done += 1
                continue
            res.calls += 1
            done += 1
            res.records.append(_record(ctx, resp, "search", seed.id, region, None, 0, {"tp_seed": seed.id}))
            ids = []
            for it in resp.payload.get("items", []):
                vid = (it.get("id") or {}).get("videoId")
                if vid and vid not in ids:
                    ids.append(vid)
            if not ids:
                continue
            contexts = {v: [[region, seed.id]] for v in ids}
            got = _fetch_videos(ctx, ids, "search_videos", seed.id, region, contexts, res)
            fetched_ids.update(got)
            until = ctx.now + timedelta(hours=ing.track_search_videos_hours)
            tracked_rows += [{"video_id": v, "region_code": region, "discovery_query": seed.id,
                              "first_discovered_at": ctx.now, "last_discovered_at": ctx.now,
                              "track_until": until} for v in got]
    except (QuotaBudgetExceeded, QuotaExhaustedError) as exc:
        log.warning("quota stop during search: %s", exc)
        res.quota_stopped = True
    except YouTubeApiError as exc:
        res.errors.append(f"search: {exc}")
        log.error("search discovery failed: %s", exc)
    finally:
        with ctx.wh.begin() as conn:
            if tracked_rows:
                ctx.wh.upsert(conn, s.tracked_videos, tracked_rows,
                              ["video_id", "region_code", "discovery_query"],
                              ["last_discovered_at", "track_until"])
            if done:
                ctx.wh.set_state(STATE_SEARCH_CURSOR, str((cursor + done) % len(pairs)), conn)
                ctx.wh.set_state(STATE_LAST_SEARCH, ctx.now.isoformat(), conn)
        res.search_ran = done > 0
    return res, fetched_ids


# ---------------------------------------------------------------- C. metric refresh
def refresh_tracked_metrics(ctx: DiscoveryContext, skip_ids: set[str]) -> DiscoveryResult:
    res = DiscoveryResult()
    t = s.tracked_videos
    with ctx.wh.connect() as conn:
        rows = conn.execute(
            select(t.c.video_id, t.c.region_code, t.c.discovery_query, t.c.last_discovered_at)
            .where(t.c.track_until > ctx.now).order_by(t.c.last_discovered_at.desc())
        ).all()
    contexts: dict[str, list[list[str]]] = {}
    for vid, region, query, _ in rows:
        if vid in skip_ids:
            continue
        contexts.setdefault(vid, []).append([region, query])
    ids = list(contexts)[: ctx.settings.pipeline.ingestion.max_tracked_videos]
    if not ids:
        return res
    try:
        _fetch_videos(ctx, ids, "search_refresh", "tracked", None, contexts, res)
    except (QuotaBudgetExceeded, QuotaExhaustedError) as exc:
        log.warning("quota stop during refresh: %s", exc)
        res.quota_stopped = True
    except YouTubeApiError as exc:
        res.errors.append(f"refresh: {exc}")
        log.error("metric refresh failed: %s", exc)
    return res


def contexts_for_record(rec: BronzeRecord) -> dict[str, list[list[str]]]:
    raw = rec.request_params.get("tp_contexts") or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw
