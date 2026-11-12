"""Bronze -> Silver: parse, conform and standardise raw API payloads.

The parser is deliberately *tolerant*: it never drops a record. Anything
odd (unparseable timestamp, non-numeric metric, missing field) is recorded
in the row's ``_issues`` list and left for the data-quality framework to
judge and quarantine. That separation keeps "how do we read the API" apart
from "what do we accept into the warehouse".
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from trendpulse.ingestion.bronze import BronzeRecord
from trendpulse.ingestion.discovery import contexts_for_record

VIDEO_LIST_METHODS = {"regional_chart", "search_videos", "search_refresh"}

# Contract for videos.list items (fields we rely on).
REQUIRED_PATHS = ["id", "snippet.title", "snippet.channelId", "snippet.publishedAt", "statistics.viewCount"]
KNOWN_TOP_LEVEL = {"kind", "etag", "id", "snippet", "statistics", "contentDetails", "status", "player",
                   "topicDetails", "localizations", "recordingDetails", "liveStreamingDetails"}

SILVER_PERSISTED_COLUMNS = [
    "run_id", "video_id", "region_code", "discovery_method", "discovery_query", "chart_category_id",
    "chart_rank", "observed_at", "channel_id", "category_id", "published_at", "duration_seconds",
    "view_count", "like_count", "comment_count", "metadata_hash",
]

SILVER_COLUMNS = [
    "run_id", "video_id", "region_code", "discovery_method", "discovery_query", "chart_category_id",
    "chart_rank", "observed_at", "title", "channel_id", "channel_title", "category_id", "published_at",
    "duration_seconds", "view_count", "like_count", "comment_count", "tags", "description_snippet",
    "thumbnail_url", "default_language",
]

_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")
DESCRIPTION_SNIPPET_CHARS = 500


def parse_duration(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    m = _DURATION_RE.match(value)
    if not m:
        return None
    d, h, mi, se = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def parse_count(value: Any) -> tuple[int | None, bool]:
    """Return (value, type_ok). Missing values are allowed (hidden likes/comments)."""
    if value is None:
        return None, True
    if isinstance(value, bool):
        return None, False
    if isinstance(value, int):
        return value, True
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip()), True
    return None, False


def _get(d: dict, path: str) -> Any:
    cur: Any = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


@dataclass
class SchemaProfile:
    items: int = 0
    missing_required: dict[str, int] = field(default_factory=dict)
    unexpected_keys: dict[str, int] = field(default_factory=dict)


@dataclass
class SilverBatch:
    df: pd.DataFrame
    schema: SchemaProfile


def _thumb(snippet: dict) -> str | None:
    th = snippet.get("thumbnails") or {}
    for k in ("high", "medium", "default", "standard", "maxres"):
        if isinstance(th.get(k), dict) and th[k].get("url"):
            return th[k]["url"]
    return None


def _parse_item(item: dict, rec: BronzeRecord, schema: SchemaProfile) -> dict[str, Any]:
    issues: list[str] = []
    schema.items += 1
    for p in REQUIRED_PATHS:
        if _get(item, p) is None:
            schema.missing_required[p] = schema.missing_required.get(p, 0) + 1
    for k in item:
        if k not in KNOWN_TOP_LEVEL:
            schema.unexpected_keys[k] = schema.unexpected_keys.get(k, 0) + 1

    sn = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
    st = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
    cd = item.get("contentDetails") if isinstance(item.get("contentDetails"), dict) else {}

    views, ok_v = parse_count(st.get("viewCount"))
    likes, ok_l = parse_count(st.get("likeCount"))
    comments, ok_c = parse_count(st.get("commentCount"))
    for name, ok in (("view_count", ok_v), ("like_count", ok_l), ("comment_count", ok_c)):
        if not ok:
            issues.append(f"invalid_type:{name}")

    published_raw = sn.get("publishedAt")
    published = parse_ts(published_raw)
    if published_raw is not None and published is None:
        issues.append("unparseable:published_at")

    cat_raw = sn.get("categoryId")
    try:
        category_id = int(cat_raw) if cat_raw not in (None, "") else None
    except (TypeError, ValueError):
        category_id = None
        issues.append("invalid_type:category_id")

    tags = sn.get("tags") if isinstance(sn.get("tags"), list) else []
    vid = item.get("id")
    return {
        "video_id": vid.strip() if isinstance(vid, str) and vid.strip() else None,
        "title": (sn.get("title") or "").strip(),
        "channel_id": (sn.get("channelId") or None),
        "channel_title": sn.get("channelTitle"),
        "category_id": category_id,
        "published_at": published,
        "duration_seconds": parse_duration(cd.get("duration")),
        "view_count": views,
        "like_count": likes,
        "comment_count": comments,
        "tags": json.dumps([str(t) for t in tags][:50], ensure_ascii=False),
        "description_snippet": (sn.get("description") or "")[:DESCRIPTION_SNIPPET_CHARS],
        "thumbnail_url": _thumb(sn),
        "default_language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
        "_issues": issues,
        "_raw": item,
    }


def bronze_to_silver(records: list[BronzeRecord]) -> SilverBatch:
    rows: list[dict[str, Any]] = []
    schema = SchemaProfile()
    for rec in records:
        if rec.endpoint != "videos.list" or rec.discovery_method not in VIDEO_LIST_METHODS:
            continue
        items = rec.payload.get("items") or []
        if rec.discovery_method == "regional_chart":
            offset = int(rec.request_params.get("tp_rank_offset", 0))
            for idx, item in enumerate(items):
                base = _parse_item(item, rec, schema)
                rows.append({**base, "run_id": rec.run_id, "region_code": rec.region_code,
                             "discovery_method": "regional_chart", "discovery_query": rec.discovery_query,
                             "chart_category_id": rec.category_id, "chart_rank": offset + idx + 1,
                             "observed_at": rec.fetched_at})
        else:
            contexts = contexts_for_record(rec)
            for item in items:
                base = _parse_item(item, rec, schema)
                ctxs = contexts.get(item.get("id") or "", []) or [[rec.region_code, rec.discovery_query]]
                for region, query in ctxs:
                    rows.append({**base, "run_id": rec.run_id, "region_code": region,
                                 "discovery_method": "search", "discovery_query": query,
                                 "chart_category_id": None, "chart_rank": None, "observed_at": rec.fetched_at})
    df = pd.DataFrame(rows, columns=SILVER_COLUMNS + ["_issues", "_raw"])
    df["metadata_hash"] = [
        hashlib.sha1(f"{t}|{c}|{tg}|{d}".encode()).hexdigest()[:16]  # noqa: S324 - change detection only
        for t, c, tg, d in zip(df["title"], df["channel_id"], df["tags"], df["description_snippet"], strict=True)]
    if not df.empty:
        for c in ("view_count", "like_count", "comment_count", "category_id", "duration_seconds",
                  "chart_category_id", "chart_rank"):
            df[c] = pd.array(df[c].tolist(), dtype="Int64")
    return SilverBatch(df=df, schema=schema)
