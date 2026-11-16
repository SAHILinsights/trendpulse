# Development progress

_Authoritative project status. Last updated: 13 Nov 2026 (IST), engineering control-plane
upgrade on branch `engineering-control-plane` (baseline tag `baseline-v1.0`)._

Status vocabulary: **Implemented** (code exists) · **Unit tested** · **Integration tested** (real
orchestrator + warehouse, offline API simulator) · **Locally verified** (run by hand in the build
environment, SQLite and/or PostgreSQL 16) · **Externally verified** (against real external
services) · **Deployed** · **Pending authorization**.

## Status by capability

| Capability | Implemented | Unit | Integration | Local | External | Deployed |
|---|---|---|---|---|---|---|
| YouTube client, quota ledger, retries, pagination | ✅ | ✅ | ✅ (simulator) | ✅ | ❌ no API key; **blocked by compliance** | ❌ |
| Hybrid discovery (charts, rotating search, refresh) | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| Bronze / Silver / Gold + migrations v1, v2 | ✅ | ✅ | ✅ | ✅ SQLite + PG16 | ❌ Supabase not created | ❌ |
| Topic intelligence, growth, lifecycle | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| 14-check DQ framework, gate, quarantine (now also on FAIL) | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| 17-stage orchestrator, lock, replay, abandoned detection | ✅ | ✅ | ✅ | ✅ (incl. a killed process) | ❌ | ❌ |
| Persisted structured logs (schema v2) | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| Data-source compliance switch (live ingestion off by default) | ✅ | — | ✅ | ✅ | n/a | ❌ |
| Control plane: 7 modules | ✅ | ✅ (rules) | ✅ (AppTest per module, empty + error states) | ✅ screenshots on PG as reader role | ❌ | ❌ |
| Operator auth + run/replay dispatch | ✅ | ✅ | — | ⚠️ dispatch path unit-tested with mocked GitHub API only | ❌ | ❌ |
| Least-privilege roles (incl. Bronze column grant) | ✅ | — | ✅ PG test | ✅ | ❌ Supabase pooler untested | ❌ |
| CI / scheduled ingestion workflows | ✅ | — | — | ✅ YAML validated, steps run locally | ❌ never ran on GitHub | ❌ |
| Docker / compose | ✅ | — | — | ❌ no Docker daemon in build env | ❌ | ❌ |

## This upgrade — completed

Phase 0 audit (`docs/audit/phase0_audit.md`) → findings F1–F10, all addressed or documented.

Backend changes (incremental, each tested):

| Change | Why | Compatibility |
|---|---|---|
| Migration v2 `pipeline_log_records` + `RunLogCollector` | log search needs persisted logs | additive; v1 databases upgrade in place (tested) |
| Quarantine persisted when the gate FAILs | rejected rows behind a closed gate were not inspectable | Gold still blocked; one test expectation updated |
| `orchestration/dag.py` stage/dataset declaration | DAG + lineage from declared metadata, not UI guesses | read-only; tested against `STAGES` |
| `sources.youtube.live_ingestion_enabled` (default false) + approval reference | YouTube derived-metrics / storage policy | live runs recorded as `skipped`; sample/replay unaffected |
| Execution clock for timestamps; real elapsed duration | simulated runs had incoherent end times / zero durations | identical for live runs |
| `simulate --chaos` fault injection | exercise gate, quarantine, retries, partial runs | opt-in flag |
| `catalog.py` (moved from the dictionary script), `quality.check_catalog()`, `GATE_POLICY` | single source for UI + docs | descriptive only |
| `migrations/002_roles.sql` → `migrations/roles.sql`; column-level Bronze metadata grant | free version number for schema v2; catalog needs Bronze metadata | payload still not readable |
| `ingest.yml`: `action`/`run_id` inputs, env-passed, validated; schedule gated by repo variable | authorised replay; no runs before approval | manual dispatch still works |
| `docker-compose.yml` password via variable | secret scan failed (F1) | — |

Unchanged: ingestion, topic engine, Gold builder logic, metrics, DQ rules, retention tiers,
all CLI commands (one new flag), all previous tests (two expectations updated, see above).

## Test results

| Run | Before (baseline re-run) | After |
|---|---|---|
| `ruff check src tests scripts streamlit_app.py` | passed | passed |
| `python scripts/check_secrets.py` | **FAILED** (F1) | passed |
| `pytest` (SQLite) | 108 passed | **149 passed, 1 skipped** (PostgreSQL-only role test) |
| `pytest` (PostgreSQL 16) | 101 passed, 7 skipped | **136 passed, 14 skipped** (page smoke tests need the SQLite fixture) |
| Coverage (views excluded as before) | 93 % of 3,091 stmts | **93 % of 3,799 stmts** |
| SQL migrations 001 + 002 via `psql -v ON_ERROR_STOP=1` | 001 OK | 001 + 002 OK |
| Screenshot smoke, 7 modules + 2 deep links, PostgreSQL as `trendpulse_reader` | 7 old pages OK | 9 captures, **0 exceptions** |

New tests: `tests/unit/test_control_plane_rules.py` (DAG declaration, health rules, recovery
availability, operator auth/lockout, dispatch validation), `tests/integration/test_control_plane.py`
(fault-injected scenario; runs filters/pagination, DAG states incl. running/interrupted, log
filters, catalog/schema/samples, quality + quarantine, lineage, observability, conditions, empty
warehouse, 7 module renders, empty + error states, reader-role permissions on PostgreSQL), plus
migration upgrade, log persistence and timestamp-coherence tests.

## Demo dataset used for screenshots (local PostgreSQL)

`trendpulse simulate --cycles 120 --chaos` → 117 success, 2 dq_failed (corrupted metrics; broken
schema), 1 partial (JP outage, retries exhausted); then one `run --sample` killed with SIGKILL
(→ genuinely `abandoned` with 15+ Bronze responses), the stale lock released per the runbook, and
one live `run` recorded as `skipped` by the compliance switch. All SAMPLE data.

## Performance findings

Reader-role query latency on PG16 (≈122 runs, ≈93k video facts): health 17 ms, run page 8 ms,
DAG state 11 ms, catalog 51 ms, log search 4 ms, lineage metrics 20 ms, Silver partitions 68 ms.
Samples are capped at 200 rows; logs/runs/quarantine paged; catalog uses planner estimates above
200k rows. Streamlit caches 60–120 s.

## Security findings

See `docs/security_review.md`. Verified on PG16: reader INSERT/DELETE → `ReadOnlySqlTransaction`;
reader `SELECT payload_gz` → `InsufficientPrivilege`; ingest role migrates without DB-level CREATE.
Residual: single shared operator passphrase; Supabase pooler + custom roles untested.

## Known issues / limitations

- No real YouTube API execution; **live use blocked pending Analytics & Reporting approval**.
- Discovered-topic names can be truncated ("starline" vs "starline t20").
- JP/KR titles yield few discovered topics (no segmentation).
- Streamlit dispatch path not exercised against real GitHub.
- Simulated history: chart bars are thin because runs are 30 min apart on a time axis.

## Deployment status

| Target | Status |
|---|---|
| GitHub repository | ⏳ pending authorization (no GitHub credentials in build env) |
| Supabase project | ⏳ pending authorization |
| Streamlit Community Cloud | ⏳ pending — **no public URL exists** |
| Live ingestion | ⛔ disabled pending YouTube compliance approval |

## Required manual actions

1. **Compliance first**: submit the YouTube quota-extension / audit request for the analytics use
   case (docs/data_source_compliance.md); or decide on an alternative source (proposal only).
2. Create the GitHub repository and push this branch (bundle/zip in your TrendPulse folder).
3. Create the Supabase project; run `migrations/001_initial_schema.sql`,
   `migrations/002_pipeline_log_records.sql`, then `migrations/roles.sql` with your passwords.
4. GitHub secret `DATABASE_URL` (ingest role). Keep `TRENDPULSE_LIVE_INGESTION_ENABLED` unset
   until approval; then set it and `TRENDPULSE_COMPLIANCE_APPROVAL_REFERENCE`, add `YOUTUBE_API_KEY`.
5. Deploy `streamlit_app.py` on Streamlit Community Cloud with `DASHBOARD_DATABASE_URL` (reader role).
   Optional operator secrets for run/replay dispatch.
6. Run the pre-deployment checklist in docs/deployment.md; verify the public URL.

## Remaining tasks

- [ ] Compliance decision (blocking for live data)
- [ ] External verification: CI on GitHub, Supabase roles via pooler, Streamlit deployment
- [ ] First real API run after approval; review DQ results on live payloads
- [ ] Optional: identity-provider auth for multiple operators; JP/KR tokenisers
