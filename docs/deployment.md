# Deployment (free tier, no AWS)

Target topology: **GitHub** (code, CI, scheduled pipeline) → **Supabase** (PostgreSQL) →
**Streamlit Community Cloud** (public dashboard). Total cost: $0 within free-tier limits.

You need four accounts: Google (Cloud Console, for the API key), GitHub, Supabase, Streamlit
Community Cloud (sign in with GitHub). None needs a credit card for this setup.

## 1. YouTube Data API key (≈5 min)

1. <https://console.cloud.google.com/> → create a project, e.g. `trendpulse`.
2. *APIs & Services → Library* → enable **YouTube Data API v3**.
3. *APIs & Services → Credentials → Create credentials → API key*.
4. *Edit API key → API restrictions → Restrict key → YouTube Data API v3*. (Application
   restriction "None" — GitHub Actions has no fixed IP.)
5. Optional but recommended for long-term history: review and accept the YouTube API
   "Analytics & Reporting" policy amendment for the project (see
   [api_quota_strategy.md](api_quota_strategy.md#compliance)); otherwise set
   `retention.topic_obs_max_days: 30`.

## 2. Supabase database (≈10 min)

1. <https://supabase.com/dashboard> → *New project* (free plan). Save the database password.
2. *SQL Editor* → paste and run `migrations/001_initial_schema.sql`.
3. Open `migrations/roles.sql`, replace both `CHANGE_ME_*` passwords with long random values
   (`python -c "import secrets; print(secrets.token_urlsafe(32))"`), run it.
4. *Project Settings → Database → Connection string → Session pooler* (IPv4-compatible; GitHub
   runners and Streamlit Cloud may lack IPv6, so do **not** use the direct `db.<ref>` host).
   Build two URLs from it:

   ```
   DATABASE_URL           = postgresql+psycopg://trendpulse_ingest.<project-ref>:<ingest-pw>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
   DASHBOARD_DATABASE_URL = postgresql+psycopg://trendpulse_reader.<project-ref>:<reader-pw>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
   ```

   URL-encode passwords containing `@ : / ? #`. If the pooler rejects the custom role, test with
   `psql "<url>"`; as a last resort use the `postgres` user for `DATABASE_URL` only — never for the
   dashboard.
5. Free projects pause after ~7 days without activity; the 30-minute pipeline keeps the project
   active. If it was paused, resume it from the dashboard.

## 3. GitHub repository (≈5 min)

```bash
cd trendpulse-youtube-trending-intelligence
git init -b main && git add . && git commit -m "TrendPulse: initial platform"
gh repo create trendpulse-youtube-trending-intelligence --public --source . --push
# or create the repo on github.com and: git remote add origin … && git push -u origin main
```

*Settings → Secrets and variables → Actions → New repository secret*:

| Secret | Required | Value |
|---|---|---|
| `YOUTUBE_API_KEY` | yes | key from step 1 |
| `DATABASE_URL` | yes | ingest URL from step 2 |
| `TRENDPULSE_ALERT_WEBHOOK_URL` | no | Slack/Discord incoming webhook |
| `TRENDPULSE_SMTP_HOST` / `_USER` / `_PASSWORD`, `TRENDPULSE_ALERT_EMAIL_TO` | no | email alerts |

Then *Actions* → enable workflows → **Scheduled ingestion → Run workflow** (tick
*force_search* for the first run). Check the job summary: status `SUCCESS`, quota used, stage
table. The `CI` workflow runs on every push.

## 4. Streamlit Community Cloud (≈5 min)

1. <https://share.streamlit.io> → *Create app* → *Deploy a public app from GitHub*.
2. Repository `…/trendpulse-youtube-trending-intelligence`, branch `main`, main file
   `streamlit_app.py`, Python 3.11 (*Advanced settings*).
3. *Advanced settings → Secrets*:

   ```toml
   DASHBOARD_DATABASE_URL = "postgresql+psycopg://trendpulse_reader.<ref>:<reader-pw>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require"
   ```

   (Optional operator trigger: `TRENDPULSE_GITHUB_DISPATCH_TOKEN` — fine-grained PAT, this repo
   only, *Actions: Read and write*; `TRENDPULSE_GITHUB_REPO = "<user>/<repo>"`;
   `TRENDPULSE_OPERATOR_PASSPHRASE`.)
4. Deploy. Pick a custom subdomain such as `trendpulse-<you>.streamlit.app`.
5. Verify: the sidebar badge reads **Live API-derived**, the Control Center shows the GitHub
   runs, freshness < 60 min.

Streamlit apps sleep after a period without visitors; the first visit wakes them (≈30 s).
Data is unaffected — it lives in Supabase.

## 5. Post-deployment checklist

- [ ] `CI` green on `main`
- [ ] ≥ 3 successful `Scheduled ingestion` runs, ~30 min apart
- [ ] Control Center: success rate, quota (< 9,000/day), DQ results visible
- [ ] Dashboard reachable logged-out in a private window
- [ ] Repository → *Settings → Code security*: enable secret scanning + push protection
- [ ] Update README badges/URLs and DEVELOPMENT_PROGRESS.md deployment status

## Local alternatives

* Everything local with SQLite: `pip install -r requirements-dev.txt && trendpulse run` (needs
  `YOUTUBE_API_KEY` in `.env`) or `trendpulse simulate --cycles 96` (sample data), then
  `streamlit run streamlit_app.py`.
* Docker: `docker compose up -d db dashboard` then
  `docker compose run --rm pipeline trendpulse run`.

## Control-plane & compliance settings (added with the engineering upgrade)

| Where | Name | Purpose |
|---|---|---|
| SQL editor | `migrations/002_pipeline_log_records.sql` | schema v2 (also applied automatically by `trendpulse migrate` / every run) |
| SQL editor | `migrations/roles.sql` | roles incl. column-level Bronze metadata grant (renamed from `002_roles.sql`) |
| GitHub → Variables | `TRENDPULSE_LIVE_INGESTION_ENABLED` | `true` only after the YouTube use case is approved; until then scheduled runs do not start |
| GitHub → Variables | `TRENDPULSE_COMPLIANCE_APPROVAL_REFERENCE` | approval ticket/date; required by the pipeline when live ingestion is on |
| Streamlit secrets (optional) | `TRENDPULSE_GITHUB_DISPATCH_TOKEN`, `TRENDPULSE_GITHUB_REPO`, `TRENDPULSE_OPERATOR_PASSPHRASE` | operator run/replay dispatch; omit all three to keep the dashboard purely read-only |

Pre-deployment validation checklist:

- [ ] `trendpulse validate-config` and `trendpulse quota-plan` pass
- [ ] `psql "$DATABASE_URL" -c 'select max(version) from trendpulse.schema_migrations'` → 2
- [ ] reader role: `SELECT` works, `INSERT` fails, `SELECT payload_gz` fails
- [ ] `ingest.yml` manual dispatch with `action=run` records a `skipped` run while compliance is pending
- [ ] dashboard sidebar shows *Warehouse PostgreSQL · schema v2* and *Live YouTube ingestion: disabled*
- [ ] no secret appears in the Actions logs (`pipeline_log_records` is redacted too)

## Demo data while live ingestion is pending (`sample-data.yml`)

*Actions → Load SAMPLE demo data → Run workflow* (type `SAMPLE` to confirm) backfills up to 96
simulated 30-minute cycles from the offline API simulator into the warehouse, using the
`DATABASE_URL` secret. No YouTube API call is made. Runs are stored with
`data_source = 'sample_fixture'` and the dashboard shows a **SAMPLE DATA** banner. Before enabling
live ingestion, start from an empty warehouse (drop and recreate the `trendpulse` schema, then
re-run the migrations and `roles.sql`) so sample and live history never mix.
