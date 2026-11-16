"""Offline YouTube API simulator — SAMPLE DATA ONLY.

Used by the test-suite and by ``trendpulse run --sample`` to exercise the
whole platform without an API key. It speaks the same HTTP contract the
real client uses (``session.get(url, params) -> response``), so the exact
production code path (quota, retries, Bronze, Silver, Gold) runs.

Everything it returns is synthetic and deliberately fictional (invented
products, games and channels prefixed "Sample ·"). Runs that use it are
recorded with ``data_source = 'sample_fixture'`` and the dashboard shows a
SAMPLE DATA banner. The pipeline refuses to write sample data into a
PostgreSQL warehouse unless explicitly forced.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _stable_id(seed: str, length: int) -> str:
    h = hashlib.sha256(seed.encode()).digest()
    return "".join(_ALPHABET[b % 64] for b in h[:length])


@dataclass
class Storyline:
    key: str
    category_id: int
    titles: list[str]
    tags: list[str]
    start_h: float        # hours relative to the reference time the story begins (negative = before)
    peak_h: float         # hours relative to the reference time it peaks
    decay_h: float        # half-life style decay after the peak
    regions: dict[str, float]
    n_videos: int = 10


STORYLINES: list[Storyline] = [
    Storyline("nova_x5", 28, ["Nova X5 smartphone review", "Nova X5 unboxing and camera test",
                              "Nova X5 vs Pixel 10 smartphone comparison", "Nova X5 launch event highlights"],
              ["nova x5", "smartphone", "phone review", "unboxing"], -30, 6, 30,
              {"IN": 1.0, "US": 0.8, "GB": 0.6, "DE": 0.5, "BR": 0.4, "JP": 0.2}, 14),
    Storyline("skyforge", 20, ["Skyforge Legends official gameplay trailer", "Skyforge Legends release date reveal",
                               "Skyforge Legends gameplay walkthrough part 1", "Skyforge Legends boss fight"],
              ["skyforge legends", "gameplay", "game trailer", "gaming"], -60, -20, 30,
              {"US": 1.0, "GB": 0.9, "CA": 0.9, "AU": 0.8, "DE": 0.7, "FR": 0.7, "JP": 0.6, "KR": 0.6,
               "BR": 0.7, "IN": 0.5}, 14),
    Storyline("aurora_cup", 17, ["Aurora Cup final match highlights", "Aurora Cup final goals and highlights",
                                 "Aurora Cup final post match reaction"],
              ["aurora cup", "football", "highlights", "goals"], -80, -50, 6,
              {"BR": 1.0, "FR": 0.8, "DE": 0.8, "GB": 0.9, "US": 0.3}, 10),
    Storyline("lumen_agent", 28, ["Lumen AI agent builds an app by itself", "Lumen AI agent tutorial for beginners",
                                  "Lumen AI agent vs ChatGPT", "Lumen AI agent explained"],
              ["lumen ai", "ai agent", "artificial intelligence", "chatgpt"], -14, 20, 40,
              {"US": 1.0, "IN": 0.9, "GB": 0.6, "KR": 0.4, "JP": 0.3, "DE": 0.4}, 12),
    Storyline("quantum_cats", 1, ["Quantum Cats season 2 official trailer", "Quantum Cats season 2 episode 1 recap",
                                  "Quantum Cats anime opening"],
              ["quantum cats", "anime", "season 2", "official trailer"], -140, -110, 400,
              {"JP": 1.0, "KR": 0.6, "US": 0.5, "FR": 0.5, "BR": 0.4}, 10),
    Storyline("starline_ipl", 17, ["Starline T20 cricket league match highlights", "Starline T20 final over cricket",
                                   "Starline T20 cricket best catches"],
              ["starline t20", "cricket", "t20", "highlights"], -40, -6, 60,
              {"IN": 1.0, "AU": 0.7, "GB": 0.5}, 12),
    Storyline("echo_wave", 10, ["Echo Wave - Midnight Lights (Official Music Video)",
                                "Echo Wave - Paper Hearts (Official Audio)", "Echo Wave live performance concert"],
              ["echo wave", "official music video", "new song", "kpop"], -8, 2, 30,
              {"KR": 1.0, "JP": 0.7, "US": 0.6, "BR": 0.5, "IN": 0.3, "GB": 0.4}, 12),
    Storyline("market_rally", 25, ["Stock market rally explained: nifty and nasdaq", "Why the stock market moved today",
                                   "Stock market crash or rally? Investing tips"],
              ["stock market", "investing", "nifty", "nasdaq"], -36, -18, 10,
              {"IN": 1.0, "US": 0.9, "GB": 0.4}, 10),
]

EVERGREEN: list[tuple[int, str, list[str]]] = [
    (10, "Official music video - new single", ["official music video", "new song"]),
    (20, "Minecraft survival gameplay", ["minecraft", "gameplay"]),
    (20, "Fortnite new season gameplay", ["fortnite", "gaming"]),
    (24, "Funny comedy sketch compilation", ["comedy", "funny"]),
    (26, "Easy street food recipe at home", ["recipe", "cooking", "street food"]),
    (22, "Day in my life travel vlog", ["vlog", "travel vlog"]),
    (27, "Physics explained in 10 minutes", ["physics", "explained"]),
    (28, "Space mission rocket launch live", ["space", "rocket launch", "nasa"]),
    (25, "Breaking news headlines today", ["breaking news", "news update"]),
    (17, "Basketball top plays of the week", ["basketball", "nba"]),
    (2, "Supercar test drive review", ["car review", "supercar"]),
    (15, "Funny cats and dogs compilation", ["cats", "dogs", "pets"]),
]

REGIONS = ["IN", "US", "GB", "CA", "AU", "DE", "FR", "JP", "KR", "BR"]


@dataclass
class _Video:
    video_id: str
    title: str
    channel_id: str
    channel_title: str
    category_id: int
    tags: list[str]
    published_h: float
    base_views: float
    rate: float
    like_ratio: float
    comment_ratio: float
    story: Storyline | None
    affinity: dict[str, float] = field(default_factory=dict)


class _Resp:
    def __init__(self, status: int, payload: dict[str, Any]):
        self.status_code = status
        self._payload = payload
        self.headers: dict[str, str] = {}
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


class SampleYouTubeSession:
    """Drop-in for ``requests.Session`` returning deterministic synthetic responses."""

    def __init__(self, clock: Callable[[], datetime], epoch: datetime | None = None, seed: int = 7,
                 fault_rate: float = 0.0):
        self.clock = clock
        # ``epoch`` is the scenario's reference time ("now" at the end of a simulation): storylines are
        # positioned relative to it, so any simulation length ends in the same, varied state.
        self.epoch = (epoch or datetime(2026, 1, 5, 0, 0, tzinfo=UTC)).astimezone(UTC)
        self.fault_rate = fault_rate
        self.calls: list[tuple[str, dict[str, Any]]] = []
        rng = random.Random(seed)
        channels = [(f"UC{_stable_id(f'ch{i}', 22)}", f"Sample · Channel {i:02d}") for i in range(60)]
        self.videos: dict[str, _Video] = {}
        for st in STORYLINES:
            for i in range(st.n_videos):
                ch = channels[rng.randrange(len(channels))]
                title = st.titles[i % len(st.titles)] + ("" if i < len(st.titles) else f" #{i}")
                vid = _stable_id(f"{st.key}-{i}", 11)
                self.videos[vid] = _Video(
                    vid, title, ch[0], ch[1], st.category_id, list(st.tags),
                    published_h=st.start_h + rng.uniform(0, 6) + i * 0.5,
                    base_views=rng.uniform(2e4, 4e5), rate=rng.uniform(0.02, 0.15),
                    like_ratio=rng.uniform(0.01, 0.06), comment_ratio=rng.uniform(0.0005, 0.004),
                    story=st, affinity=st.regions)
        for j in range(160):
            cat, title, tags = EVERGREEN[j % len(EVERGREEN)]
            ch = channels[rng.randrange(len(channels))]
            vid = _stable_id(f"ever-{j}", 11)
            aff = {r: rng.uniform(0.1, 1.0) for r in rng.sample(REGIONS, rng.randint(2, 6))}
            self.videos[vid] = _Video(vid, f"{title} {j}", ch[0], ch[1], cat, list(tags),
                                      published_h=-rng.uniform(10, 200),
                                      base_views=rng.uniform(1e4, 2e6), rate=rng.uniform(0.005, 0.05),
                                      like_ratio=rng.uniform(0.01, 0.05), comment_ratio=rng.uniform(0.0005, 0.003),
                                      story=None, affinity=aff)

    # ----------------------------------------------------------- model
    def _hours(self) -> float:
        return (self.clock() - self.epoch).total_seconds() / 3600

    @staticmethod
    def _curve(st: Storyline | None, h: float) -> float:
        if st is None:
            return 0.6
        if h < st.start_h:
            return 0.0
        if h <= st.peak_h:
            span = max(st.peak_h - st.start_h, 1e-6)
            return 0.15 + 0.85 * ((h - st.start_h) / span)
        return math.exp(-(h - st.peak_h) / st.decay_h)

    def _stats(self, v: _Video, h: float) -> dict[str, str]:
        age = max(0.0, h - v.published_h)
        boost = 1 + 3 * self._curve(v.story, h)
        views = int(v.base_views * (1 + v.rate * age * boost))
        return {"viewCount": str(views), "likeCount": str(int(views * v.like_ratio)),
                "commentCount": str(int(views * v.comment_ratio))}

    def _item(self, v: _Video, h: float) -> dict[str, Any]:
        pub = self.epoch + timedelta(hours=v.published_h)
        item = {
            "kind": "youtube#video", "etag": _stable_id(v.video_id + str(int(h * 2)), 12), "id": v.video_id,
            "snippet": {
                "publishedAt": pub.strftime("%Y-%m-%dT%H:%M:%SZ"), "channelId": v.channel_id,
                "title": v.title, "description": f"SAMPLE DATA (synthetic). {v.title}. " + " ".join(v.tags),
                "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{v.video_id}/hqdefault.jpg"}},
                "channelTitle": v.channel_title, "tags": v.tags, "categoryId": str(v.category_id),
                "defaultAudioLanguage": "en",
            },
            "contentDetails": {"duration": f"PT{3 + int(v.base_views) % 20}M{int(v.base_views) % 60}S"},
            "statistics": self._stats(v, h),
        }
        if self.fault_rate and random.Random(f"{v.video_id}{int(h * 2)}").random() < self.fault_rate:
            item["snippet"].pop("channelId")  # exercise quarantine
        return item

    def _visible(self, v: _Video, h: float) -> bool:
        return v.published_h <= h

    def _chart(self, region: str, category: str | None, h: float) -> list[_Video]:
        scored = []
        for v in self.videos.values():
            if not self._visible(v, h) or region not in v.affinity:
                continue
            if category and str(v.category_id) != category:
                continue
            rng = random.Random(f"{v.video_id}{region}{int(h * 2)}")
            curve = self._curve(v.story, h)
            # chart membership is probabilistic: a story's videos chart more often as it heats up
            if rng.random() > min(1.0, curve * v.affinity[region] * 1.2):
                continue
            score = (0.2 + curve) * v.affinity[region] * rng.uniform(0.85, 1.15) * math.log10(v.base_views)
            scored.append((score, v))
        scored.sort(key=lambda x: -x[0])
        return [v for _, v in scored]

    # ----------------------------------------------------------- HTTP surface
    # Fault injection (``trendpulse simulate --chaos``): exercises the real failure paths.
    #   corrupt_metrics — 40 % of items get viewCount "-1"      → negative_view_count FAIL (gate closes)
    #   schema_break    — 30 % of items lose statistics.viewCount → schema_drift FAIL
    #   outage          — every request for region JP returns 503 → retries exhausted, run partial
    #   flaky           — every 4th request fails once with 503   → client retries succeed
    FAULT_MODES = ("corrupt_metrics", "schema_break", "outage", "flaky")

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> _Resp:
        params = dict(params or {})
        params.pop("key", None)
        self.calls.append((url, params))
        mode = getattr(self, "mode", None)
        if mode == "outage" and params.get("regionCode") == "JP":
            return _Resp(503, {"error": {"code": 503, "errors": [{"reason": "backendError"}]}})
        if mode == "flaky" and len(self.calls) % 4 == 0 and not getattr(self, "_flaked", False):
            self._flaked = True
            return _Resp(503, {"error": {"code": 503, "errors": [{"reason": "backendError"}]}})
        self._flaked = False
        resp = self._get(url, params)
        if mode in {"corrupt_metrics", "schema_break"} and resp.status_code == 200:
            for i, it in enumerate(resp.json().get("items", [])):
                if "statistics" not in it:
                    continue
                if mode == "corrupt_metrics" and i % 5 < 2:
                    it["statistics"]["viewCount"] = "-1"
                if mode == "schema_break" and i % 10 < 3:
                    it["statistics"].pop("viewCount", None)
        return resp

    def _get(self, url: str, params: dict[str, Any]) -> _Resp:
        h = self._hours()
        resource = url.rstrip("/").rsplit("/", 1)[-1]
        if resource == "videos":
            if params.get("chart") == "mostPopular":
                region = params.get("regionCode", "US")
                cat = params.get("videoCategoryId")
                if cat and cat not in {"1", "2", "10", "15", "17", "20", "22", "23", "24", "25", "26", "27", "28"}:
                    return _Resp(404, {"error": {"code": 404, "errors": [{"reason": "videoChartNotFound"}]}})
                vids = self._chart(region, cat, h)
                size = int(params.get("maxResults", 5))
                start = int(params.get("pageToken") or 0)
                page = vids[start:start + size]
                payload = {"kind": "youtube#videoListResponse", "items": [self._item(v, h) for v in page],
                           "pageInfo": {"totalResults": len(vids), "resultsPerPage": size}}
                if start + size < min(len(vids), 200):
                    payload["nextPageToken"] = str(start + size)
                return _Resp(200, payload)
            ids = [i for i in str(params.get("id", "")).split(",") if i]
            items = [self._item(self.videos[i], h) for i in ids if i in self.videos and self._visible(self.videos[i], h)]
            return _Resp(200, {"kind": "youtube#videoListResponse", "items": items,
                               "pageInfo": {"totalResults": len(items), "resultsPerPage": 50}})
        if resource == "search":
            q = str(params.get("q", "")).lower().split()
            region = params.get("regionCode", "US")
            after = params.get("publishedAfter")
            after_h = -1e9
            if after:
                after_h = (datetime.fromisoformat(after.replace("Z", "+00:00")) - self.epoch).total_seconds() / 3600
            hits = []
            for v in self.videos.values():
                if not self._visible(v, h) or v.published_h < after_h:
                    continue
                text = (v.title + " " + " ".join(v.tags)).lower()
                if any(w in text for w in q) and (region in v.affinity):
                    hits.append(v)
            hits.sort(key=lambda v: -int(self._stats(v, h)["viewCount"]))
            hits = hits[: int(params.get("maxResults", 5))]
            items = [{"kind": "youtube#searchResult", "id": {"kind": "youtube#video", "videoId": v.video_id},
                      "snippet": {"title": v.title, "channelId": v.channel_id, "channelTitle": v.channel_title,
                                  "publishedAt": (self.epoch + timedelta(hours=v.published_h)).strftime(
                                      "%Y-%m-%dT%H:%M:%SZ")}} for v in hits]
            return _Resp(200, {"kind": "youtube#searchListResponse", "items": items,
                               "pageInfo": {"totalResults": len(items), "resultsPerPage": len(items)}})
        return _Resp(404, {"error": {"code": 404, "errors": [{"reason": "notFound"}]}})
