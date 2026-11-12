# Topic intelligence

YouTube does not publish "topics". Everything below is **inferred by TrendPulse** from video
metadata, and the dashboard labels it that way.

## 1. Inputs

Title, tags, description (first 500 chars), channel title and YouTube `categoryId` of every
valid Silver observation in the run.

## 2. Normalisation (`topics/normalize.py`)

NFKC unicode normalisation → lower-case → strip URLs, @mentions, `#` → punctuation to spaces.
Stop words = scikit-learn English list + YouTube boilerplate (`official`, `video`, `4k`, `shorts`,
`trailer`, `lyrics` …) + small German/French/Portuguese/Spanish/romanised-Hindi lists.

Canonical key of a phrase = sorted set of singularised, non-stop-word tokens:
`"GTA 6 Trailers"` and `"trailer gta 6"` both → `"6 gta"`.

## 3. Taxonomy classification (`topics/classifier.py`)

`config/taxonomy.yaml` defines 11 categories and 56 topics with keyword phrases. For each video:

```
score(topic)  = Σ best field weight of each matched keyword      (title 3, tags 2, description 1, channel 1)
              + 1.0 if categoryId is listed for the topic AND at least one keyword matched
confidence    = min(1, score / 4.0)
```

| Confidence | Result |
|---|---|
| ≥ 0.50 | assigned, `review_status = auto` (max 3 topics per video) |
| 0.25 – 0.50 | stored as `needs_review` — visible in the Control Center review queue, **excluded from metrics** |
| < 0.25 | no assignment |

A YouTube category alone never creates an assignment — that is how we avoid "classify every
music video as Music Videos" noise. Keywords are matched on word boundaries with one compiled
alternation per field (longest first), so a run of ~2,000 videos classifies in milliseconds.

## 4. Emerging-topic discovery (`topics/discovery.py`)

Taxonomy topics are broad by design. Real trends are entities: a phone launch, a game, a match.

1. **Documents**: title and each tag are separate *segments*; n-grams never cross a segment.
2. **Candidates**: binary `CountVectorizer`, 1–3-grams, `min_df = 3 videos`, `max_df = 25 %` of the
   run's videos (drops boilerplate).
3. **Breadth**: ≥ 2 distinct channels (≥ 3 for single words) — one channel's series is not a trend.
4. **Entity filter**: at least one *distinctive* token — not a digit, not a generic word
   (`final`, `season`, `easy`…), not already a taxonomy keyword. `"street food recipe"` and
   `"final match"` are rejected; `"nova x5"` and `"skyforge legends"` pass.
5. **Salience**: summed sub-linear TF-IDF × ln(1 + channels).
6. **Similar-topic grouping**: a candidate joins an accepted topic if the phrases nest and their
   video sets overlap ≥ 80 % (overlap coefficient), or they share a distinctive token and ≥ 80 % of
   the candidate's videos already belong to the topic. Broadest phrase wins; ties go to the phrase
   with more distinctive tokens, then the shorter one. Variants are kept as aliases.
7. At most 40 *new* topics per run.

## 5. Stable identities (`topics/registry.py`)

* Taxonomy: `tx.<category>.<topic>` from config (renaming a topic keeps its ID).
* Discovered: `dx_` + first 12 hex chars of `sha1(canonical_key)` — deterministic, so a rebuild
  from scratch produces the same IDs.
* Before minting a new ID a candidate is resolved against existing topics by (1) alias key,
  (2) exact key, (3) token-set containment with ≥ 2 shared tokens, (4) character 3–4-gram TF-IDF
  cosine ≥ 0.88. Every non-exact match is stored in `topic_aliases`, so it stays stable.
* Discovered topics observed in the last 14 days are re-matched **every run** (all key tokens
  present in title/tags), even when they fall below the discovery threshold. This is what makes
  *declining* and *dormant* observable instead of topics silently vanishing.

## 6. Unclassified & review

Videos with no `auto` assignment get `tx.unclassified` (counted in coverage metrics, never
ranked). Low-confidence matches stay in the review queue. The `missing_topic_ids` quality check
guarantees every valid video has a countable topic.

## 7. Topic metrics

Per topic and region (plus `GLOBAL`, which counts a video once even if it charts in 10 countries):

| Metric | Definition |
|---|---|
| `video_count`, `channel_count` | distinct observed videos / channels in the snapshot |
| `total_views` | Σ **lifetime** view counts of those videos — a size measure, *not* growth |
| `views_gained`, `views_per_hour` | Σ views gained since each video's previous observation (÷ hours) — the topic-level view-growth signal; `NULL` on a video's first observation, never invented |
| `avg_engagement_rate` | mean of (likes + comments) / views; hidden counts treated as 0 |
| `popularity_score` | `20·ln(1+videos) + 10·ln(1+channels) + 8·log10(1+views/hour) + 1·engagement%` (weights in `pipeline.yaml → scoring`) |
| `popularity_rank` | rank by score within region; `Unclassified` has rank 0 (not ranked) |

The score is **absolute**, not normalised per run, so it is comparable across time.

## 8. Growth, velocity, acceleration

For each window `w ∈ {30m, 1h, 6h, 24h, 7d, 30d}` the builder finds the published snapshot closest
to `now − w` within ±50 % of `w` (if none exists: `growth_status = no_history`, values `NULL`).

```
growth_pct    = (current − previous) / previous × 100
                previous missing → NULL ("new");  previous = 0 & current > 0 → NULL (undefined)
                both 0 → 0;  topic gone now → current treated as 0 (−100 %, "vanished")
velocity      = (score_now − score_then) / hours_between            [points per hour]
acceleration  = (velocity(now, now−w) − velocity(now−w, now−2w)) / hours   [points per hour²]
prior_growth  = growth of the preceding window (now−2w → now−w)
```

Growth is computed for popularity score, video count and views/hour.
`growth_status ∈ {no_history, new, rising, stable, falling, vanished}` (threshold ±15 %).

## 9. Lifecycle

Ordered rules on the 24 h lookback (falling back to 6 h, 1 h, 30 m while history is short).
Every threshold lives in `pipeline.yaml → lifecycle`.

| Stage | Rule |
|---|---|
| **baseline** | platform has < 6 h of history — nothing can honestly be called "new" yet |
| **dormant** | not observed for ≥ 24 h |
| **declining** | not observed for < 24 h; or growth ≤ −15 %; or score < 0.6 × its 7-day max |
| **emerging** | first observed < 24 h ago |
| **growing** | growth ≥ +15 % |
| **peaking** | |growth| < 15 %, **preceding window grew ≥ 15 %**, and score ≥ 0.9 × 7-day max |
| **stable** | everything else (flat with no preceding rise) |

`stable` and `baseline` are additions to the five requested stages: without them, flat topics
and day-one data would be mislabelled as "peaking" or "emerging".

## Limitations

* CJK and other unsegmented scripts: titles become long tokens, so discovery rarely finds
  phrases there (taxonomy matching on tags still works). A segmenter (e.g. `fugashi`, `kiwipiepy`)
  could be added per region.
* Keyword taxonomies drift; the review queue and unclassified share are the signals to tune it.
* Topic popularity reflects *what the API exposes* (charts + sampled search results), not all of
  YouTube.
