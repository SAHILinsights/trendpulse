from datetime import timedelta

import pandas as pd

from tests.helpers import NOW, chart_record, item, search_videos_record
from trendpulse.quality.framework import (
    FAIL,
    PASS,
    WARNING,
    check_schema,
    check_stale,
    check_topic_coverage,
    check_volume,
    gate_passed,
    overall_status,
    validate_records,
)
from trendpulse.transform.silver import bronze_to_silver, parse_count, parse_duration, parse_ts


def test_parsers():
    assert parse_duration("PT1H2M3S") == 3723 and parse_duration("P1DT1S") == 86401
    assert parse_duration("bogus") is None and parse_duration(None) is None
    assert parse_count("42") == (42, True) and parse_count(None) == (None, True)
    assert parse_count("4.2") == (None, False) and parse_count(True) == (None, False)
    assert parse_count("-3") == (-3, True)
    assert parse_ts("2026-03-01T10:00:00Z").tzinfo is not None and parse_ts("nope") is None


def test_chart_records_get_ranks_and_utc():
    rec = chart_record([item("a" * 11), item("b" * 11)], offset=50, category=10)
    df = bronze_to_silver([rec]).df
    assert list(df.chart_rank) == [51, 52]
    assert set(df.discovery_query) == {"chart:10"} and set(df.chart_category_id) == {10}
    assert str(df.published_at.iloc[0].tzinfo) == "UTC"
    assert df.duration_seconds.iloc[0] == 605


def test_search_results_expand_to_every_tracking_context():
    rec = search_videos_record([item("v1"), item("v2")], {"v1": [["US", "ai"], ["IN", "ai"]], "v2": [["GB", "tech"]]},
                               method="search_refresh")
    df = bronze_to_silver([rec]).df
    assert len(df) == 3 and set(df.discovery_method) == {"search"}
    assert set(zip(df.video_id, df.region_code, strict=True)) == {("v1", "US"), ("v1", "IN"), ("v2", "GB")}


def test_parser_is_tolerant_and_records_issues():
    bad = item("x1", views="lots", published="yesterday")
    bad["snippet"].pop("channelId")
    batch = bronze_to_silver([chart_record([bad, item("ok", newField={"a": 1})])])
    row = batch.df.iloc[0]
    assert "invalid_type:view_count" in row._issues and "unparseable:published_at" in row._issues
    assert batch.schema.missing_required["snippet.channelId"] == 1
    assert batch.schema.unexpected_keys == {"newField": 1}


def _df(items, settings):
    return bronze_to_silver([chart_record(items)]).df


def test_record_checks_quarantine_with_reasons(settings):
    items = [item(f"good{i:07d}") for i in range(20)]
    items += [item("", title="no id"), item("neg", views="-5"), item("negl", likes="-1"),
              item("negc", comments="-2"), item("future", published="2099-01-01T00:00:00Z"),
              item("type", views="1e6"), item("cat", category="999")]
    nochan = item("nochan")
    nochan["snippet"]["channelId"] = ""
    items.append(nochan)
    df = bronze_to_silver([chart_record(items, region="IN"), chart_record([item("zz")], region="XX")]).df
    res = validate_records(df, settings, "run-1", NOW)
    by = {r.check_name: r for r in res.results}
    for name in ("null_video_id", "negative_view_count", "negative_like_count", "negative_comment_count",
                 "invalid_timestamps", "invalid_metric_types", "missing_channel_id", "invalid_region_code"):
        assert by[name].failed == 1, name
        assert by[name].status == WARNING, name      # below the 20% critical ratio
    assert by["invalid_category"].warnings == 1 and by["invalid_category"].failed == 0
    assert "cat" in set(res.valid.video_id)          # flagged, not quarantined
    assert len(res.quarantined) == 8 and all(q["reason"] for q in res.quarantined)
    assert len(res.valid) == len(df) - 8
    assert gate_passed(res.results)


def test_duplicates_keep_best_rank_and_quarantine_rest(settings):
    df = bronze_to_silver([chart_record([item("dup"), item("other"), item("dup")])]).df
    res = validate_records(df, settings, "r", NOW)
    kept = res.valid[res.valid.video_id == "dup"]
    assert len(kept) == 1 and kept.chart_rank.iloc[0] == 1
    assert [q["check_name"] for q in res.quarantined] == ["duplicate_observations"]


def test_critical_ratio_fails_gate(settings):
    items = [item(f"n{i}", views="-1") for i in range(5)] + [item(f"g{i}") for i in range(5)]
    res = validate_records(_df(items, settings), settings, "r", NOW)
    assert {r.check_name: r.status for r in res.results}["negative_view_count"] == FAIL
    assert not gate_passed(res.results) and overall_status(res.results) == FAIL


def test_check_can_be_disabled_by_config(settings):
    settings.pipeline.quality.checks["invalid_category"] = {"enabled": False}
    res = validate_records(_df([item("c", category="999")], settings), settings, "r", NOW)
    assert "invalid_category" not in {r.check_name for r in res.results}


def test_schema_drift_statuses(settings):
    ok = bronze_to_silver([chart_record([item("a")])]).schema
    assert check_schema(ok, settings).status == PASS
    drift = bronze_to_silver([chart_record([item("a", extraThing=1)])]).schema
    assert check_schema(drift, settings).status == WARNING
    broken_items = [item(f"b{i}") for i in range(5)]
    for it in broken_items:
        it["statistics"].pop("viewCount")
    assert check_schema(bronze_to_silver([chart_record(broken_items)]).schema, settings).status == FAIL


def test_stale_and_volume_checks(settings):
    df = pd.DataFrame({"observed_at": [NOW], "video_id": ["a"]})
    assert check_stale(df, NOW + timedelta(minutes=10), settings, NOW - timedelta(minutes=30)).status == PASS
    assert check_stale(df, NOW + timedelta(minutes=10), settings, NOW - timedelta(hours=5)).status == WARNING
    assert check_stale(df, NOW + timedelta(hours=5), settings, None).status == FAIL
    assert check_stale(df.iloc[0:0], NOW, settings, None).status == FAIL
    assert check_volume(100, [100, 100], settings).status == PASS          # insufficient history
    assert check_volume(100, [100, 110, 95, 105], settings).status == PASS
    assert check_volume(40, [100, 110, 95, 105], settings).status == WARNING
    assert check_volume(5, [100, 110, 95, 105], settings).status == FAIL


def test_topic_coverage_check():
    valid = pd.DataFrame({"video_id": ["a", "b"]})
    a = pd.DataFrame({"video_id": ["a", "b"], "review_status": ["auto", "unclassified"]})
    assert check_topic_coverage(valid, a).status == PASS
    a2 = pd.DataFrame({"video_id": ["a", "b"], "review_status": ["auto", "needs_review"]})
    r = check_topic_coverage(valid, a2)
    assert r.status == FAIL and r.failed == 1
