"""Warehouse access: engine factory, upserts, reads, locks, retention.

Backends
--------
* PostgreSQL (Supabase) — production. Pooled connections, ``trendpulse``
  schema via ``schema_translate_map``, ``INSERT .. ON CONFLICT`` upserts.
* SQLite — local fallback when ``DATABASE_URL`` is unset, and the test
  backend. Same Core statements, same upsert semantics.

All timestamps are written as timezone-aware UTC datetimes. SQLite hands
them back naive; :func:`as_utc` and :meth:`Warehouse.read_df` normalise
everything back to aware UTC so callers never see mixed timezones.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Table, and_, create_engine, delete, event, insert, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from trendpulse.logging_utils import get_logger
from trendpulse.storage import schema as s

log = get_logger("storage")

CHUNK = 500


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: Any) -> datetime | None:
    if value is None or value is pd.NaT or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        value = value.to_pydatetime()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise TypeError(f"not a datetime: {value!r}")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def default_sqlite_url(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(data_dir / 'trendpulse.db').as_posix()}"


def normalise_url(url: str) -> str:
    """Accept ``postgres://`` / ``postgresql://`` URLs and pin the psycopg3 driver."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _clean_value(v: Any) -> Any:
    """Convert pandas/numpy scalars and NaN/NaT into DB-friendly Python values."""
    if v is None or v is pd.NaT or v is pd.NA:
        return None
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else as_utc(v)
    if isinstance(v, datetime):
        return as_utc(v)
    if isinstance(v, float) and math.isnan(v):
        return None
    if hasattr(v, "item") and not isinstance(v, (bytes, str)):
        try:
            v = v.item()
        except (ValueError, AttributeError):
            return v
        if isinstance(v, float) and math.isnan(v):
            return None
    try:
        if v is pd.NA or v is pd.NaT:
            return None
    except TypeError:  # pragma: no cover
        pass
    return v


def records_from_df(df: pd.DataFrame, columns: Sequence[str] | None = None) -> list[dict[str, Any]]:
    cols = list(columns) if columns is not None else list(df.columns)
    out = []
    for row in df[cols].itertuples(index=False, name=None):
        out.append({c: _clean_value(v) for c, v in zip(cols, row, strict=True)})
    return out


class Warehouse:
    def __init__(self, url: str | None = None, *, data_dir: Path | None = None, read_only: bool = False,
                 engine: Engine | None = None):
        self.read_only = read_only
        if engine is not None:
            self.engine = engine
        else:
            if not url:
                url = default_sqlite_url(data_dir or Path("data"))
            self.engine = self._make_engine(normalise_url(url), read_only)
        self.dialect = self.engine.dialect.name

    # ------------------------------------------------------------------ engine
    @staticmethod
    def _make_engine(url: str, read_only: bool) -> Engine:
        if url.startswith("sqlite"):
            if read_only and ":memory:" not in url:
                path = url.split("sqlite:///", 1)[1]
                url = f"sqlite:///file:{path}?mode=ro&uri=true"
            engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

            @event.listens_for(engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
                cur = dbapi_conn.cursor()
                if not read_only:
                    cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

            return engine

        engine = create_engine(
            url,
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=1800,
            # prepare_threshold=None keeps psycopg compatible with PgBouncer/Supavisor
            connect_args={"prepare_threshold": None, "connect_timeout": 15,
                          "application_name": "trendpulse-dashboard" if read_only else "trendpulse-pipeline"},
        ).execution_options(schema_translate_map={None: s.PG_SCHEMA})

        @event.listens_for(engine, "connect")
        def _pg_session(dbapi_conn, _):  # pragma: no cover - needs postgres
            with dbapi_conn.cursor() as cur:
                cur.execute("SET TIME ZONE 'UTC'")
                if read_only:
                    cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            dbapi_conn.commit()

        return engine

    @property
    def is_postgres(self) -> bool:
        return self.dialect == "postgresql"

    def describe(self) -> str:
        if self.is_postgres:
            u = self.engine.url
            return f"postgresql://{u.host}:{u.port}/{u.database} (schema={s.PG_SCHEMA})"
        return f"sqlite:{self.engine.url.database}"

    @contextmanager
    def begin(self):
        with self.engine.begin() as conn:
            yield conn

    @contextmanager
    def connect(self):
        with self.engine.connect() as conn:
            yield conn

    def dispose(self) -> None:
        self.engine.dispose()

    # ------------------------------------------------------------------ writes
    def _insert_stmt(self, table: Table):
        if self.is_postgres:
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        return dialect_insert(table)

    def upsert(self, conn: Connection, table: Table, rows: Iterable[dict[str, Any]],
               conflict_cols: Sequence[str], update_cols: Sequence[str] | None = None) -> int:
        """Insert rows; on primary/unique conflict update ``update_cols`` (or do nothing)."""
        self._guard_write()
        rows = [{k: _clean_value(v) for k, v in r.items()} for r in rows]
        if not rows:
            return 0
        if update_cols is None:
            update_cols = [c for c in rows[0] if c not in conflict_cols]
        n = 0
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i:i + CHUNK]
            stmt = self._insert_stmt(table).values(chunk)
            if update_cols:
                stmt = stmt.on_conflict_do_update(
                    index_elements=list(conflict_cols),
                    set_={c: getattr(stmt.excluded, c) for c in update_cols},
                )
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_cols))
            conn.execute(stmt)
            n += len(chunk)
        return n

    def insert_many(self, conn: Connection, table: Table, rows: Sequence[dict[str, Any]]) -> int:
        self._guard_write()
        rows = [{k: _clean_value(v) for k, v in r.items()} for r in rows]
        for i in range(0, len(rows), CHUNK):
            conn.execute(insert(table), rows[i:i + CHUNK])
        return len(rows)

    def _guard_write(self) -> None:
        if self.read_only:
            raise PermissionError("this Warehouse handle is read-only")

    # ------------------------------------------------------------------ reads
    def read_df(self, stmt, conn: Connection | None = None) -> pd.DataFrame:
        def _run(c: Connection) -> pd.DataFrame:
            result = c.execute(stmt)
            df = pd.DataFrame(result.fetchall(), columns=list(result.keys()))
            return normalise_datetimes(df)

        if conn is not None:
            return _run(conn)
        with self.connect() as c:
            return _run(c)

    def scalar(self, stmt, conn: Connection | None = None):
        if conn is not None:
            return conn.execute(stmt).scalar()
        with self.connect() as c:
            return c.execute(stmt).scalar()

    # ------------------------------------------------------------------ locks
    def acquire_lock(self, name: str, owner: str, ttl: timedelta) -> bool:
        """Acquire a named lock. Expired locks (crashed runs) are taken over atomically."""
        now = utcnow()
        t = s.pipeline_lock
        with self.begin() as conn:
            res = conn.execute(
                update(t)
                .where(and_(t.c.lock_name == name, t.c.expires_at < now))
                .values(owner_run_id=owner, acquired_at=now, expires_at=now + ttl)
            )
            if res.rowcount == 1:
                log.warning("took over expired lock %s", name)
                return True
        try:
            with self.begin() as conn:
                conn.execute(insert(t).values(lock_name=name, owner_run_id=owner, acquired_at=now,
                                              expires_at=now + ttl))
            return True
        except IntegrityError:
            return False

    def release_lock(self, name: str, owner: str) -> None:
        t = s.pipeline_lock
        with self.begin() as conn:
            conn.execute(delete(t).where(and_(t.c.lock_name == name, t.c.owner_run_id == owner)))

    def lock_holder(self, name: str) -> dict[str, Any] | None:
        t = s.pipeline_lock
        with self.connect() as conn:
            row = conn.execute(select(t).where(t.c.lock_name == name)).mappings().first()
        return dict(row) if row else None

    # ------------------------------------------------------------------ key/value state
    def get_state(self, key: str, default: str | None = None) -> str | None:
        t = s.discovery_state
        v = self.scalar(select(t.c.value).where(t.c.key == key))
        return default if v is None else v

    def set_state(self, key: str, value: str, conn: Connection | None = None) -> None:
        row = {"key": key, "value": value, "updated_at": utcnow()}
        if conn is not None:
            self.upsert(conn, s.discovery_state, [row], ["key"])
        else:
            with self.begin() as c:
                self.upsert(c, s.discovery_state, [row], ["key"])


def normalise_datetimes(df: pd.DataFrame) -> pd.DataFrame:
    """Make every datetime-like column tz-aware UTC (SQLite returns naive values)."""
    for col in df.columns:
        series = df[col]
        if pd.api.types.is_datetime64_any_dtype(series):
            df[col] = series.dt.tz_localize("UTC") if series.dt.tz is None else series.dt.tz_convert("UTC")
        elif series.dtype == object and len(series) and col.endswith("_at"):
            sample = series.dropna()
            if len(sample) and isinstance(sample.iloc[0], (datetime, str)):
                with contextlib.suppress(ValueError, TypeError):
                    df[col] = pd.to_datetime(series, utc=True, format="mixed")
    return df
