"""Stable topic identities.

* Taxonomy topics: ``tx.<category>.<topic>`` straight from config.
* Discovered topics: ``dx_<sha1(canonical_key)[:12]>``. The canonical key is
  order- and plural-insensitive ("gta 6 trailer" == "trailer gta 6"), so the
  same phrase gets the same ID on every run — even after a full rebuild.
* Near-duplicates map onto the existing topic instead of minting a new one:
  1. exact alias key (``topic_aliases``),
  2. token-set containment with an existing key (>= 2 shared tokens),
  3. character n-gram TF-IDF cosine >= ``similarity_threshold``.
  Every such mapping is recorded as an alias, so it stays stable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


def discovered_topic_id(key: str) -> str:
    return "dx_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]  # noqa: S324 - identity, not security


@dataclass
class KnownTopic:
    topic_id: str
    key: str
    name: str


@dataclass
class Resolution:
    topic_id: str
    is_new: bool
    matched_by: str          # alias | key | containment | similarity | new
    name: str


@dataclass
class TopicRegistry:
    topics: dict[str, KnownTopic] = field(default_factory=dict)       # topic_id -> topic
    aliases: dict[str, str] = field(default_factory=dict)             # alias key -> topic_id
    similarity_threshold: float = 0.88
    new_aliases: dict[str, tuple[str, str]] = field(default_factory=dict)  # key -> (topic_id, surface)

    def __post_init__(self) -> None:
        self._by_key = {t.key: t.topic_id for t in self.topics.values()}
        self._vec = None
        self._matrix = None
        self._ids: list[str] = []

    def _build_index(self) -> None:
        if self._vec is not None or not self.topics:
            return
        self._ids = list(self.topics)
        names = [self.topics[i].name for i in self._ids]
        self._vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 4))
        self._matrix = self._vec.fit_transform(names)

    def resolve(self, key: str, phrase: str) -> Resolution:
        if key in self.aliases:
            tid = self.aliases[key]
            return Resolution(tid, False, "alias", self.topics[tid].name if tid in self.topics else phrase)
        if key in self._by_key:
            tid = self._by_key[key]
            return Resolution(tid, False, "key", self.topics[tid].name)
        toks = set(key.split())
        if len(toks) >= 2:
            for t in self.topics.values():
                other = set(t.key.split())
                if len(other) >= 2 and (toks <= other or other <= toks) and len(toks & other) >= 2:
                    self.record_alias(key, t.topic_id, phrase)
                    return Resolution(t.topic_id, False, "containment", t.name)
        self._build_index()
        if self._vec is not None:
            sims = cosine_similarity(self._vec.transform([phrase]), self._matrix).ravel()
            j = int(sims.argmax())
            if sims[j] >= self.similarity_threshold:
                tid = self._ids[j]
                self.record_alias(key, tid, phrase)
                return Resolution(tid, False, "similarity", self.topics[tid].name)
        tid = discovered_topic_id(key)
        self.topics[tid] = KnownTopic(tid, key, phrase)
        self._by_key[key] = tid
        self._vec = None  # rebuild lazily with the new topic
        return Resolution(tid, True, "new", phrase)

    def record_alias(self, key: str, topic_id: str, surface: str) -> None:
        self.aliases[key] = topic_id
        self.new_aliases[key] = (topic_id, surface)
