# Technical design & technology decisions

## Goals and non-goals

**Goals:** continuous, quota-safe collection of YouTube popularity signals for 10+ countries;
durable history; topic-level trend analytics (growth, velocity, acceleration, lifecycle);
explicit data quality; observable, recoverable runs; $0 infrastructure; a public dashboard.

**Non-goals:** sub-minute latency (the API and quota make 30 min the sensible cadence); full
coverage of YouTube (impossible through the API — and we say so); ML-heavy NLP (no paid LLMs,
must run in a 25-minute CI job).

## Decisions

| Decision | Chosen | Alternatives considered | Why |
|---|---|---|---|
| Language | Python 3.11 | — | Ecosystem for data + NLP + Streamlit |
| Scheduler/runtime | GitHub Actions cron + `workflow_dispatch` | Airflow/Prefect/Dagster, cron VM, AWS | Free, no servers, secrets + logs + concurrency groups built in. Orchestration logic lives in our own code so it is testable and portable |
| Orchestrator | Custom 17-stage Python orchestrator | Prefect flows | One linear DAG; we need run/stage metadata *in the warehouse* for the dashboard anyway |
| Warehouse | PostgreSQL on Supabase | BigQuery sandbox, MotherDuck, Neon | Free, durable, transactional, `ON CONFLICT`, roles for least privilege, reachable from GHA and Streamlit |
| Local fallback | SQLite via the same SQLAlchemy Core | DuckDB file | DuckDB allows one writer process and locks readers out — the dashboard and pipeline must coexist locally. SQLite with WAL handles it |
| DB access layer | SQLAlchemy Core (no ORM) | raw SQL, ORM | Dialect-portable upserts, schema translation, connection pooling; set-based code without ORM overhead |
| In-run analytics | DuckDB over pandas | pandas groupby, Polars | Readable SQL aggregations (topic × region incl. global dedupe) executed columnar, zero-copy |
| Lake format | Parquet (Silver, Gold export) + gzip JSON (Bronze) | CSV | Typed, compressed, DuckDB-native |
| Bronze in Postgres | gzip `bytea` with 2-day retention | JSONB | 6–10× smaller; Bronze is for replay, not querying |
| Topic NLP | Keyword taxonomy + scikit-learn n-gram TF-IDF + char-n-gram similarity | BERTopic/embeddings, LLM APIs | Deterministic, explainable, millisecond-fast, no GPU, no paid API; stable IDs are trivial with deterministic keys |
| Topic identity | `sha1(canonical_key)` + alias table | sequence IDs | Re-deriving after a rebuild yields the same IDs; aliases absorb phrase variants |
| Scoring | Log-damped absolute composite score | per-run normalised score | Comparable across time (growth is meaningful), breadth rewarded over one viral video |
| DQ framework | In-house, config-driven | Great Expectations, Soda | 14 checks with quarantine semantics + audit table, no heavyweight dependency or extra infra; easy to read in an interview |
| Config | YAML + pydantic validation + env overrides | env-only | Reviewable in PRs; CI validates it; secrets never in YAML |
| Dashboard | Streamlit + Plotly | Dash, Superset, Metabase | Free public hosting on Community Cloud, Python-only, interactive charts |
| CI | GitHub Actions: ruff, pytest on SQLite + Postgres service, DDL check, E2E smoke, gitleaks + custom secret scan | — | Proves Postgres compatibility on every push |
| Packaging | `pyproject.toml` + `requirements.txt` + Dockerfile | Poetry | Streamlit Cloud installs `requirements.txt` natively |

## Key trade-offs

* **Search vs. charts.** `search.list` is 100× the cost of `videos.list`. We rotate searches
  (6 every 3 h) and convert their results into cheap tracked refreshes. Consequence: topic
  discovery via search is sampled, so we label it.
* **Serving tables vs. on-the-fly queries.** Pre-computing rankings/growth inside the
  publication transaction doubles some logic but keeps the public dashboard fast and the free
  database idle.
* **Absolute score.** Weights are opinionated; they are configurable and documented, and raw
  components (videos, channels, views/hour, engagement) are always shown next to the score.
* **History compaction.** Free-tier storage and API policy forbid keeping everything; we compact
  by whole runs to keep comparisons exact.

## Scalability path

* More regions/seeds: linear quota cost; a Google quota increase is the bottleneck, not code.
* Larger warehouse: move to a paid Postgres or MotherDuck; table partitioning by `observed_at`
  for the fact tables; serving tables unchanged.
* Heavier NLP: swap `topics/discovery.py` for embedding clustering — the registry/alias layer
  already isolates identity from extraction.
* Orchestrator: `Pipeline.run()` can be wrapped as a Prefect/Dagster task without changes.
