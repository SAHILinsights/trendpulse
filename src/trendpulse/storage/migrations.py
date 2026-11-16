"""Versioned schema migrations.

Each migration is an ordered, idempotent step recorded in
``schema_migrations``. The runner is safe to call at the start of every
pipeline execution: already-applied versions are skipped, and each version
runs in its own transaction.

The equivalent PostgreSQL DDL is exported to ``migrations/*.sql`` by
``scripts/export_sql_migrations.py`` for review or for applying by hand in
the Supabase SQL editor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.engine import Connection

from trendpulse.logging_utils import get_logger
from trendpulse.storage import schema as s
from trendpulse.storage.db import Warehouse, utcnow

log = get_logger("migrations")


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: Callable[[Connection, Warehouse], None]


def _create_schema(conn: Connection) -> None:
    # Checking first lets a least-privilege role (schema owner, no CREATE on the database) run migrations.
    conn.execute(text(
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = '{s.PG_SCHEMA}') THEN "
        f"CREATE SCHEMA {s.PG_SCHEMA}; END IF; END $$;"))


def _v1_initial(conn: Connection, wh: Warehouse) -> None:
    if wh.is_postgres:
        _create_schema(conn)
        # Never expose the warehouse through Supabase's auto-generated REST API.
        conn.execute(text(
            f"DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN "
            f"REVOKE ALL ON SCHEMA {s.PG_SCHEMA} FROM anon; END IF; "
            f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN "
            f"REVOKE ALL ON SCHEMA {s.PG_SCHEMA} FROM authenticated; END IF; END $$;"
        ))
    later = {name for names in TABLES_BY_VERSION.values() for name in names}
    tables = [t for t in s.metadata.sorted_tables if t.name != "schema_migrations" and t.name not in later]
    s.metadata.create_all(conn, tables=tables, checkfirst=True)


def _v2_pipeline_logs(conn: Connection, wh: Warehouse) -> None:
    """Additive: run-scoped structured log records (no change to existing tables)."""
    s.metadata.create_all(conn, tables=[s.metadata.tables[n] for n in TABLES_BY_VERSION[2]], checkfirst=True)


# Tables introduced after the initial schema, by migration version (used by v1 and the SQL exporter).
TABLES_BY_VERSION: dict[int, list[str]] = {2: ["pipeline_log_records"]}

MIGRATIONS: list[Migration] = [
    Migration(1, "initial medallion schema (bronze/silver/gold + operational metadata)", _v1_initial),
    Migration(2, "pipeline_log_records: persisted run-scoped structured logs", _v2_pipeline_logs),
]


def _ensure_migrations_table(wh: Warehouse) -> None:
    with wh.begin() as conn:
        if wh.is_postgres:
            _create_schema(conn)
        s.schema_migrations.create(conn, checkfirst=True)


def applied_versions(wh: Warehouse) -> set[int]:
    with wh.connect() as conn:
        return {r[0] for r in conn.execute(select(s.schema_migrations.c.version))}


def migrate(wh: Warehouse) -> list[int]:
    """Apply pending migrations. Returns the versions applied in this call."""
    _ensure_migrations_table(wh)
    done = applied_versions(wh)
    applied: list[int] = []
    for m in sorted(MIGRATIONS, key=lambda m: m.version):
        if m.version in done:
            continue
        log.info("applying migration %03d: %s", m.version, m.description)
        with wh.begin() as conn:
            m.apply(conn, wh)
            conn.execute(s.schema_migrations.insert().values(
                version=m.version, description=m.description, applied_at=utcnow()))
        applied.append(m.version)
    return applied


def current_version(wh: Warehouse) -> int:
    try:
        v = applied_versions(wh)
    except Exception:  # noqa: BLE001 - table may not exist yet
        return 0
    return max(v) if v else 0
