"""Topic intelligence engine: classify every observed video into stable topics.

Two complementary layers run on each batch:

1. **Taxonomy topics** (curated, ``tx.*``) via :class:`TaxonomyClassifier`.
2. **Discovered topics** (``dx_*``) — salient multi-channel keyphrases from
   :func:`discover_candidates`, given stable identities by
   :class:`TopicRegistry`. Previously discovered, still-active topics are
   re-matched every run so their history continues even when they fall
   below the discovery threshold (that's how *declining* becomes visible).

Videos without a confident assignment go to ``tx.unclassified`` and any
low-confidence taxonomy match is stored as ``needs_review`` — visible in
the Control Center review queue, excluded from topic metrics.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import select

from trendpulse.logging_utils import get_logger
from trendpulse.settings import Settings
from trendpulse.storage import schema as s
from trendpulse.storage.db import Warehouse, as_utc
from trendpulse.topics.classifier import UNCLASSIFIED_CATEGORY, UNCLASSIFIED_ID, TaxonomyClassifier
from trendpulse.topics.discovery import discover_candidates
from trendpulse.topics.normalize import canonical_key, normalize_text, singular, tokenize
from trendpulse.topics.registry import KnownTopic, TopicRegistry
from trendpulse.youtube_categories import TAXONOMY_CATEGORY_FALLBACK

log = get_logger("topics")

ACTIVE_DISCOVERED_DAYS = 14


@dataclass
class TopicOutput:
    assignments: pd.DataFrame
    topics: pd.DataFrame
    aliases: list[dict]
    video_category: dict[str, str]
    stats: dict = field(default_factory=dict)


def _tags(v) -> list[str]:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        try:
            out = json.loads(v)
            return out if isinstance(out, list) else []
        except ValueError:
            return []
    return []


def taxonomy_dim_rows(settings: Settings) -> list[dict]:
    rows = []
    for cat in settings.taxonomy.categories:
        for t in cat.topics:
            rows.append({"topic_id": f"tx.{cat.id}.{t.id}", "canonical_name": t.name,
                         "canonical_key": f"taxonomy:{cat.id}.{t.id}", "topic_kind": "taxonomy",
                         "category": cat.name, "parent_topic_id": None,
                         "keywords": json.dumps(t.keywords, ensure_ascii=False), "is_active": t.active})
    rows.append({"topic_id": UNCLASSIFIED_ID, "canonical_name": "Unclassified", "canonical_key": "taxonomy:unclassified",
                 "topic_kind": "unclassified", "category": UNCLASSIFIED_CATEGORY, "parent_topic_id": None,
                 "keywords": "[]", "is_active": True})
    return rows


def load_registry(wh: Warehouse, settings: Settings, now: datetime) -> tuple[TopicRegistry, dict[str, dict]]:
    t, a = s.dim_topics, s.topic_aliases
    with wh.connect() as conn:
        rows = conn.execute(select(t.c.topic_id, t.c.canonical_key, t.c.canonical_name, t.c.category,
                                   t.c.last_observed_at, t.c.is_active)
                            .where(t.c.topic_kind == "discovered")).mappings().all()
        aliases = {r.alias_key: r.topic_id for r in conn.execute(select(a.c.alias_key, a.c.topic_id))}
    reg = TopicRegistry({r["topic_id"]: KnownTopic(r["topic_id"], r["canonical_key"], r["canonical_name"])
                         for r in rows}, aliases, settings.pipeline.topics.discovery.similarity_threshold)
    cutoff = now - timedelta(days=ACTIVE_DISCOVERED_DAYS)
    active = {}
    for r in rows:
        last = as_utc(r["last_observed_at"])
        if r["is_active"] and last is not None and last >= cutoff:
            active[r["topic_id"]] = dict(r)
    return reg, active


def run_topic_engine(silver: pd.DataFrame, settings: Settings, wh: Warehouse, run_id: str,
                     now: datetime) -> TopicOutput:
    cfg = settings.pipeline.topics
    videos = (silver.drop_duplicates("video_id")
              [["video_id", "title", "tags", "description_snippet", "channel_title", "channel_id", "category_id"]]
              .reset_index(drop=True).copy())
    videos["tags"] = videos["tags"].map(_tags)
    clf = TaxonomyClassifier(settings.taxonomy, cfg)

    assign: list[dict] = []
    tax_cats: dict[str, Counter] = {}
    for v in videos.itertuples(index=False):
        cat_id = None if pd.isna(v.category_id) else int(v.category_id)
        for c in clf.classify(v.title, v.tags, v.description_snippet or "", v.channel_title or "", cat_id):
            assign.append({"video_id": v.video_id, "topic_id": c.topic_id, "confidence": c.confidence,
                           "assignment_method": "taxonomy", "review_status": c.review_status,
                           "matched_terms": json.dumps(c.matched_terms, ensure_ascii=False)})
            if c.review_status == "auto":
                tax_cats.setdefault(v.video_id, Counter())[clf.topics[c.topic_id].category] += c.confidence

    # ---- discovered topics
    registry, active = load_registry(wh, settings, now)
    new_topic_rows: dict[str, dict] = {}
    run_topics: dict[str, dict] = {}   # topic_id -> {"key", "name"}
    n_new = 0
    for cand in discover_candidates(videos, cfg.discovery, clf.all_keywords):
        res = registry.resolve(cand.key, cand.phrase)
        if res.is_new:
            if n_new >= cfg.discovery.max_new_topics_per_run:
                registry.topics.pop(res.topic_id, None)
                continue
            n_new += 1
            cats = Counter()
            for vid in cand.video_ids:
                cats.update(tax_cats.get(vid, Counter()))
            if cats:
                category = cats.most_common(1)[0][0]
            else:
                yt = Counter(int(c) for c in videos.loc[videos.video_id.isin(cand.video_ids), "category_id"].dropna())
                category = TAXONOMY_CATEGORY_FALLBACK.get(yt.most_common(1)[0][0], UNCLASSIFIED_CATEGORY) \
                    if yt else UNCLASSIFIED_CATEGORY
            new_topic_rows[res.topic_id] = {
                "topic_id": res.topic_id, "canonical_name": cand.phrase, "canonical_key": cand.key,
                "topic_kind": "discovered", "category": category, "parent_topic_id": None,
                "keywords": json.dumps(sorted({cand.phrase, *cand.aliases}), ensure_ascii=False),
                "created_run_id": run_id, "is_active": True,
            }
        run_topics[res.topic_id] = {"key": registry.topics[res.topic_id].key}
        # remember multi-word variants so future runs resolve them to the same identity
        for alias in cand.aliases:
            akey = canonical_key(alias)
            if len(akey.split()) >= 2 and akey not in registry.aliases and akey != registry.topics[res.topic_id].key:
                registry.record_alias(akey, res.topic_id, alias)
    for tid, row in active.items():
        run_topics.setdefault(tid, {"key": row["canonical_key"]})

    # inverted index over singularised title/tag tokens
    title_tokens: dict[str, set[str]] = {}
    index: dict[str, set[str]] = {}
    for v in videos.itertuples(index=False):
        tt = {singular(t) for t in tokenize(normalize_text(v.title))}
        tg = {singular(t) for tag in v.tags for t in tokenize(normalize_text(tag))}
        title_tokens[v.video_id] = tt
        for tok in tt | tg:
            index.setdefault(tok, set()).add(v.video_id)
    matched_topics = 0
    for tid, info in run_topics.items():
        toks = info["key"].split()
        if not toks:
            continue
        vids = set.intersection(*(index.get(t, set()) for t in toks))
        if vids:
            matched_topics += 1
        for vid in vids:
            in_title = set(toks) <= title_tokens.get(vid, set())
            assign.append({"video_id": vid, "topic_id": tid, "confidence": 0.9 if in_title else 0.7,
                           "assignment_method": "discovered", "review_status": "auto",
                           "matched_terms": json.dumps([info["key"]], ensure_ascii=False)})

    adf = pd.DataFrame(assign, columns=["video_id", "topic_id", "confidence", "assignment_method",
                                        "review_status", "matched_terms"])
    adf = adf.sort_values("confidence", ascending=False).drop_duplicates(["video_id", "topic_id"])

    # ---- unclassified fallback (every video gets >= 1 countable topic)
    covered = set(adf.loc[adf.review_status == "auto", "video_id"])
    fallback = [{"video_id": vid, "topic_id": UNCLASSIFIED_ID, "confidence": 0.0, "assignment_method": "fallback",
                 "review_status": "unclassified", "matched_terms": "[]"}
                for vid in videos.video_id if vid not in covered]
    if fallback:
        adf = pd.concat([adf, pd.DataFrame(fallback)], ignore_index=True)

    # ---- primary category per video
    topic_cat = {r["topic_id"]: r["category"] for r in new_topic_rows.values()}
    topic_cat.update({tid: row["category"] for tid, row in active.items()})
    video_category: dict[str, str] = {}
    for v in videos.itertuples(index=False):
        if v.video_id in tax_cats:
            video_category[v.video_id] = tax_cats[v.video_id].most_common(1)[0][0]
            continue
        disc = adf[(adf.video_id == v.video_id) & (adf.assignment_method == "discovered")]
        cats = [topic_cat.get(t) for t in disc.topic_id if topic_cat.get(t)]
        if cats:
            video_category[v.video_id] = Counter(cats).most_common(1)[0][0]
        elif not pd.isna(v.category_id):
            video_category[v.video_id] = TAXONOMY_CATEGORY_FALLBACK.get(int(v.category_id), UNCLASSIFIED_CATEGORY)
        else:
            video_category[v.video_id] = UNCLASSIFIED_CATEGORY

    aliases = [{"alias_key": k, "topic_id": tid, "surface_form": surf, "created_at": now}
               for k, (tid, surf) in registry.new_aliases.items()]
    stats = {
        "videos": len(videos),
        "assignments": int((adf.review_status == "auto").sum()),
        "needs_review": int((adf.review_status == "needs_review").sum()),
        "unclassified_videos": len(fallback),
        "new_discovered_topics": n_new,
        "discovered_topics_matched": matched_topics,
    }
    log.info("topic engine: %s", stats)
    return TopicOutput(adf.reset_index(drop=True), pd.DataFrame(list(new_topic_rows.values())), aliases,
                       video_category, stats)
