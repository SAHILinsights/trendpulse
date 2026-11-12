import json
from datetime import timedelta

import pandas as pd
import pytest

from tests.helpers import NOW
from trendpulse.settings import DiscoveryCfg
from trendpulse.topics.classifier import UNCLASSIFIED_ID, TaxonomyClassifier
from trendpulse.topics.discovery import Candidate, discover_candidates, group_candidates, is_entity_like
from trendpulse.topics.engine import run_topic_engine
from trendpulse.topics.normalize import canonical_key, normalize_text, tokenize
from trendpulse.topics.registry import KnownTopic, TopicRegistry, discovered_topic_id


def test_normalisation():
    assert normalize_text("Check THIS out!! https://x.y/z #GTA6 @someone") == "check this out gta6"
    assert tokenize("the official new trailer for skyforge") == ["skyforge"]
    assert canonical_key("GTA 6 Trailers") == canonical_key("trailer gta 6") == "6 gta"


@pytest.fixture
def clf(settings):
    return TaxonomyClassifier(settings.taxonomy, settings.pipeline.topics)


def test_classifier_assigns_relevant_topics_with_confidence(clf):
    res = clf.classify("IPL 2026 final highlights | Virat Kohli century", ["cricket", "ipl"], "", "Star Sports", 17)
    top = res[0]
    assert top.topic_id == "tx.sports.cricket" and top.review_status == "auto" and top.confidence == 1.0
    assert {"ipl", "cricket", "virat kohli"} <= set(top.matched_terms)


def test_classifier_does_not_force_unrelated_topics(clf):
    assert clf.classify("我的日常", [], "", "", 22) == []
    weak = clf.classify("Weekend thoughts", [], "a short vlog", "", None)  # description-only evidence
    assert all(c.review_status == "needs_review" for c in weak)


def test_category_prior_never_creates_topic_alone(clf):
    assert clf.classify("Untitled 1", [], "", "", 10) == []


def test_entity_filter():
    tax = frozenset({"gameplay", "official", "trailer", "cricket"})
    assert is_entity_like("skyforge legends", tax)
    assert not is_entity_like("final match", tax)
    assert not is_entity_like("10 minutes", tax)
    assert not is_entity_like("gameplay trailer", tax)


def _videos(rows):
    return pd.DataFrame([{"video_id": v, "title": t, "tags": tags, "channel_id": c, "category_id": 20}
                         for v, t, tags, c in rows])


def test_discovery_requires_multiple_videos_and_channels():
    rows = [(f"v{i}", f"Skyforge Legends gameplay part {i}", ["skyforge legends"], f"ch{i % 3}") for i in range(4)]
    rows += [(f"s{i}", f"Moonbeam Diaries episode {i}", ["moonbeam diaries"], "solo") for i in range(5)]
    rows += [(f"x{i}", f"random words {i} {i * 7}", [], f"c{i}") for i in range(10)]
    cands = discover_candidates(_videos(rows), DiscoveryCfg(), frozenset({"gameplay"}))
    phrases = {c.phrase for c in cands}
    assert "skyforge legends" in phrases
    assert not any("moonbeam" in p for p in phrases)  # one channel only
    sky = next(c for c in cands if c.phrase == "skyforge legends")
    assert sky.video_ids == {f"v{i}" for i in range(4)} and sky.channels == 3


def test_grouping_merges_nested_and_overlapping_phrases():
    a = Candidate("nova x5", "nova x5", {"1", "2", "3", "4"}, 3, 5.0)
    b = Candidate("nova x5 smartphone", "nova smartphone x5", {"1", "2"}, 2, 3.0)
    c = Candidate("x5 pixel", "pixel x5", {"3"}, 2, 1.0)
    d = Candidate("echo wave", "echo wave", {"9", "8", "7"}, 3, 4.0)
    out = group_candidates([b, c, a, d], frozenset({"smartphone"}))
    names = {x.phrase for x in out}
    assert names == {"nova x5", "echo wave"}
    nova = next(x for x in out if x.phrase == "nova x5")
    assert {"nova x5 smartphone", "x5 pixel"} <= nova.aliases


def test_registry_identity_is_stable_and_deduplicates():
    assert discovered_topic_id("gta 6") == discovered_topic_id("gta 6")
    reg = TopicRegistry({"dx_1": KnownTopic("dx_1", "legend skyforge", "skyforge legends")}, {})
    assert reg.resolve("legend skyforge", "skyforge legends").matched_by == "key"
    r = reg.resolve("legend release skyforge", "skyforge legends release")
    assert (r.topic_id, r.matched_by) == ("dx_1", "containment")
    assert reg.resolve("legend release skyforge", "x").matched_by == "alias"
    reg2 = TopicRegistry({"dx_2": KnownTopic("dx_2", "lumen", "lumen agents")}, {}, similarity_threshold=0.8)
    assert reg2.resolve("agent lumen", "lumen agent").matched_by == "similarity"
    new = reg2.resolve("brand totally", "totally new brand")
    assert new.is_new and new.topic_id == discovered_topic_id("brand totally")


def _silver(rows, run="r1"):
    return pd.DataFrame([{"video_id": v, "title": t, "tags": json.dumps(tags), "description_snippet": "",
                          "channel_title": "", "channel_id": c, "category_id": cat, "run_id": run}
                         for v, t, tags, c, cat in rows])


def test_engine_end_to_end_stable_ids_and_unclassified(settings, wh):
    from trendpulse.gold.builder import _touch_topics
    from trendpulse.storage import schema as s
    from trendpulse.storage.db import records_from_df

    rows = [(f"v{i}", f"Skyforge Legends official gameplay trailer {i}", ["skyforge legends", "gaming"],
             f"ch{i}", 20) for i in range(5)]
    rows += [("u1", "日常の記録 その1", [], "chx", 22)]
    rows += [(f"n{i}", f"Easy street food recipe number {i}", ["recipe"], f"nc{i}", 26) for i in range(14)]
    out1 = run_topic_engine(_silver(rows), settings, wh, "r1", NOW)
    sky_id = discovered_topic_id(canonical_key("skyforge legends"))
    assert sky_id in set(out1.topics.topic_id)
    a = out1.assignments
    assert set(a[a.video_id == "u1"].topic_id) == {UNCLASSIFIED_ID}
    assert "tx.gaming.game_releases" in set(a[a.video_id == "v0"].topic_id)
    assert out1.video_category["v0"] == "Gaming"
    # persist like publish_gold would, then a second run must reuse the identity and keep matching it
    with wh.begin() as c:
        wh.upsert(c, s.dim_topics, records_from_df(out1.topics), ["topic_id"], [])
        _touch_topics(c, list(out1.topics.topic_id), NOW)
    rows2 = [("w1", "Skyforge Legends patch notes", ["skyforge legends"], "chq", 20)]
    out2 = run_topic_engine(_silver(rows2, "r2"), settings, wh, "r2", NOW + timedelta(hours=1))
    assert out2.topics.empty  # nothing new minted
    assert sky_id in set(out2.assignments.topic_id)  # still tracked though below discovery threshold
