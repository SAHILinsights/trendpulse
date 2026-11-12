"""Emerging-topic discovery: salient keyphrases shared across videos and channels.

Algorithm (per run, over the run's unique videos)
-------------------------------------------------
1. Document = normalised title + tags as separate segments (n-grams never
   cross a segment boundary; descriptions are excluded as too noisy).
2. ``CountVectorizer`` (binary, 1–3-grams, multilingual-ish stop words)
   keeps n-grams in >= ``min_videos`` docs and <= ``max_doc_fraction`` of
   docs (drops boilerplate like "official").
3. Each candidate must come from >= ``min_channels`` distinct channels —
   one channel uploading a series is not a trend.
4. Salience = summed TF-IDF weight x ln(1 + channels).
5. Similar-topic grouping: candidates whose token sets nest and whose video
   sets overlap >= 80% are merged; the phrase covering the most videos
   wins (ties -> the more specific, longer phrase).
6. Entity filter: a phrase needs at least one *distinctive* token — not a
   digit, not a generic word, not already a taxonomy keyword token — so
   "street food recipe" or "final match" never become topics, while
   "nova x5" or "skyforge legends" do. Single words need one extra channel.

The output is a list of :class:`Candidate` phrases with their video sets.
Stable identities are assigned later by :mod:`trendpulse.topics.registry`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import CountVectorizer, TfidfTransformer

from trendpulse.settings import DiscoveryCfg
from trendpulse.topics.normalize import (
    GENERIC_WORDS,
    canonical_key,
    is_meaningful_phrase,
    normalize_text,
    tokenize,
)

SEGMENT_SEP = " \u241e "  # record separator: n-grams never span title/tag boundaries


@dataclass
class Candidate:
    phrase: str
    key: str
    video_ids: set[str]
    channels: int
    salience: float
    aliases: set[str] = field(default_factory=set)


def build_documents(videos: pd.DataFrame) -> pd.Series:
    """One document per video: title and each tag are separate segments."""
    def _doc(row) -> str:
        tags = row["tags"] if isinstance(row["tags"], list) else []
        return SEGMENT_SEP.join([normalize_text(row["title"])] + [normalize_text(t) for t in tags[:15]])
    return videos.apply(_doc, axis=1)


def make_analyzer(ngram_min: int, ngram_max: int):
    def analyze(doc: str) -> list[str]:
        grams: list[str] = []
        for seg in doc.split(SEGMENT_SEP):
            toks = tokenize(seg)
            for n in range(ngram_min, ngram_max + 1):
                for i in range(len(toks) - n + 1):
                    grams.append(" ".join(toks[i:i + n]))
        return grams
    return analyze


def is_entity_like(phrase: str, taxonomy_tokens: frozenset[str]) -> bool:
    """A discovered topic must name *something*: at least one token that is
    not a digit, not generic and not already a taxonomy keyword token."""
    toks = phrase.split()
    if toks[0].isdigit() or toks[-1].isdigit():
        return False
    distinctive = [t for t in toks if not t.isdigit() and t not in GENERIC_WORDS and t not in taxonomy_tokens]
    if not distinctive:
        return False
    if len(toks) == 1:
        return len(toks[0]) >= 4
    return True


def _nested(a: str, b: str) -> bool:
    ta, tb = set(a.split()), set(b.split())
    return ta <= tb or tb <= ta


def discover_candidates(videos: pd.DataFrame, cfg: DiscoveryCfg, generic_terms: frozenset[str]) -> list[Candidate]:
    """``generic_terms``: taxonomy keywords (already covered by curated topics)."""
    if not cfg.enabled or len(videos) < max(cfg.min_videos, 5):
        return []
    videos = videos.reset_index(drop=True)
    docs = build_documents(videos)
    n_docs = len(docs)
    taxonomy_tokens = frozenset(tok for term in generic_terms for tok in term.split())
    vec = CountVectorizer(analyzer=make_analyzer(cfg.ngram_min, cfg.ngram_max), binary=True,
                          min_df=cfg.min_videos, max_df=max(cfg.max_doc_fraction, cfg.min_videos / n_docs))
    try:
        counts = vec.fit_transform(docs)
    except ValueError:  # empty vocabulary
        return []
    terms = vec.get_feature_names_out()
    tfidf = TfidfTransformer(sublinear_tf=True).fit_transform(counts)
    weight = np.asarray(tfidf.sum(axis=0)).ravel()
    csc = counts.tocsc()
    video_ids = videos["video_id"].to_numpy()
    channels = videos["channel_id"].to_numpy()

    raw: list[Candidate] = []
    for j, term in enumerate(terms):
        if not is_meaningful_phrase(term) or term in generic_terms or not is_entity_like(term, taxonomy_tokens):
            continue
        rows = csc.indices[csc.indptr[j]:csc.indptr[j + 1]]
        n_channels = len(set(channels[rows]))
        min_ch = cfg.min_channels + (1 if len(term.split()) == 1 else 0)
        if n_channels < min_ch:
            continue
        raw.append(Candidate(term, canonical_key(term), set(video_ids[rows]), n_channels,
                             float(weight[j]) * math.log1p(n_channels)))

    return group_candidates(raw, taxonomy_tokens)


def _distinctive(phrase: str, taxonomy_tokens: frozenset[str]) -> set[str]:
    return {t for t in phrase.split() if not t.isdigit() and t not in GENERIC_WORDS and t not in taxonomy_tokens}


def group_candidates(raw: list[Candidate], taxonomy_tokens: frozenset[str] = frozenset()) -> list[Candidate]:
    """Merge near-duplicate phrases into one topic (similar-topic grouping).

    A candidate joins an accepted topic when either
    * the phrases nest ("nova x5" / "nova x5 smartphone") and their video
      sets overlap >= 80% (overlap coefficient), or
    * they share a distinctive token and >= 80% of the candidate's videos
      already belong to the topic ("legends gameplay" -> "skyforge legends").
    Processing order is most videos first, so the broadest phrase becomes
    the canonical name; on equal coverage the phrase with more distinctive
    tokens wins ("skyforge legends" beats "skyforge" and "skyforge legends gameplay").
    """
    raw = sorted(raw, key=lambda c: (-len(c.video_ids), -len(_distinctive(c.phrase, taxonomy_tokens)),
                                     len(c.phrase.split()), -c.salience))
    accepted: list[Candidate] = []
    for cand in raw:
        merged = False
        cand_dist = _distinctive(cand.phrase, taxonomy_tokens)
        for acc in accepted:
            inter = len(cand.video_ids & acc.video_ids)
            overlap = inter / max(1, min(len(cand.video_ids), len(acc.video_ids)))
            nested = cand.key == acc.key or _nested(cand.phrase, acc.phrase)
            shares = bool(cand_dist & _distinctive(acc.phrase, taxonomy_tokens))
            if (nested and overlap >= 0.8) or (shares and inter / max(1, len(cand.video_ids)) >= 0.8):
                if (len(cand.video_ids) == len(acc.video_ids) and nested
                        and len(cand_dist) > len(_distinctive(acc.phrase, taxonomy_tokens))):
                    acc.aliases.add(acc.phrase)
                    acc.phrase, acc.key = cand.phrase, cand.key
                else:
                    acc.aliases.add(cand.phrase)
                acc.video_ids |= cand.video_ids
                acc.salience = max(acc.salience, cand.salience)
                merged = True
                break
        if not merged:
            accepted.append(cand)
    accepted.sort(key=lambda c: -c.salience)
    return accepted
