"""Taxonomy classifier: weighted keyword evidence -> topic + confidence.

Scoring per video and taxonomy topic::

    score = sum(best field weight of each matched keyword)      # title 3, tags 2, description 1, channel 1
          + category_prior if the YouTube categoryId is listed  # 1.0
    confidence = min(1, score / confidence_saturation)

* ``confidence >= min_confidence``  -> assigned (``review_status = auto``)
* ``review_confidence <= c < min``  -> kept as ``needs_review`` (not counted in metrics)
* otherwise                         -> no assignment

Keywords are matched on word boundaries against normalised text using one
compiled alternation per field (longest keywords first), so classifying a
few thousand videos takes well under a second.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trendpulse.settings import TaxonomyConfig, TopicsCfg
from trendpulse.topics.normalize import normalize_text

FIELD_WEIGHTS = {"title": 3.0, "tags": 2.0, "description": 1.0, "channel": 1.0}
CATEGORY_PRIOR = 1.0
UNCLASSIFIED_ID = "tx.unclassified"
UNCLASSIFIED_CATEGORY = "Unclassified"


def taxonomy_topic_id(category_id: str, topic_id: str) -> str:
    return f"tx.{category_id}.{topic_id}"


@dataclass(frozen=True)
class TaxTopic:
    topic_id: str
    name: str
    category: str
    keywords: tuple[str, ...]
    youtube_categories: frozenset[int]


@dataclass
class Classification:
    topic_id: str
    confidence: float
    review_status: str
    matched_terms: list[str]


class TaxonomyClassifier:
    def __init__(self, taxonomy: TaxonomyConfig, cfg: TopicsCfg):
        self.cfg = cfg
        self.topics: dict[str, TaxTopic] = {}
        self.keyword_topics: dict[str, set[str]] = {}
        for cat in taxonomy.categories:
            for t in cat.topics:
                if not t.active:
                    continue
                tid = taxonomy_topic_id(cat.id, t.id)
                kws = tuple(sorted({normalize_text(k) for k in t.keywords if normalize_text(k)}))
                self.topics[tid] = TaxTopic(tid, t.name, cat.name, kws, frozenset(t.youtube_categories))
                for k in kws:
                    self.keyword_topics.setdefault(k, set()).add(tid)
        alternation = "|".join(re.escape(k) for k in sorted(self.keyword_topics, key=len, reverse=True))
        self._pattern = re.compile(rf"(?<!\w)(?:{alternation})(?!\w)") if alternation else None
        self.all_keywords = frozenset(self.keyword_topics)

    def _matches(self, text: str) -> set[str]:
        if not text or self._pattern is None:
            return set()
        return set(self._pattern.findall(text))

    def classify(self, title: str, tags: list[str], description: str, channel: str,
                 category_id: int | None) -> list[Classification]:
        fields = {
            "title": normalize_text(title),
            "tags": " | ".join(normalize_text(t) for t in tags),
            "description": normalize_text(description),
            "channel": normalize_text(channel),
        }
        best_weight: dict[str, float] = {}
        for fname, text in fields.items():
            w = FIELD_WEIGHTS[fname]
            for kw in self._matches(text):
                if w > best_weight.get(kw, 0):
                    best_weight[kw] = w
        scores: dict[str, float] = {}
        terms: dict[str, list[str]] = {}
        for kw, w in best_weight.items():
            for tid in self.keyword_topics[kw]:
                scores[tid] = scores.get(tid, 0.0) + w
                terms.setdefault(tid, []).append(kw)
        if category_id is not None:
            for tid, topic in self.topics.items():
                if category_id in topic.youtube_categories and tid in scores:
                    scores[tid] += CATEGORY_PRIOR
        out = []
        for tid, sc in scores.items():
            conf = round(min(1.0, sc / self.cfg.confidence_saturation), 4)
            if conf >= self.cfg.min_confidence:
                status = "auto"
            elif conf >= self.cfg.review_confidence:
                status = "needs_review"
            else:
                continue
            out.append(Classification(tid, conf, status, sorted(terms.get(tid, []))))
        out.sort(key=lambda c: (-c.confidence, c.topic_id))
        autos = [c for c in out if c.review_status == "auto"][: self.cfg.max_topics_per_video]
        reviews = [c for c in out if c.review_status == "needs_review"][:2]
        return autos + reviews
