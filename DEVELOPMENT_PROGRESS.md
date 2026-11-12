# Development progress

_Last updated: 13 Nov 2026 (IST). Everything below was actually executed; nothing is projected
unless labelled "estimate"._

## Status summary

| Area | Status |
|---|---|
| Ingestion (charts, rotating search, metric refresh, quota, retries) | ✅ built, unit + integration tested (mocked HTTP + offline simulator) |
| Real YouTube API run | ⏳ **not yet executed — no API key available in the build environment** |
| Bronze / Silver / Gold + migrations | ✅ verified on SQLite and PostgreSQL 16 |
| Topic intelligence (taxonomy, discovery, stable IDs) | ✅ tested |
| Growth / velocity / acceleration / lifecycle | ✅ tested (incl. edge cases) |
| Data-quality framework (14 checks, quarantine, audit, gate) | ✅ tested |
| Orchestration (17 stages, lock, retries, replay, abandoned runs) | ✅ tested incl. crash + recovery |
| Monitoring & alerts | ✅ Control Center; webhook alert tested with mock |
| Dashboard (7 pages) | ✅ renders on PostgreSQL data, page smoke tests pass |
| CI/CD workflows | ✅ written, YAML validated; ⏳ not yet run on GitHub (no repository yet) |
| Docker | ✅ Dockerfile + compose written; ⏳ image not built (no Docker daemon in build env) |
| Public deployment | ⏳ blocked on account authorisation (see below) |
| Documentation | ✅ README, 8 docs, technical design, security review, data dictionary |

## Completed

- Config-driven platform: `countries.yaml` (10 regions), `search_seeds.yaml` (14 seeds),
  `taxonomy.yaml` (11 categories / 56 topics), `pipeline.yaml` (schedule, quota, scoring,
  lifecycle, windows, quality, retention, alerts) with pydantic validation + env overrides.
- YouTube client: quota pre-check, Pacific-day ledger, refresh reserve, full-jitter backoff,
  Retry-After, fail-fast 4xx, quota-exhausted handling, pagination, request logging w/o key.
- Hybrid discovery: regional charts (all + Music + Gaming), interleaved search rotation with
  persisted cursor, 72 h tracking + `videos.list` refresh.
- Bronze per-response persistence (gzip JSON, SHA-256, lineage) + local lake; replay.
- Silver tolerant parser, 10 record checks + dedupe, quarantine, slim Silver persistence,
  Parquet lake.
- Topic engine; deterministic IDs; alias table; continuity matching of active topics.
- Gold: 4 dims, 2 append-only facts, 5 serving tables, single-transaction publication.
- Metrics: popularity score, growth for 6 windows, velocity, acceleration, prior growth,
  growth status, 7-stage lifecycle, rank changes, consecutive observations.
- Retention: policy-driven metadata deletion (30 d), whole-run compaction tiers.
- Orchestrator + CLI (`run`, `simulate`, `replay`, `migrate`, `status`, `validate-config`,
  `quota-plan`, `retention`, `export-parquet`).
- Least-privilege Postgres roles (verified: reader cannot write or read Bronze; ingest role
  migrates without database-level CREATE).
- Streamlit dashboard (dark navy theme, validated colour palette, sample/live/historical banners,
  timezone selector, operator-only dispatch trigger).
- CI (ruff, config validation, secret scans, tests on SQLite + Postgres service for 3.11/3.12,
  DDL check, E2E smoke), scheduled ingestion workflow with concurrency group.

## Test results (latest run)

| Suite | Result |
|---|---|
| `ruff check src tests scripts streamlit_app.py` | All checks passed |
| `python scripts/check_secrets.py` | passed |
| `pytest` (SQLite warehouse) | **108 passed**, coverage **93 %** (3,091 statements) |
| `pytest` with `TRENDPULSE_TEST_DATABASE_URL` (PostgreSQL 16) | **101 passed, 7 skipped** (Streamlit page smoke tests are SQLite-only) |
| `migrations/001_initial_schema.sql` applied with `psql -v ON_ERROR_STOP=1` | OK |
| `migrations/002_roles.sql` + privilege checks | OK |
| Dashboard screenshot smoke (`scripts/screenshot_dashboard.py`, 7 pages) | no exceptions |

Environment: Python 3.13 locally (CI targets 3.11 and 3.12 — not yet executed on GitHub).

## End-to-end simulation (SAMPLE data)

`trendpulse simulate --cycles 120 --interval 30 --allow-sample-on-postgres` against local
PostgreSQL 16 (60 simulated hours): **120/120 runs success**, mean run 1.6 s (max 2.8 s),
~850 valid Silver rows/run, 15,907 quota units *as if* spent over 3 simulated days (the
simulator does not call Google).

### Measured storage

After the 60 h simulation (before the tighter retention defaults now in `pipeline.yaml`):
116 MB total — `silver_video_observations` 35 MB, `fact_video_observations` 34 MB (93k rows,
~365 B/row incl. indexes), `bronze_api_responses` 11 MB, `fact_topic_observations` 11 MB (27k rows).

**Estimate** for real API volume (~3,000 observation rows/run, ~3.5× the simulator) with the
current retention tiers: ~120 MB rolling video-level data + ~35 MB recent topic history,
growing ~0.25 MB/day for daily topic snapshots — roughly 200–350 MB steady state, inside the
500 MB free tier. Verify against Supabase's *Database → Usage* after the first week.

## Known issues / limitations

- No real-API execution yet; response parsing follows the documented v3 schema and is
  exercised with documented-format fixtures, but production payload edge cases are unverified.
- Discovered-topic naming sometimes prefers a shorter phrase (e.g. "starline" instead of
  "starline t20") when the extra word is a taxonomy keyword.
- Japanese/Korean titles produce few discovered topics (no word segmentation).
- `growth_24h`/`7d`/`30d` stay `no_history` until that much history exists (by design).
- Supabase pooler + custom roles: documented, not tested against a live Supabase project.
- GitHub cron is best-effort; scheduled workflows pause after 60 days of repo inactivity.

## Deployment status

| Target | Status |
|---|---|
| GitHub repository `trendpulse-youtube-trending-intelligence` | ⏳ not created — the build environment had no valid GitHub credentials |
| Supabase project | ⏳ not created — requires your account |
| GitHub Actions secrets | ⏳ pending |
| Streamlit Community Cloud app | ⏳ not deployed — **no public URL exists yet** |

## Required manual actions

1. Create a Google Cloud project, enable **YouTube Data API v3**, create a restricted API key
   (docs/deployment.md §1). Decide on the "Analytics & Reporting" policy amendment (retention).
2. Create a Supabase project; run `migrations/001_initial_schema.sql`, then
   `migrations/002_roles.sql` with your own passwords; copy the *Session pooler* URLs (§2).
3. Create the GitHub repository and push this folder; add secrets `YOUTUBE_API_KEY` and
   `DATABASE_URL` (§3); run **Scheduled ingestion** once with *force_search*.
4. Deploy `streamlit_app.py` on Streamlit Community Cloud with secret `DASHBOARD_DATABASE_URL` (§4).
5. Confirm CI is green, ≥ 3 scheduled runs succeeded, dashboard shows **Live API-derived**;
   replace `OWNER` in README badges and add the public URL; then publish the LinkedIn post
   (`docs/linkedin_post_draft.md`).

## Remaining tasks

- [ ] Execute first real API run and review Silver/DQ results on live payloads
- [ ] Tune taxonomy keywords from the review queue after a week of live data
- [ ] Verify storage growth on Supabase after 7 days
- [ ] Optional: Dependabot, branch protection, JP/KR tokenisers
