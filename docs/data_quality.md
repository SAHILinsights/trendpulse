# Data quality framework

Implementation: `src/trendpulse/quality/framework.py`. Audit table: `dq_results`.
Rejected rows: `quarantine_records`. Configuration: `config/pipeline.yaml → quality`.

## Principles

1. **Parse tolerantly, judge explicitly.** The Silver parser never drops a row; it records
   problems in `_issues`. The DQ framework decides what is accepted.
2. **Never silently discard.** Every rejected row is written to `quarantine_records` with its
   check name(s), record key and raw payload.
3. **Gate before publication.** Any `FAIL` stops the run before Gold is touched; the previous
   snapshot keeps serving.
4. **Audit everything.** One `dq_results` row per check per run, including passing checks.

## Status semantics

| Status | Record-level check | Dataset-level check |
|---|---|---|
| PASS | no failing rows | condition holds |
| WARNING | failing rows ≤ `critical_failure_ratio` (20 %) — they are quarantined, the run continues; or check severity is `warning` | soft anomaly (e.g. new API field, ±50 % volume) |
| FAIL | `critical` check with failing rows > 20 % of the batch | hard violation — **gate closes** |

## The 14 checks

| # | Check | Level | Severity | Action | Rule |
|---|---|---|---|---|---|
| 1 | `null_video_id` | record | critical | quarantine | `id` missing/blank |
| 2 | `duplicate_observations` | record | warning | quarantine extra copies | same (run, video, region, method, query); best chart rank kept |
| 3 | `invalid_region_code` | record | critical | quarantine | region not in `countries.yaml` |
| 4 | `negative_view_count` | record | critical | quarantine | viewCount < 0 |
| 5 | `negative_like_count` | record | critical | quarantine | likeCount < 0 |
| 6 | `negative_comment_count` | record | critical | quarantine | commentCount < 0 |
| 7 | `invalid_timestamps` | record | critical | quarantine | publishedAt/observedAt missing, unparseable, or > 60 min in the future |
| 8 | `missing_topic_ids` | dataset | critical | gate | a valid video without a countable topic assignment |
| 9 | `invalid_category` | record | warning | flag (kept) | categoryId missing or not a known YouTube category |
| 10 | `schema_drift` | dataset | critical | gate | required paths (`id`, `snippet.title`, `snippet.channelId`, `snippet.publishedAt`, `statistics.viewCount`) missing in > 20 % → FAIL; any missing or unexpected top-level keys → WARNING |
| 11 | `stale_data` | dataset | critical | gate | newest observation older than `stale_after_minutes` (120) → FAIL; gap since previous success > 120 min → WARNING; empty batch → FAIL |
| 12 | `volume_anomaly` | dataset | critical | gate | vs median of last 12 successful runs: drop > 90 % → FAIL, change ≥ ±50 % → WARNING; < 3 runs history → PASS ("insufficient history") |
| 13 | `invalid_metric_types` | record | critical | quarantine | statistics value not an integer string, or viewCount missing |
| 14 | `missing_channel_id` | record | critical | quarantine | `snippet.channelId` missing/blank |

Missing `likeCount`/`commentCount` are **valid** (creators can hide them) and stored as NULL.

## Configuration

```yaml
quality:
  critical_failure_ratio: 0.2
  volume_lookback_runs: 12
  volume_warning_change: 0.5
  volume_failure_drop: 0.9
  stale_after_minutes: 120
  max_future_skew_minutes: 60
  checks:                       # per-check overrides
    invalid_category: {enabled: true, severity: warning}
    duplicate_observations: {enabled: true, severity: warning}
```

## Where checks run in the pipeline

```
validate_schema   → schema_drift (Bronze payload profile)
transform_silver  → record checks 1–7, 9, 13, 14 → valid set + quarantine list
extract_topics    → (on valid rows only)
data_quality      → stale_data, volume_anomaly, missing_topic_ids; write dq_results; GATE
quarantine_records→ persist quarantine — runs on PASS **and on FAIL** (since schema v2 / control-plane
                    upgrade), so the rows behind a closed gate are inspectable; Gold stays blocked
```

## Tested behaviour

`tests/unit/test_silver_and_quality.py` covers each check, the ratio-based FAIL, per-check
disabling, schema drift levels, staleness and volume rules. `tests/integration/test_pipeline.py`
proves that a failed gate leaves Gold and history untouched and that malformed records end up
in quarantine with their raw payload.

## Inspecting quality in the control plane

**Data Quality & Governance** reads `dq_results` and `quarantine_records` directly:

* *Overview* — gate decision for the latest audited run with the configured policy text, and the
  14 checks merged with their definitions (`quality.framework.check_catalog()`).
* *Validation history* — check × run heat-map (PASS/WARNING/FAIL), gate decisions over 7 days.
* *Failed checks* — every WARNING/FAIL with its JSON details.
* *Rejected records & quarantine* — paginated, filter by run and rule; selecting a record shows the
  violated rule(s) with severity and description, and the raw API item as received.
* *Schema validation*, *Data freshness*, *Audit history*, *Check catalog*.

No quality score is computed — the page shows only what the audit tables contain.
