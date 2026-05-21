"""Pure-stdlib similarity helpers shared by the two-tier clustering refactor.

Tier 1 — ingest-time lexical clustering of same-day news headlines:
``titles_similar(a, b)`` collapses syndicated echoes published within hours of
the original story.

Tier 2 — resolver-time semantic clustering of cross-day events:
``entity_jaccard(set_a, set_b)`` uses Gemini-extracted ``primary_entities`` to
trace a brief's triggering event back to the root source of the story arc.

Both helpers are designed so the cap-200 cap-by-recency and the
catalyst-by-latest-timestamp pathologies stop dominating the daily brief — see
the design memo at ``/home/jacoren/.claude/plans/witty-marinating-stroustrup.md``.
"""

from __future__ import annotations

import re

JACCARD_THRESHOLD = 0.6
MIN_TOKEN_OVERLAP = 3
ENTITY_JACCARD_THRESHOLD = 0.3

# Tokens shorter than this are dropped wholesale (kills "the", "and", "for",
# ticker stubs, etc. without enumerating every short stopword by hand).
_MIN_TOKEN_LEN = 4

# Longer-form filler that survives the length filter but carries no
# discrimination signal in news headlines. Keep small; the length-4 cutoff
# already does the bulk of the work.
_STOPWORDS = frozenset(
    {
        "says", "said",
        "report", "reports", "reported", "reporting",
        "after", "before",
        "with", "without",
        "from", "into", "onto", "upon",
        "this", "that", "what", "when", "where", "which",
        "will", "would", "could", "should", "shall",
        "have", "been", "were",
        "news", "update", "updates", "breaking", "story", "article",
        "again",
    }
)

# Strip these so "Tesla's" collapses to "teslas" rather than splitting into
# "tesla" + the orphan "s" (which would then get dropped by the length filter
# and lose the entity).
_APOSTROPHES = ("'", "’", "ʼ")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def normalize_title(s: str) -> frozenset[str]:
    """Lowercase, strip punctuation, drop short tokens + inline stopwords.

    Returns a frozenset so callers can use it directly in set arithmetic and
    so equal titles produce equal hashes (handy for tests).
    """
    if not s:
        return frozenset()
    cleaned = s
    for ap in _APOSTROPHES:
        cleaned = cleaned.replace(ap, "")
    tokens = _TOKEN_RE.findall(cleaned.lower())
    return frozenset(
        t for t in tokens if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS
    )


def titles_similar(
    a: str,
    b: str,
    *,
    threshold: float = JACCARD_THRESHOLD,
    min_overlap: int = MIN_TOKEN_OVERLAP,
) -> bool:
    """Return True iff titles cluster under both Jaccard AND absolute-overlap bars.

    The absolute-overlap floor kills the short-headline failure mode where a
    2-of-3-tokens overlap would otherwise satisfy a relaxed Jaccard threshold
    despite being two unrelated stories.
    """
    set_a = normalize_title(a)
    set_b = normalize_title(b)
    if not set_a or not set_b:
        return False
    intersection_size = len(set_a & set_b)
    if intersection_size < min_overlap:
        return False
    union_size = len(set_a | set_b)
    return intersection_size / union_size >= threshold


def entity_jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity over two entity sets. Returns 0.0 if either is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


__all__ = [
    "ENTITY_JACCARD_THRESHOLD",
    "JACCARD_THRESHOLD",
    "MIN_TOKEN_OVERLAP",
    "entity_jaccard",
    "normalize_title",
    "titles_similar",
]
