-- TrendPulse least-privilege database roles (manual, not versioned: contains passwords).
-- Run ONCE, by hand, as the `postgres` user in the Supabase SQL editor
-- (it contains passwords, so it is not applied by the pipeline).
-- 1. Replace both CHANGE_ME_* values with long random passwords
--    (e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
-- 2. Run the whole script.
-- 3. Use the roles in connection strings (Supabase pooler user format:
--    `<role>.<project-ref>`):
--      GitHub secret  DATABASE_URL            -> trendpulse_ingest
--      Streamlit secret DASHBOARD_DATABASE_URL -> trendpulse_reader

-- Pipeline role: owns the trendpulse schema (runs migrations, writes data).
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trendpulse_ingest') THEN
    CREATE ROLE trendpulse_ingest LOGIN PASSWORD 'CHANGE_ME_INGEST_PASSWORD' NOINHERIT;
  END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS trendpulse;
ALTER SCHEMA trendpulse OWNER TO trendpulse_ingest;
-- If migration 001 already ran as `postgres`, hand the tables over:
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT format('%I.%I', schemaname, tablename) AS t FROM pg_tables WHERE schemaname = 'trendpulse' LOOP
    EXECUTE 'ALTER TABLE ' || r.t || ' OWNER TO trendpulse_ingest';
  END LOOP;
  FOR r IN SELECT format('%I.%I', sequence_schema, sequence_name) AS s FROM information_schema.sequences
           WHERE sequence_schema = 'trendpulse' LOOP
    EXECUTE 'ALTER SEQUENCE ' || r.s || ' OWNER TO trendpulse_ingest';
  END LOOP;
END $$;
ALTER ROLE trendpulse_ingest SET statement_timeout = '120s';

-- Dashboard role: read-only, cannot write anything anywhere.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trendpulse_reader') THEN
    CREATE ROLE trendpulse_reader LOGIN PASSWORD 'CHANGE_ME_READER_PASSWORD' NOINHERIT;
  END IF;
END $$;
GRANT USAGE ON SCHEMA trendpulse TO trendpulse_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA trendpulse TO trendpulse_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE trendpulse_ingest IN SCHEMA trendpulse GRANT SELECT ON TABLES TO trendpulse_reader;
ALTER ROLE trendpulse_reader SET default_transaction_read_only = on;
ALTER ROLE trendpulse_reader SET statement_timeout = '30s';
-- Bronze: the control plane may read lineage METADATA (ids, endpoint, region, checksum, size,
-- timestamps) but never the raw payload column.
REVOKE SELECT ON trendpulse.bronze_api_responses FROM trendpulse_reader;
GRANT SELECT (response_id, run_id, endpoint, discovery_method, discovery_query, region_code, category_id,
              page_index, request_params, http_status, item_count, payload_sha256, fetched_at)
  ON trendpulse.bronze_api_responses TO trendpulse_reader;
