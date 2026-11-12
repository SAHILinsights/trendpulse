"""Shared fixtures. The suite needs no API key and no cloud credentials.

Set ``TRENDPULSE_TEST_DATABASE_URL`` to a disposable PostgreSQL database to run
the storage/pipeline tests against Postgres instead of SQLite (CI does this).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from trendpulse.ingestion.sample import SampleYouTubeSession
from trendpulse.orchestration.pipeline import Pipeline
from trendpulse.settings import load_settings
from trendpulse.storage.db import Warehouse
from trendpulse.storage.migrations import migrate

REPO = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("TRENDPULSE__") or k in {"YOUTUBE_API_KEY", "DATABASE_URL", "DASHBOARD_DATABASE_URL",
                                                 "TRENDPULSE_ALERT_WEBHOOK_URL", "GITHUB_STEP_SUMMARY"}:
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def settings(tmp_path):
    s = load_settings(config_dir=REPO / "config", data_dir=tmp_path, load_dotenv_file=False)
    s.pipeline.ingestion.min_seconds_between_requests = 0
    s.pipeline.ingestion.backoff_base_seconds = 0
    return s


@pytest.fixture
def wh(tmp_path):
    url = os.environ.get("TRENDPULSE_TEST_DATABASE_URL")
    if url:
        w = Warehouse(url)
        with w.begin() as c:
            c.execute(text("DROP SCHEMA IF EXISTS trendpulse CASCADE"))
    else:
        w = Warehouse(f"sqlite:///{tmp_path / 'test.db'}")
    migrate(w)
    yield w
    w.dispose()


class Clock:
    def __init__(self, t: datetime = T0):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, minutes: int = 30) -> datetime:
        self.t = self.t + timedelta(minutes=minutes)
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_pipeline(settings, wh, clock):
    def _make(session=None, **kw):
        sess = session or SampleYouTubeSession(clock, epoch=T0 + timedelta(hours=24))
        return Pipeline(settings, wh, session_factory=lambda st: sess, clock=clock, sleep=lambda s: None,
                        notify=False, **kw)
    return _make
