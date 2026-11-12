"""Text normalisation shared by classification and keyphrase discovery."""

from __future__ import annotations

import re
import unicodedata

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_MENTION_RE = re.compile(r"@\w+")
# keep letters/digits of any script plus a few meaningful joiners
_NON_WORD_RE = re.compile(r"[^\w\s+&'./-]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
TOKEN_RE = re.compile(r"(?u)\b[\w][\w+'.-]*[\w+]|\b\w\b")

# YouTube boilerplate that carries no topical meaning.
YOUTUBE_NOISE = {
    "video", "videos", "official", "new", "latest", "full", "hd", "4k", "1080p", "shorts", "short", "ytshorts",
    "youtube", "subscribe", "channel", "watch", "live", "today", "now", "best", "top", "vs", "ft", "feat",
    "part", "ep", "episode", "2024", "2025", "2026", "2027", "day", "week", "time", "like", "comment", "share",
    "free", "update", "trending", "viral", "reaction", "react", "reacts", "highlight", "highlights", "sample",
    "data", "synthetic", "music", "song", "audio", "mv", "lyrics", "lyric", "trailer", "teaser", "review",
    "ki", "ke", "ka", "hai", "se", "ko", "aur", "na", "mein", "kya",  # common Hindi (romanised) function words
    "der", "die", "das", "und", "ist", "mit", "für", "von", "ein", "eine", "nicht",  # German
    "le", "la", "les", "des", "et", "en", "du", "un", "une", "est", "pour", "avec",  # French
    "o", "os", "as", "e", "de", "do", "da", "em", "um", "uma", "com", "para", "que", "não",  # Portuguese
    "el", "los", "y", "por",  # Spanish
}

STOPWORDS: frozenset[str] = frozenset(set(ENGLISH_STOP_WORDS) | YOUTUBE_NOISE)

# Common descriptive words that are never a topic *on their own* and do not
# make a phrase an entity ("easy home recipe", "final match", "season").
GENERIC_WORDS: frozenset[str] = frozenset({
    "season", "final", "finals", "match", "test", "launch", "compilation", "minutes", "minute", "hours", "easy",
    "home", "funny", "plays", "play", "life", "game", "games", "news", "people", "world", "story", "first", "last",
    "real", "big", "little", "good", "great", "vs", "special", "edition", "version", "series", "show", "movie",
    "film", "songs", "challenge", "guide", "tips", "tricks", "ways", "things", "reasons", "secret", "truth",
    "inside", "behind", "making", "behind-the-scenes", "explained", "event", "events", "moments", "clip", "clips",
    "scene", "scenes", "opening", "ending", "recap", "update", "updates", "release", "date", "reveal", "build",
    "builds", "app", "beginners", "beginner", "tutorial", "mission", "post", "goals", "comparison",
    "camera", "rally", "moved", "crash", "worth", "catches", "catch", "over", "hearts", "lights", "itself",
})


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).lower()
    t = _URL_RE.sub(" ", t)
    t = _MENTION_RE.sub(" ", t)
    t = t.replace("#", " ").replace("|", " ").replace("_", " ")
    t = _NON_WORD_RE.sub(" ", t)
    return _SPACE_RE.sub(" ", t).strip()


def tokenize(text: str, drop_stopwords: bool = True) -> list[str]:
    toks = [tok.strip(".'-") for tok in TOKEN_RE.findall(text)]
    toks = [t for t in toks if t]
    if drop_stopwords:
        toks = [t for t in toks if t not in STOPWORDS]
    return toks


def singular(token: str) -> str:
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def canonical_key(phrase: str) -> str:
    """Order-insensitive, plural-insensitive identity key for a phrase."""
    toks = {singular(t) for t in tokenize(normalize_text(phrase), drop_stopwords=False)}
    return " ".join(sorted(t for t in toks if t not in STOPWORDS))


def is_meaningful_phrase(phrase: str) -> bool:
    toks = phrase.split()
    if not toks:
        return False
    if all(t.isdigit() for t in toks):
        return False
    if all(t in STOPWORDS for t in toks):
        return False
    return not (len(toks) == 1 and len(toks[0]) < 3)
