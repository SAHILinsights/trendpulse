# Security review

Scope: TrendPulse pipeline, warehouse, CI/CD and public dashboard (November 2026).

## Assets and threats

| Asset | Threat | Control |
|---|---|---|
| YouTube API key | Leak → quota theft | Only in GitHub Actions secrets / local `.env` (git-ignored). Never logged: the client strips `key` from logged params and a log filter redacts `AIza…`/`key=` patterns. Key restricted to YouTube Data API v3 in Google Cloud |
| Database credentials | Leak → data tampering | Ingest URL only in GitHub secrets; dashboard uses a separate **read-only** role (`default_transaction_read_only=on`, `SELECT` only, no Bronze access). Log redaction masks passwords in Postgres URLs |
| Warehouse data | Exposure via Supabase REST API | Tables live in schema `trendpulse`, not exposed by PostgREST; `anon`/`authenticated` revoked |
| Pipeline execution | Public visitors triggering expensive runs | Dashboard never runs ingestion. Optional trigger requires a server-side fine-grained token **and** an operator passphrase (constant-time compare); workflow concurrency + DB lock still apply |
| CI | Secrets committed | `.gitignore` for `.env`/`secrets.toml`; `scripts/check_secrets.py` + gitleaks (history) on every push; enable GitHub push protection |
| Supply chain | Malicious dependency | Pinned lower/upper bounds, small dependency set, Dependabot recommended |
| Dashboard | XSS through video titles | All user/API strings rendered via `st.dataframe` or `html.escape` before `unsafe_allow_html` |
| SQL injection | Crafted search input | All queries are SQLAlchemy Core with bound parameters; the free-text search is a bound `LIKE` parameter |
| Sample data | Synthetic data mistaken for real | Runs record `data_source`; dashboard shows a SAMPLE banner; sample writes to Postgres refused without explicit flag |

## Least privilege

| Principal | Privileges |
|---|---|
| `trendpulse_ingest` (GitHub Actions) | Owns schema `trendpulse` (DDL for migrations, DML). No other schemas |
| `trendpulse_reader` (Streamlit) | `USAGE` + `SELECT` on schema, read-only transactions, 30 s statement timeout, no Bronze |
| GitHub workflow token | `permissions: contents: read` |
| Optional dispatch PAT | Fine-grained, single repo, Actions read/write only |

Verified locally on PostgreSQL 16: the reader role's `INSERT` fails with
`ReadOnlySqlTransaction`; `SELECT` on Bronze fails with `InsufficientPrivilege`; the ingest role
runs migrations and upserts without database-level `CREATE`.

## Data protection

Only public video/channel metadata and statistics are stored; no viewer personal data, no
OAuth. Raw payloads are kept 2 days; metadata not refreshed for 30 days is deleted (YouTube
Developer Policies).

## Residual risks / recommendations

* Supabase pooler supports custom roles, but verify after setup; falling back to `postgres` for
  the dashboard would remove the read-only guarantee — don't.
* Rotate the API key and DB passwords if a secret is ever printed in a public log.
* Enable Dependabot alerts and GitHub secret-scanning push protection on the repository.
* Streamlit Community Cloud apps are public by design; do not add private data sources.
