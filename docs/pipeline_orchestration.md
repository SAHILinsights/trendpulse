# Pipeline orchestration

Implementation: `src/trendpulse/orchestration/pipeline.py`. Entry points: `trendpulse run`,
`trendpulse replay --run-id`, `trendpulse simulate` (sample data only).

## Why a Python orchestrator (not Airflow/Prefect/Dagster)?

The workload is one DAG of 17 strictly sequential stages every 30 minutes. A scheduler
service would need always-on infrastructure that the free tiers don't provide. GitHub Actions
supplies the trigger, isolation, logs and secrets; the orchestrator supplies everything a
scheduler would otherwise give us — run IDs, stage state, retries, locks, idempotency, run
history, replay — persisted in the warehouse where the dashboard can show it. Swapping in a
real scheduler later is a matter of calling `Pipeline.run()` from a task.

## Stages

| # | Stage | Retries | Notes |
|---|---|---|---|
| 1 | `start_execution` | – | run row inserted (`status=running`), lock acquired |
| 2 | `load_configuration` | 1 | validated YAML + env overrides; migrations applied before the run; **enforces the data-source compliance switch** (live YouTube runs are recorded as `skipped` while disabled) |
| 3 | `check_quota_budget` | – | seeds tracker with units used this Pacific day; skips run if budget is spent; fails fast without API key |
| 4 | `discover_regional_charts` | client-level | per region × category; unsupported charts skipped |
| 5 | `discover_topic_search` | client-level | only when due (`search_interval_minutes`); rotation cursor persisted |
| 6 | `fetch_video_metrics` | client-level | `videos.list` refresh of tracked search videos |
| 7 | `save_bronze` | – | verifies every fetched response is persisted (they're written on arrival) |
| 8 | `validate_schema` | – | payload profile → `schema_drift` |
| 9 | `transform_silver` | 2 (DB) | parse, record checks, dedupe, persist Silver (+ Parquet lake locally) |
| 10 | `extract_topics` | – | taxonomy + discovery + stable IDs |
| 11 | `data_quality` | 1 (DB) | dataset checks, audit rows, **gate** |
| 12 | `quarantine_records` | 2 (DB) | persist rejected rows — also runs when the gate FAILs |
| 13 | `publish_gold` | 2 (DB) | **one transaction**: dims, facts, serving tables, publication |
| 14 | `generate_gold_aggregates` | – | verifies serving tables reflect this run |
| 15 | `update_execution_metadata` | 1 | counts, status, DQ status, retention/compaction |
| 16 | `publish_execution_status` | 1 | `data/status/last_run.json` + GitHub job summary table |
| 17 | `send_notifications` | 1 | failure / recovery / optional DQ-warning alerts |

Stages 13 and 14 cover "update historical observations" and "generate Gold aggregates": they
share one transaction on purpose, so history and serving tables can never diverge.

Stages 15–17 always run (even after a failure) so failures are recorded and alerted.

## Run statuses

| Status | Meaning | Gold |
|---|---|---|
| `success` | all stages succeeded | published |
| `partial` | quota budget reached or some API calls failed, remaining data valid | published |
| `dq_failed` | quality gate closed | **unchanged** |
| `failed` | a stage raised after retries | **unchanged** (transaction rolled back) |
| `skipped` | no quota left today / no data | unchanged |
| `skipped_locked` | another execution holds the lock (not persisted as a run) | unchanged |
| `abandoned` | a `running` row older than the lock TTL (process died) | unchanged; replayable |

## Idempotency & recovery

* Facts are keyed by `run_id` and inserted with `ON CONFLICT DO NOTHING`; replay never
  duplicates history (`test_replay_is_idempotent`).
* Bronze is committed per API response, so a crash after ingestion loses nothing:
  `trendpulse replay --run-id <id>` rebuilds Silver → Gold from Bronze and updates the original
  run row (`test_crash_during_publication_rolls_back_and_replay_recovers`).
* A replay may not overwrite serving tables with a snapshot older than the published one.

## Concurrency

1. GitHub Actions `concurrency: trendpulse-ingestion` with `cancel-in-progress: false` queues a
   second trigger instead of running it in parallel.
2. Database lock `pipeline_lock` (TTL 50 min, below the 30-minute cadence + 25-minute job
   timeout headroom) protects against anything else (local runs, manual dispatch). An expired
   lock from a crashed job is taken over atomically.

## Scheduling

`.github/workflows/ingest.yml` runs at minutes 7 and 37 of every hour (off the top-of-hour peak
when GitHub queues are longest) plus `workflow_dispatch` with an optional `force_search` input.
GitHub's cron is best-effort: delays of several minutes are normal, and GitHub disables
scheduled workflows in repositories with no activity for 60 days — the dashboard's freshness
indicator and the `stale_data` check make either visible.

## Observability

* Structured logs (JSON in CI) carrying `run_id` and `stage` on every line; secrets redacted.
* `pipeline_runs`, `pipeline_stage_runs`, `api_request_log`, `dq_results` drive the Control Center.
* Job summary in the GitHub Actions UI with a stage table per run.
* `trendpulse status` prints recent runs and platform health from any shell.

## Declared DAG (`orchestration/dag.py`)

The executor runs `STAGES` in order; `dag.py` declares, for every stage, its group, upstream
dependencies, run condition (`on success`, `always after the gate`, `only if the gate passed`,
`always`) and the datasets it reads and writes. The control plane draws the DAG and the dataset
lineage only from this declaration, and `tests/unit/test_control_plane_rules.py` fails if it ever
disagrees with `STAGES` (names, order, acyclicity, known tables).

## Persisted logs (schema v2)

`RunLogCollector` attaches to the `trendpulse` logger for the duration of a run, captures INFO+
records of that run (matched on the run-id context), redacts secrets, and flushes them to
`pipeline_log_records` after every stage — so logs survive a later crash. Each record carries
`run_id`, `stage`, level, logger, message and the formatted stack trace for exceptions.
Retention: 30 days.

## Execution clock

Run, stage, API-log and log timestamps use *run start + real elapsed time*. For live runs this is
wall-clock time; for `trendpulse simulate` it keeps simulated runs internally coherent; replays
use wall-clock time because they really execute later. Durations are always real elapsed time.

## Fault injection

`trendpulse simulate --chaos` injects, on a fixed schedule: corrupted metrics (gate FAIL),
broken payload schema (schema-drift FAIL), a regional outage (retries exhausted → `partial`) and
flaky requests (client retries succeed). These exercise the real failure paths; nothing is
mocked in the orchestrator.

## Recovery from the control plane

**Execution History & Recovery** lists, per execution, the actions the backend actually supports
and why others are unavailable:

| Action | Backed by | Available when |
|---|---|---|
| Replay from Bronze | `trendpulse replay --run-id` (stages 8–17, idempotent) | failed / dq_failed / partial / abandoned run with Bronze still retained (2 days) |
| Reconcile interrupted execution | same replay | abandoned run with Bronze |
| Start a new execution | `trendpulse run` | always (recorded as skipped while live ingestion is disabled) |
| Retry a single stage | — | never: stages share in-memory state; replay is the supported mechanism |
| Historical backfill | — | never: the API has no historical charts |

Actions run in GitHub Actions via `workflow_dispatch` (`action`, `run_id` inputs), only for a
signed-in operator — see [security_review.md](security_review.md).
