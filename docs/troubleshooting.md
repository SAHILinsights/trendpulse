# Troubleshooting

Start with the **Control Center** (last attempted vs last successful run, stage table, DQ tab,
API errors) or `trendpulse status`. Every log line carries the `run_id` and stage.

| Symptom | Likely cause | Fix |
|---|---|---|
| Ingestion job fails at *Check required secrets* | `YOUTUBE_API_KEY` / `DATABASE_URL` not set in repo secrets | Add them (docs/deployment.md §3) |
| `check_quota_budget` failed: "YOUTUBE_API_KEY is not set" | Running live mode without a key | Set the key, or `trendpulse run --sample` for offline demo |
| Run `skipped`: "daily quota budget exhausted" | 9,000 units used this Pacific day | Wait for reset (shown in Control Center) or lower `searches_per_cycle` |
| Many `403 quotaExceeded` in API errors | Another app shares the Google project quota | Use a dedicated project; lower `quota.daily_budget` |
| `400 keyInvalid` / `403 accessNotConfigured` | Key restricted to another API, or API not enabled | Enable YouTube Data API v3, restrict key to it |
| Some `404 videoChartNotFound` | A category chart not offered in that region | Expected and harmless; remove the category for that country in `countries.yaml` |
| Run `dq_failed` | A critical check exceeded its threshold | Control Center → Data quality: read `details`; look at quarantine payloads; fix, then `trendpulse replay --run-id <id>` |
| `volume_anomaly` FAIL after config change | Fewer regions/charts → >90 % drop vs history | Expected after a deliberate change; temporarily `quality.checks.volume_anomaly.severity: warning` |
| `schema_drift` WARNING "unexpected keys" | YouTube added fields | Usually harmless; review and add to `KNOWN_TOP_LEVEL` |
| Run `abandoned` | Job killed/timed out | Lock expires automatically; `trendpulse replay --run-id <id>` to recover the data |
| `skipped_locked` repeatedly | Stuck lock (TTL 50 min) | Wait for TTL or `DELETE FROM trendpulse.pipeline_lock;` |
| `password authentication failed` / `Tenant or user not found` | Wrong pooler user format | Pooler user must be `<role>.<project-ref>`; URL-encode the password |
| `could not translate host name db.<ref>.supabase.co` / IPv6 errors | Direct host is IPv6-only | Use the *Session pooler* host |
| Dashboard: "Warehouse unavailable" | Secret missing in Streamlit, project paused | Check `DASHBOARD_DATABASE_URL`; resume Supabase project |
| Dashboard: "SAMPLE DATA" banner in production | A sample run was written to that DB | Sample writes to Postgres are blocked unless `--allow-sample-on-postgres`; drop the schema and re-run live |
| Freshness "stale" | Scheduled workflow disabled (60 days repo inactivity) or failing | Actions tab → re-enable workflow; check last run logs |
| Growth columns empty ("no_history") | Not enough history for the window | Expected: 24h growth needs ~24 h of runs, 7d needs a week |
| Everything "baseline" | Platform < 6 h old | Expected; lifecycle starts after `min_platform_history_hours` |
| Too many "Unclassified" | Taxonomy lacks keywords for current content | Review queue → add keywords to `config/taxonomy.yaml` (IDs stay stable) |
| Supabase storage near 500 MB | Long retention windows | Lower `retention.*`, run `trendpulse retention`, `VACUUM FULL` in SQL editor |

## Useful commands

```bash
trendpulse status                       # recent runs + health JSON
trendpulse validate-config              # config + quota plan sanity check
trendpulse quota-plan                   # expected units/day
trendpulse replay --run-id <uuid>       # rebuild Silver/Gold from Bronze
trendpulse retention                    # apply retention/compaction now
trendpulse export-parquet --out data/lake/gold   # Gold -> Parquet + DuckDB views
TRENDPULSE_LOG_LEVEL=DEBUG trendpulse run --sample
```

## Control plane

| Symptom | Cause | Fix |
|---|---|---|
| Every run is `skipped` with "live YouTube ingestion is disabled pending data-source compliance approval" | Compliance switch off (default) | Expected until approval — see docs/data_source_compliance.md |
| Run `failed` at `load_configuration`: "compliance_approval_reference is empty" | Switch on without a recorded approval | Set `TRENDPULSE_COMPLIANCE_APPROVAL_REFERENCE` |
| Orchestrator shows a stage as `interrupted` | Run was abandoned (process killed) while in that stage | Execution History → Reconcile/Replay (if Bronze retained) |
| "No log records for this stage" | Run predates schema v2, or the stage was skipped | — |
| Lakehouse shows Bronze rows but samples have no payload | By design: reader has metadata-only column grant | Inspect raw payloads with the ingest role via SQL if needed |
| Recovery buttons disabled | Not signed in as operator, dispatch secrets missing, or action unsupported for this run | Hover the button / read the reason column; use the CLI equivalent shown |
| Operator sign-in locked | 5 wrong passphrases | Wait 15 minutes (per session) |
