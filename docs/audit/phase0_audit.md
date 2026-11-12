# Phase 0 — Repository audit (engineering control-plane upgrade)

Date: 13 Nov 2026 · Branch: `engineering-control-plane` · Baseline tag: `baseline-v1.0` (commit `534b8da`)

## Baseline re-run (before any change)

| Check | Result |
|---|---|
| `ruff check src tests scripts streamlit_app.py` | passed |
| `python scripts/check_secrets.py` | **FAILED** — `docker-compose.yml:22,30` (local-only Postgres password in a URL). The file was committed after the previous scan ran, so CI would fail on first push. |
| `pytest` (SQLite) | 108 passed, coverage 93 % (3,091 statements) |
| `pytest` (PostgreSQL 16, `TRENDPULSE_TEST_DATABASE_URL`) | 101 passed, 7 skipped (SQLite-only page smoke tests) |

## Inventory

| Area | Modules (lines) | Reuse for the control plane |
|---|---|---|
| Orchestration | `orchestration/pipeline.py` (539) — 17 `STAGES`, `pipeline_runs`, `pipeline_stage_runs`, lock, replay | Stage metadata drives the DAG, execution history, timelines |
| Storage | `storage/schema.py` (25 tables), `db.py`, `migrations.py` (v1), `retention.py` | SQLAlchemy metadata drives the catalog/schema inspector |
| Quality | `quality/framework.py` — 10 record + 4 dataset checks, `dq_results`, `quarantine_records` | Quality & governance module |
| Ingestion | `ingestion/*` — client, quota ledger, discovery, Bronze writer, simulator | API request log → observability |
| Topics / Gold | `topics/*`, `gold/*` | Trend intelligence (unchanged) |
| Dashboard | `dashboard/data.py` (33 pure query functions), `runtime.py`, `components.py`, 7 views | Queries reused; views re-organised |

## Findings

| # | Finding | Severity | Action |
|---|---|---|---|
| F1 | Secret scan fails on `docker-compose.yml` | High (CI red) | Use `${LOCAL_PG_PASSWORD:-…}` substitution |
| F2 | Logs only go to stdout — nothing to search in the UI | High for "log inspection" | Additive migration 002: `pipeline_log_records` + run-scoped logging handler |
| F3 | When the quality gate FAILs, `quarantine_records` is skipped, so the rows that caused the failure cannot be inspected | Medium (governance gap) | Persist quarantine before the gate decision; Gold still blocked |
| F4 | DAG/lineage relationships exist only implicitly (stage order in code) | Medium | Declarative stage graph (`orchestration/dag.py`) with inputs/outputs per stage, tested to match `STAGES` |
| F5 | Dashboard role cannot read any Bronze column → catalog can't show Bronze metadata | Low | Column-level grant on Bronze metadata (payload stays revoked) |
| F6 | **Compliance:** current YouTube policy says derived metrics and stored statistics require an approved "Analytics & Reporting" use case (audited developers, via quota-extension request, from 1 Jun 2026); aggregation restrictions apply to authorized data | **Blocking for live use** | Live YouTube ingestion disabled by default behind an explicit compliance flag; `docs/data_source_compliance.md` |
| F7 | Recovery: replay from Bronze exists; per-stage retry and historical backfill do not (the API has no historical charts) | Info | Expose only replay / new run, via authenticated workflow dispatch; document the rest as unsupported |
| F8 | Analytics occupy 6 of 7 pages; engineering lives in one tab-heavy page | Product | New IA: 6 engineering modules + 1 consolidated Trend Intelligence module |
| F9 | `ingest.yml` cannot replay a run | Low | Add `action`/`run_id` dispatch inputs |
| F10 | Discovered topic names can be truncated ("starline" vs "starline t20") | Low, cosmetic | Documented, unchanged |

## Plan (Phase 1)

Navigation (7 modules): Platform Overview · Pipeline Orchestrator · Data Lakehouse · Data Quality &
Governance · Lineage & Observability · Execution History & Recovery · Trend Intelligence.

New read-only query module `dashboard/ops.py` (health status, DAG state, catalog, lineage, logs,
run filters) — pure functions, unit-tested. Business rules (health status, gate decision text,
action availability) live outside page files. No changes to ingestion, topic or Gold logic.
