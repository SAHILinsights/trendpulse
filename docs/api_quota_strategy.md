# YouTube API quota strategy

The default YouTube Data API v3 allocation is **10,000 units per project per day**, reset at
midnight **Pacific Time**. Every request costs units even when it fails. Relevant costs:

| Endpoint | Units / call | Notes |
|---|---|---|
| `videos.list` | 1 | up to 50 IDs or 50 chart results per call |
| `search.list` | **100** | max 50 results per call |
| `videoCategories.list` | 1 | not needed — category IDs are a static global list |

## What TrendPulse can and cannot see

* **Regional popular charts** (`videos.list?chart=mostPopular&regionCode=…`). Since July 2025 YouTube
  retired the general "Trending" page and the chart reflects its category charts (Music, Movies,
  Gaming). We request the uncategorised chart plus categories 10 (Music) and 20 (Gaming) per region.
  These rows are labelled `regional_chart` everywhere.
* **Topic-oriented search** (`search.list` over configurable seeds, `order=viewCount`,
  published within 72 h). Labelled `search`. This is a *sample* of recent popular uploads for a
  query, not an exhaustive list.
* **Topics** are inferred by TrendPulse — never presented as a YouTube field.

The platform never claims to see "every trending video".

## Daily plan (default config)

`trendpulse quota-plan` computes this from `config/*.yaml`:

| Component | Calls | Units/day |
|---|---|---|
| Charts: 10 regions × 3 charts × 1 page × 48 runs | 1,440 | 1,440 |
| Search: 8 cycles/day × 6 searches × (100 + 1 enrichment) | 48 + 48 | 4,848 |
| Refresh: ≤ 1,500 tracked videos → ≤ 30 calls × 48 runs | ≤ 1,440 | ≤ 1,440 |
| **Planned total** | | **≤ 7,728** |
| Budget (`quota.daily_budget`) | | 9,000 |
| Hard limit | | 10,000 |

## Guard rails

1. **Pre-flight check per request** — `QuotaTracker.reserve()` refuses any call that would exceed
   the daily budget, *before* the HTTP request is sent.
2. **Shared daily ledger** — the tracker starts each run with the units already spent in the
   current Pacific quota day (`SUM(quota_cost)` over `api_request_log`), so 48 runs share one budget.
3. **Refresh reserve** — `search.list` may not consume the last 600 budget units, so cheap
   metric refreshes (the thing that keeps history continuous) can never be starved.
4. **Rotation instead of fan-out** — 133 (seed, region) pairs are walked round-robin with a
   persisted cursor, 6 per cycle, interleaved so each cycle covers six different seeds.
5. **Refresh instead of re-search** — search-discovered videos are tracked for 72 h and re-measured
   with `videos.list` (50 IDs per unit) instead of repeating 100-unit searches.
6. **Billing-aware accounting** — answered error responses are billed in the ledger; transport
   failures (no response) are not.
7. **Clean stop** — when the budget is reached the run finishes as `partial` with what it has;
   a Google `quotaExceeded` 403 is never retried.

## Tuning

| Goal | Change |
|---|---|
| More countries | add to `countries.yaml`; each costs 3 units/run (144/day) |
| More search coverage | raise `searches_per_cycle` or lower `search_interval_minutes` (each search ≈ 101 units) |
| Lower usage | `TRENDPULSE__SCHEDULE__INTERVAL_MINUTES=60` and the cron, or drop chart categories |
| Higher quota | request an increase in Google Cloud Console (requires compliance audit) |

Run `trendpulse validate-config` after changes — it warns if the plan exceeds the budget.

## Compliance

Read the current [YouTube API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies-guide)
and the [Additional Policies for Derived Metrics and Data Storage](https://developers.google.com/youtube/terms/derived-metrics-policy)
before going live. As summarised from those pages (checked November 2026 — they change):

* Non-statistical API data (titles, descriptions, channel names) is subject to a **30-day
  refresh-or-delete** rule. TrendPulse refreshes metadata every time a video is re-observed and
  `retention.api_metadata_max_days: 30` deletes `dim_videos`, `dim_channels` and topic
  assignments that have not been refreshed for 30 days. Bronze (raw payloads) lives 2 days;
  quarantined payloads 30 days.
* Storing **statistics** (views, likes, comments) and **derived metrics** (topic tags, scores,
  leaderboards, historical comparisons) for up to 36 months requires accepting the
  "Analytics & Reporting" policy amendment for your Google Cloud project. Defaults:
  video-level statistics history 30 days (`video_obs_max_days`), TrendPulse topic metrics
  400 days (`topic_obs_max_days`). **If you have not accepted the amendment, set
  `topic_obs_max_days: 30`.**
* Derived metrics must be distinguishable from API data: the dashboard labels popularity score,
  topics, lifecycle and growth as TrendPulse calculations, keeps YouTube's own category separate
  from TrendPulse categories (tags are additive, never a replacement), and links every video
  back to YouTube.
* Only public metadata and statistics are stored; no audiovisual content is downloaded; no
  personal data of viewers is collected.

This is an engineering summary, not legal advice.
