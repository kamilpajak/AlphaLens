"""Canonical theme-string helper.

Themes originate as free-form LLM keywords (DeepSeek Flash extraction) and were
historically inconsistent — some ``underscore_slug``, some ``space separated`` —
because the model picked either style per concept and nothing canonicalised it.

We pick ONE shape — a slug — and apply it at every point a theme string ENTERS
the system (extraction normalisation) and is COMPARED across days (the novelty
rollup). Storage + the UI chip therefore stay uniform.
"""

from __future__ import annotations

import re

# Any run of characters that is not a lowercase letter or digit becomes a single
# underscore. Applied after lower-casing + trimming.
_SLUG_NONALNUM = re.compile(r"[^a-z0-9]+")


def slugify_theme(theme: str) -> str:
    """Canonical theme slug: lowercase, non-alphanumeric runs → single ``_``, trimmed.

    Idempotent — ``slugify_theme(slugify_theme(x)) == slugify_theme(x)``.

    Examples::

        "AI ethics"           -> "ai_ethics"
        "oil & gas"           -> "oil_gas"
        "defense_procurement" -> "defense_procurement"
        "5G rollout"          -> "5g_rollout"
        "  spaced  "          -> "spaced"

    Returns ``""`` for an empty / all-separator input; callers filter empties.

    NOTE: non-ASCII characters are NOT transliterated — they are treated as
    separators and dropped (e.g. ``"café tech"`` -> ``"caf_tech"``). Acceptable
    because thematic keywords are English-only by repo convention; a theme that
    is *entirely* non-ASCII would slug to ``""`` and be filtered.
    """
    return _SLUG_NONALNUM.sub("_", str(theme).strip().lower()).strip("_")
