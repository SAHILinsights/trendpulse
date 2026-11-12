"""Dashboard runtime: read-only connection, caching, secrets, data-mode detection."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

import streamlit as st

from trendpulse.dashboard import data as d
from trendpulse.settings import get_settings
from trendpulse.storage.db import Warehouse, utcnow

CACHE_TTL = 120


def secret(name: str) -> str | None:
    """Streamlit secrets first (Community Cloud), then environment."""
    try:
        if name in st.secrets:
            v = str(st.secrets[name]).strip()
            return v or None
    except Exception:  # noqa: BLE001, S110 - no secrets.toml locally
        pass
    v = os.environ.get(name, "").strip()
    return v or None


@st.cache_resource(show_spinner=False)
def get_wh() -> Warehouse | None:
    settings = get_settings()
    url = secret("DASHBOARD_DATABASE_URL") or secret("DATABASE_URL")
    if not url:
        db = settings.data_dir / "trendpulse.db"
        if not db.exists():
            return None
        return Warehouse(None, data_dir=settings.data_dir, read_only=True)
    return Warehouse(url, read_only=True)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def q(name: str, *args, **kwargs):
    """Cached call of ``trendpulse.dashboard.data.<name>(wh, *args)``."""
    wh = get_wh()
    fn = getattr(d, name)
    if name in {"platform_health", "quota_usage"}:
        return fn(wh, get_settings(), *args, **kwargs)
    return fn(wh, *args, **kwargs)


@dataclass
class DataMode:
    key: str          # live | historical | sample | empty | error
    label: str
    detail: str


def data_mode(health: dict | None, error: str | None = None) -> DataMode:
    if error:
        return DataMode("error", "Warehouse unavailable", error)
    if not health or health.get("data_source") is None:
        return DataMode("empty", "No data yet", "The pipeline has not published a snapshot.")
    if health["data_source"] == "sample_fixture":
        return DataMode("sample", "SAMPLE DATA",
                        "Synthetic data from the offline API simulator — not real YouTube observations.")
    if health["freshness"] in {"fresh", "delayed"}:
        return DataMode("live", "Live API-derived",
                        "Observations collected from the YouTube Data API v3 by the scheduled pipeline.")
    return DataMode("historical", "Historical (stale)",
                    "Real API-derived history; no successful run recently, so this is not current.")


def now() -> datetime:
    return utcnow()
