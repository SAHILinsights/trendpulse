# LinkedIn launch post — DRAFT

> Publish only after the public dashboard URL is verified and shows **Live API-derived** data.
> Replace the bracketed placeholders.

I built TrendPulse, an end-to-end data engineering platform that tracks how YouTube topics
emerge, grow, peak and fade across 10 countries.

Every 30 minutes a GitHub Actions job pulls regional popular charts and rotating topic searches
from the YouTube Data API, under a quota guard that keeps usage below 10,000 units/day.

What's under the hood:
• Bronze → Silver → Gold medallion warehouse on PostgreSQL (Supabase), with Bronze persisted per response so any run can be replayed
• A 14-check data-quality gate with quarantine and an audit table. A failed check never touches published data
• Topic inference using a keyword taxonomy plus TF-IDF keyphrase discovery, with deterministic IDs so topics keep their identity over time
• Growth over 6 windows (30 min → 30 days), plus velocity, acceleration and lifecycle stages
• A 17-stage Python orchestrator with locks, retries, idempotent facts and single-transaction publication
• 108 automated tests (93% coverage), run on SQLite and PostgreSQL in CI
• A Streamlit dashboard with a Data Engineering Control Center showing runs, stages, DQ results and quota

Total infrastructure cost: $0, no AWS.

Dashboard: [PUBLIC URL]
Code: [GITHUB URL]

#DataEngineering #Python #PostgreSQL #DataQuality #Streamlit #YouTubeAPI
