"""Builders for realistic YouTube API payloads used across tests."""

from __future__ import annotations

from datetime import UTC, datetime

from trendpulse.ingestion.bronze import BronzeRecord

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


def item(vid="vid00000001", title="Nova X5 smartphone review", channel="UCchan1", channel_title="Chan 1",
         views="1000", likes="50", comments="5", category="28", tags=None, published="2026-03-01T10:00:00Z",
         duration="PT10M5S", **extra):
    it = {
        "kind": "youtube#video", "etag": "e", "id": vid,
        "snippet": {"publishedAt": published, "channelId": channel, "title": title, "description": "desc",
                    "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{vid}/hq.jpg"}},
                    "channelTitle": channel_title, "tags": tags if tags is not None else ["nova x5"],
                    "categoryId": category},
        "contentDetails": {"duration": duration},
        "statistics": {"viewCount": views, "likeCount": likes, "commentCount": comments},
    }
    for k, v in extra.items():
        it[k] = v
    return it


def chart_record(items, region="IN", category=None, page=0, run_id="run-1", fetched=NOW, offset=0):
    return BronzeRecord(run_id=run_id, endpoint="videos.list", discovery_method="regional_chart",
                        discovery_query=f"chart:{category or 'all'}", region_code=region, category_id=category,
                        page_index=page, request_params={"chart": "mostPopular", "tp_rank_offset": offset},
                        http_status=200, payload={"items": items}, fetched_at=fetched)


def search_videos_record(items, contexts, run_id="run-1", fetched=NOW, method="search_videos"):
    return BronzeRecord(run_id=run_id, endpoint="videos.list", discovery_method=method, discovery_query="ai",
                        region_code="US", category_id=None, page_index=0,
                        request_params={"id": ",".join(i["id"] for i in items), "tp_contexts": contexts},
                        http_status=200, payload={"items": items}, fetched_at=fetched)
