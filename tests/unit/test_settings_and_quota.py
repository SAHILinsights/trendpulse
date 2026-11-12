from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from trendpulse.ingestion.quota import QuotaBudgetExceeded, QuotaTracker, quota_day_bounds, units_used_today
from trendpulse.settings import CountriesConfig, QuotaCfg, apply_env_overrides, load_settings
from trendpulse.storage import schema as s


def test_config_loads_and_is_consistent(settings):
    assert settings.region_codes == ["IN", "US", "GB", "CA", "AU", "DE", "FR", "JP", "KR", "BR"]
    assert settings.pipeline.schedule.interval_minutes == 30
    assert set(settings.pipeline.comparison_windows) == {"30m", "1h", "6h", "24h", "7d", "30d"}
    assert len(settings.seeds.seeds) >= 12


def test_env_override_coerces_types():
    raw = {"quota": {"daily_budget": 9000}, "ingestion": {"search_enabled": True}}
    out = apply_env_overrides(raw, {"TRENDPULSE__QUOTA__DAILY_BUDGET": "5000",
                                    "TRENDPULSE__INGESTION__SEARCH_ENABLED": "false",
                                    "UNRELATED": "x"})
    assert out["quota"]["daily_budget"] == 5000
    assert out["ingestion"]["search_enabled"] is False


def test_env_override_applies_via_load(monkeypatch, tmp_path):
    from pathlib import Path
    monkeypatch.setenv("TRENDPULSE__QUOTA__DAILY_BUDGET", "1234")
    st = load_settings(config_dir=Path(__file__).resolve().parents[2] / "config", data_dir=tmp_path,
                       load_dotenv_file=False)
    assert st.pipeline.quota.daily_budget == 1234


def test_invalid_region_rejected():
    with pytest.raises(ValidationError):
        CountriesConfig(countries=[{"code": "INDIA", "name": "x"}])
    with pytest.raises(ValidationError):
        CountriesConfig(countries=[{"code": "IN", "name": "x"}, {"code": "in", "name": "y"}])


def test_quota_config_validation():
    with pytest.raises(ValidationError):
        QuotaCfg(daily_limit=100, daily_budget=200)
    with pytest.raises(ValidationError):
        QuotaCfg(daily_budget=500, refresh_reserve=600)


def test_quota_day_follows_pacific_time_including_dst():
    start, end = quota_day_bounds(datetime(2026, 7, 1, 6, 59, tzinfo=UTC), "America/Los_Angeles")  # PDT
    assert start == datetime(2026, 6, 30, 7, 0, tzinfo=UTC) and end == datetime(2026, 7, 1, 7, 0, tzinfo=UTC)
    start, _ = quota_day_bounds(datetime(2026, 1, 15, 12, 0, tzinfo=UTC), "America/Los_Angeles")  # PST
    assert start == datetime(2026, 1, 15, 8, 0, tzinfo=UTC)


def test_tracker_protects_refresh_reserve_from_search():
    t = QuotaTracker(QuotaCfg(daily_budget=1000, refresh_reserve=300), used_before_run=601)
    assert not t.can_spend("search.list")          # 701 > 1000-300
    assert t.can_spend("videos.list")
    t.record("videos.list", 1)
    t2 = QuotaTracker(QuotaCfg(daily_budget=1000, refresh_reserve=100), used_before_run=1000)
    with pytest.raises(QuotaBudgetExceeded):
        t2.reserve("videos.list")
    assert t2.remaining == 0


def test_units_used_today_sums_only_current_quota_day(wh):
    now = datetime(2026, 3, 2, 20, 0, tzinfo=UTC)  # 12:00 PST
    rows = [
        {"endpoint": "search.list", "quota_cost": 100, "requested_at": datetime(2026, 3, 2, 9, 0, tzinfo=UTC)},
        {"endpoint": "videos.list", "quota_cost": 1, "requested_at": datetime(2026, 3, 2, 19, 0, tzinfo=UTC)},
        {"endpoint": "videos.list", "quota_cost": 7, "requested_at": datetime(2026, 3, 2, 7, 59, tzinfo=UTC)},
    ]
    with wh.begin() as c:
        wh.insert_many(c, s.api_request_log, rows)
    assert units_used_today(wh, now, "America/Los_Angeles") == 101
