"""The 6 dnia-jeden named predicates referenced by template YAML files.

Each predicate is a pure function over ``PredicateContext``: no I/O, no
hidden global state. Telemetry is intentionally NOT emitted from inside the
predicate — the engine wraps each call and increments
``TemplateMetrics.record_predicate`` so the telemetry path is one-deep and
testable from the engine's perspective without mocking module globals.

See ``docs/research/template_engine_design_2026_05_30.md`` §2.3 for the
rationale on the canonical set.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from alphalens_pipeline.thematic.extraction.templates.spec import (
    Article,
    PredicateRef,
    ResolvedEntity,
)


@dataclass
class PredicateContext:
    """Everything a predicate may inspect.

    ``blocklists`` is a named-dict of regex pattern lists so a template
    can reference a logical list (``url_blocklist``) without binding to
    the file on disk — keeps the engine testable.
    """

    article: Article
    resolved_entities: list[ResolvedEntity]
    blocklists: dict[str, list[str]] = field(default_factory=dict)


Predicate = Callable[[PredicateContext], bool]


# ---------------------------------------------------------------------------
# Individual predicate implementations
# ---------------------------------------------------------------------------


def _any_sentence_contains(ctx: PredicateContext, *, words: list[str]) -> bool:
    haystack = f"{ctx.article.title}\n{ctx.article.body}".lower()
    for w in words:
        if not w:
            continue
        # Word-boundary so "beats" does not match "heartbeats".
        pattern = r"\b" + re.escape(w.lower()) + r"\b"
        if re.search(pattern, haystack):
            return True
    return False


# Currency amount patterns. Two variants:
#   1) "$5 billion", "$1.2 million" — sign + space + magnitude word
#   2) "$250M", "$1.2B"             — sign + short form attached to number
# Locale variants ("£", "€") deliberately not covered in PR-1 — the ship
# templates target US issuers + US-feed sources. A follow-up YAML can
# extend the named regex via a list-form predicate when the corpus surfaces
# the need.
_AMOUNT_LONG = re.compile(
    r"\$\d+(?:[.,]\d+)?\s*(?:billion|million|bn|mn)",
    re.IGNORECASE,
)
_AMOUNT_SHORT = re.compile(r"\$\d+(?:[.,]\d+)?\s*[BM]\b")


def _amount_mentioned(ctx: PredicateContext) -> bool:
    haystack = f"{ctx.article.title}\n{ctx.article.body}"
    return bool(_AMOUNT_LONG.search(haystack) or _AMOUNT_SHORT.search(haystack))


def _entity_type_present(ctx: PredicateContext, *, type: str) -> bool:
    return any(e.role == type for e in ctx.resolved_entities)


def _not_in_blocklist(ctx: PredicateContext, *, list: str) -> bool:
    patterns = ctx.blocklists.get(list)
    if patterns is None:
        # Conservative: missing named list → pass (cannot prove blocked).
        # The companion design doc explicitly calls out that a config
        # typo must NOT silently drop legitimate articles. The engine
        # logs the missing-list event via predicate telemetry.
        return True
    url = ctx.article.url or ""
    return not any(re.search(p, url) for p in patterns)


_IR_SUBDOMAIN = re.compile(r"^https?://ir\.[\w\.-]+", re.IGNORECASE)
_PRESS_RELEASE_TITLE_MARKER = re.compile(r"\(press release\)", re.IGNORECASE)
_PRESS_RELEASE_SOURCES: frozenset[str] = frozenset(
    {
        "prnewswire",
        "businesswire",
        "globenewswire",
        "pr-newswire",
    }
)


def _is_press_release(ctx: PredicateContext) -> bool:
    source = (ctx.article.source or "").strip().lower()
    if source in _PRESS_RELEASE_SOURCES:
        return True
    if _PRESS_RELEASE_TITLE_MARKER.search(ctx.article.title or ""):
        return True
    return bool(_IR_SUBDOMAIN.match(ctx.article.url or ""))


# Listicle / promo title patterns — the exact concern that opened #143.
# The pattern targets "Top N", "Best [thing]", "Cheapest", "Guide to",
# "Coupon Codes" / "Promo Codes". Title-only matching keeps false-positive
# risk low (a body that happens to contain "top" doesn't kill the article).
_LISTICLE_PATTERN = re.compile(
    r"\b(top|best|cheapest|guide\s+to)\b\s+\d*|"
    r"\b(coupon|promo)\s+codes?\b",
    re.IGNORECASE,
)


def _not_listicle(ctx: PredicateContext) -> bool:
    return not bool(_LISTICLE_PATTERN.search(ctx.article.title or ""))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PREDICATE_REGISTRY: dict[str, Callable[..., bool]] = {
    "any_sentence_contains": _any_sentence_contains,
    "amount_mentioned": _amount_mentioned,
    "entity_type_present": _entity_type_present,
    "not_in_blocklist": _not_in_blocklist,
    "is_press_release": _is_press_release,
    "not_listicle": _not_listicle,
}


def available_predicates() -> list[str]:
    """Sorted list of registered predicate names — used by the validator."""
    return sorted(PREDICATE_REGISTRY.keys())


def evaluate(ref: PredicateRef, ctx: PredicateContext) -> bool:
    """Dispatch a :class:`PredicateRef` to its registered implementation.

    Raises:
        KeyError: if the predicate name is not registered — the YAML
            validator catches this at template-load time, so a runtime
            ``KeyError`` here means the template bypassed validation.
    """
    impl = PREDICATE_REGISTRY[ref.name]
    return bool(impl(ctx, **ref.kwargs))
