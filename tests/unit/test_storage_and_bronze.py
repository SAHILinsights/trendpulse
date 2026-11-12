import gzip
import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import func, select

from tests.helpers import NOW, chart_record, item
from trendpulse.ingestion.bronze import BronzeRecord, BronzeWriter, load_bronze
from trendpulse.storage import schema as s
from trendpulse.storage.db import Warehouse, as_utc, normalise_url, records_from_df
from trendpulse.storage.migrations import current_version, migrate
from trendpulse.storage.retention import apply_retention


def test_migrations_are_idempotent(wh):
    assert migrate(wh) == []
    assert current_version(wh) == 1


def test_url_normalisation():
    assert normalise_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_url("sqlite:///x.db") == "sqlite:///x.db"


def test_upsert_insert_update_and_do_nothing(wh):
    row = {"channel_id": "c1", "channel_title": "A", "first_observed_at": NOW, "last_observed_at": NOW}
    with wh.begin() as c:
        wh.upsert(c, s.dim_channels, [row], ["channel_id"], ["channel_title", "last_observed_at"])
        later = NOW + timedelta(hours=1)
        wh.upsert(c, s.dim_channels, [{**row, "channel_title": "B", "first_observed_at": later,
                                        "last_observed_at": later}], ["channel_id"],
                  ["channel_title", "last_observed_at"])
        wh.upsert(c, s.dim_channels, [{**row, "channel_title": "IGNORED"}], ["channel_id"], [])
    df = wh.read_df(select(s.dim_channels))
    assert len(df) == 1 and df.channel_title[0] == "B"
    assert df.first_observed_at[0] == pd.Timestamp(NOW)          # insert-only column preserved
    assert str(df.last_observed_at.dt.tz) == "UTC"


def test_records_from_df_cleans_nan_and_numpy():
    df = pd.DataFrame({"a": [1, None], "b": [float("nan"), 2.5], "t": [pd.Timestamp(NOW), pd.NaT]})
    out = records_from_df(df)
    assert out[0]["b"] is None and out[1]["t"] is None and isinstance(out[1]["b"], float)


def test_lock_is_exclusive_and_expired_locks_are_taken_over(wh):
    assert wh.acquire_lock("p", "run-a", timedelta(minutes=5))
    assert not wh.acquire_lock("p", "run-b", timedelta(minutes=5))
    wh.release_lock("p", "run-b")                        # not the owner: no effect
    assert wh.lock_holder("p")["owner_run_id"] == "run-a"
    wh.release_lock("p", "run-a")
    assert wh.acquire_lock("p", "run-c", timedelta(seconds=-1))   # immediately expired
    assert wh.acquire_lock("p", "run-d", timedelta(minutes=5))
    assert wh.lock_holder("p")["owner_run_id"] == "run-d"


def test_read_only_handle_refuses_writes(tmp_path):
    w = Warehouse(f"sqlite:///{tmp_path / 'ro.db'}")
    migrate(w)
    ro = Warehouse(f"sqlite:///{tmp_path / 'ro.db'}", read_only=True)
    with pytest.raises(PermissionError), ro.begin() as c:
        ro.upsert(c, s.discovery_state, [{"key": "k", "value": "v", "updated_at": NOW}], ["key"])


def test_state_roundtrip(wh):
    assert wh.get_state("x", "d") == "d"
    wh.set_state("x", "1")
    wh.set_state("x", "2")
    assert wh.get_state("x") == "2"


def test_bronze_roundtrip_and_lake(wh, tmp_path):
    w = BronzeWriter(wh, tmp_path / "lake")
    rec = chart_record([item("a"), item("b")], run_id="run-z")
    w.write(rec)
    loaded = load_bronze(wh, "run-z")
    assert len(loaded) == 1 and loaded[0].payload == rec.payload and loaded[0].item_count == 2
    assert loaded[0].fetched_at == NOW
    files = list((tmp_path / "lake" / "bronze").rglob("*.json.gz"))
    assert len(files) == 1
    with gzip.open(files[0], "rt") as fh:
        assert json.load(fh)["payload"]["items"][0]["id"] == "a"
    row = rec.to_row()
    assert len(row["payload_sha256"]) == 64 and isinstance(row["payload_gz"], bytes)


def _fto_row(run, at, topic="t1", region="US"):
    return {"run_id": run, "observed_at": at, "topic_id": topic, "region_code": region, "video_count": 1,
            "channel_count": 1, "popularity_score": 1.0, "popularity_rank": 1}


def test_retention_deletes_old_operational_data_and_compacts_whole_runs(wh, settings):
    now = datetime.now(UTC)
    old = now - timedelta(days=10)
    rows = []
    for i in range(4):  # four runs inside the same old hour -> only the last survives (all its rows)
        at = old.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=10 * i)
        rows += [_fto_row(f"r{i}", at, "t1"), _fto_row(f"r{i}", at, "t2")]
    rows += [_fto_row("fresh", now - timedelta(hours=1))]
    with wh.begin() as c:
        wh.insert_many(c, s.fact_topic_observations, rows)
        wh.insert_many(c, s.bronze_api_responses, [{**chart_record([item()], fetched=old).to_row(),
                                                    "response_id": "old-1"}])
        wh.insert_many(c, s.api_request_log, [{"endpoint": "videos.list", "quota_cost": 1,
                                               "requested_at": now - timedelta(days=60)}])
    out = apply_retention(wh, settings.pipeline.retention)
    left = wh.read_df(select(s.fact_topic_observations.c.run_id, s.fact_topic_observations.c.topic_id))
    assert set(left.run_id) == {"r3", "fresh"} and len(left[left.run_id == "r3"]) == 2
    assert out["fact_topic_observations_compacted"] == 6
    assert wh.scalar(select(func.count()).select_from(s.bronze_api_responses)) == 0
    assert wh.scalar(select(func.count()).select_from(s.api_request_log)) == 0


def test_as_utc():
    assert as_utc("2026-01-01T00:00:00Z") == datetime(2026, 1, 1, tzinfo=UTC)
    assert as_utc(datetime(2026, 1, 1)).tzinfo == UTC
    assert as_utc(None) is None


def test_bronze_record_from_row_preserves_lineage(wh):
    rec = BronzeRecord("r", "search.list", "search", "ai", "US", None, 0, {"q": "ai"}, 200, {"items": []}, NOW)
    back = BronzeRecord.from_row(rec.to_row())
    assert (back.discovery_method, back.discovery_query, back.request_params) == ("search", "ai", {"q": "ai"})


def test_retention_deletes_unrefreshed_api_metadata(wh, settings):
    now = datetime.now(UTC)
    vids = [{"video_id": v, "title": "t", "channel_id": "c", "first_observed_at": at, "last_observed_at": at}
            for v, at in (("old", now - timedelta(days=31)), ("new", now - timedelta(hours=1)))]
    with wh.begin() as c:
        wh.insert_many(c, s.dim_videos, vids)
        wh.insert_many(c, s.bridge_video_topics, [{"video_id": v, "topic_id": "t", "confidence": 1.0,
                                                   "assignment_method": "taxonomy", "review_status": "auto",
                                                   "assigned_run_id": "r", "updated_at": now} for v in ("old", "new")])
    apply_retention(wh, settings.pipeline.retention, now)
    assert set(wh.read_df(select(s.dim_videos.c.video_id)).video_id) == {"new"}
    assert set(wh.read_df(select(s.bridge_video_topics.c.video_id)).video_id) == {"new"}
