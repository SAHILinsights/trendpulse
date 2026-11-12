from datetime import timedelta

import pandas as pd

from tests.helpers import NOW
from trendpulse.gold.builder import build_topic_facts, build_video_facts
from trendpulse.topics.classifier import UNCLASSIFIED_ID


def _valid():
    rows = [
        # video, region, method, query, views, likes, comments, channel
        ("a", "US", "regional_chart", "chart:all", 1000, 90, 10, "c1"),
        ("a", "US", "regional_chart", "chart:10", 1000, 90, 10, "c1"),   # same video, 2nd chart
        ("a", "IN", "regional_chart", "chart:all", 1000, 90, 10, "c1"),
        ("b", "US", "search", "ai", 500, None, 5, "c2"),
        ("c", "IN", "regional_chart", "chart:all", 200, 2, 0, "c3"),
    ]
    df = pd.DataFrame(rows, columns=["video_id", "region_code", "discovery_method", "discovery_query", "view_count",
                                     "like_count", "comment_count", "channel_id"])
    df["run_id"] = "r2"
    df["chart_category_id"] = None
    df["chart_rank"] = [1, 1, 1, None, 2]
    for c in ("view_count", "like_count", "comment_count"):
        df[c] = df[c].astype("Int64")
    return df


def test_video_facts_measure_growth_since_previous_observation():
    prev = pd.DataFrame({"video_id": ["a", "b"], "latest_view_count": [400, 600],
                         "last_observed_at": [NOW - timedelta(hours=2), NOW - timedelta(hours=1)]})
    f = build_video_facts(_valid(), prev, NOW).set_index(["video_id", "region_code", "discovery_query"])
    a = f.loc[("a", "US", "chart:all")]
    assert a.views_delta == 600 and a.hours_since_prev == 2 and a.views_per_hour == 300
    assert a.engagement_rate == 0.1
    b = f.loc[("b", "US", "ai")]
    assert b.views_delta == -100 and pd.isna(b.views_per_hour)    # count correction: delta kept, no fake rate
    assert b.engagement_rate == 0.01                              # hidden likes treated as 0
    c = f.loc[("c", "IN", "chart:all")]
    assert pd.isna(c.views_delta) and pd.isna(c.views_per_hour)   # first observation: no growth claimed


def test_topic_facts_dedupe_videos_and_rank_excluding_unclassified(settings):
    v = _valid()
    prev = pd.DataFrame({"video_id": ["a"], "latest_view_count": [400], "last_observed_at": [NOW - timedelta(hours=2)]})
    fvo = build_video_facts(v, prev, NOW)
    assign = pd.DataFrame({"video_id": ["a", "b", "a", "c"], "topic_id": ["t1", "t1", "t2", UNCLASSIFIED_ID],
                           "review_status": ["auto", "auto", "auto", "unclassified"]})
    fto = build_topic_facts(fvo, v, assign, settings, "r2", NOW).set_index(["topic_id", "region_code"])
    g = fto.loc[("t1", "GLOBAL")]
    assert g.video_count == 2 and g.channel_count == 2 and g.total_views == 1500   # 'a' counted once
    assert g.views_per_hour == 300
    us = fto.loc[("t1", "US")]
    assert us.video_count == 2 and us.total_views == 1500
    assert fto.loc[("t1", "IN")].video_count == 1
    assert fto.loc[(UNCLASSIFIED_ID, "GLOBAL")].popularity_rank == 0
    ranks = fto.xs("GLOBAL", level="region_code").query("popularity_rank > 0").popularity_rank
    assert sorted(ranks) == [1, 2]
    assert fto.loc[("t1", "GLOBAL")].popularity_rank == 1
