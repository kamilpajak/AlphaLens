"""Human-readable execution-side labels for entry tiers and TP tranches.

The operator must see ``E1``/``E2``/``E3`` for entry tiers and ``TP1``/``TP2``/
``TP3`` for take-profit tranches — never a raw crid (``...-entry-t0``), a 0-based
"tier 0", or a lowercase "tp1". These helpers render ONLY message / log text.
The crid, ``throttle_key`` (alert dedup), request-id / ``ExternalReference``
(idempotency), and every journal field keep their raw machine values — the
label is a presentation-only overlay.

NOTE: ``thematic/trade_setup/model.py`` (brief generation) already defines
``entry_tier_label`` / ``tp_tranche_label``. That is a DIFFERENT subsystem;
importing it here would couple execution to brief generation, so this module
keeps its own copy. The two MUST agree on the ``E{i+1}`` / ``TP{i+1}``
convention (1-based, ordinal).
"""

from __future__ import annotations

import re

_ENTRY_LABEL_PREFIX = "E"
_TP_LABEL_PREFIX = "TP"
_FIRE_SUFFIX = "-fire"

# The crid is ``<ticker>-<briefdate>-entry-t<i>`` and the fire id appends
# ``-fire``; capture the 0-based tier index anchored at the end.
_ENTRY_CRID_RE = re.compile(r"-entry-t(\d+)(?:" + re.escape(_FIRE_SUFFIX) + r")?$")
# A tranche tag / ref carries ``tp<n>`` (already 1-based) as its own segment.
_TP_TAG_RE = re.compile(r"^tp(\d+)$")
_TP_REF_RE = re.compile(r"-tp(\d+)(?:-|$)")


def human_entry_label(tier_index: int) -> str:
    """0-based tier index -> ``"E1"``, ``"E2"``, ... (1-based, ordinal)."""
    return f"{_ENTRY_LABEL_PREFIX}{tier_index + 1}"


def human_tp_label(index: int) -> str:
    """0-based tranche index -> ``"TP1"``, ``"TP2"``, ... (1-based, ordinal)."""
    return f"{_TP_LABEL_PREFIX}{index + 1}"


def entry_tier_index_from_crid(crid: str) -> int | None:
    """Recover the 0-based tier index from a ``...-entry-t<i>[-fire]`` crid.

    Returns ``None`` when the crid does not match the pattern (foreign /
    malformed), so callers can fall back rather than crash.
    """
    match = _ENTRY_CRID_RE.search(crid)
    return int(match.group(1)) if match else None


def entry_label_from_crid(crid: str) -> str:
    """``...-entry-t<i>[-fire]`` -> ``"E{i+1}"``.

    FALLBACK to the raw ``crid`` string when it does not match, so a malformed /
    foreign crid still prints something and never crashes.
    """
    index = entry_tier_index_from_crid(crid)
    return human_entry_label(index) if index is not None else crid


def tp_label_from_tag(tag: str) -> str:
    """``"tp<n>"`` (already 1-based) -> ``"TP<n>"``.

    FALLBACK to ``tag.upper()`` when the tag is not the ``tp<n>`` shape, so a
    non-tranche tag (e.g. ``"sl"``) still renders sensibly.
    """
    match = _TP_TAG_RE.match(tag)
    return f"{_TP_LABEL_PREFIX}{match.group(1)}" if match else tag.upper()


def human_label_from_external_reference(ref: str) -> str:
    """Render an orphan-alert line from a machine ``ExternalReference``.

    ``...-entry-t<i>[-fire]`` -> ``"<TICKER> E{i+1}"`` (``" (fire)"`` appended
    for the fire id); ``...-tp<n>-...`` -> ``"<TICKER> TP<n>"``. Anything else
    returns the raw ``ref`` unchanged (never crashes). The ticker prefix is the
    leading segment of the ref where present, so the line reads e.g.
    ``"OLN E1 (fire)"`` rather than the full machine reference.
    """
    entry_index = entry_tier_index_from_crid(ref)
    if entry_index is not None:
        suffix = " (fire)" if ref.endswith(_FIRE_SUFFIX) else ""
        return f"{_ticker_prefix(ref)} {human_entry_label(entry_index)}{suffix}".strip()
    tp_match = _TP_REF_RE.search(ref)
    if tp_match:
        return f"{_ticker_prefix(ref)} {_TP_LABEL_PREFIX}{tp_match.group(1)}".strip()
    return ref


def _ticker_prefix(ref: str) -> str:
    """The leading ``<ticker>`` segment of a machine ref (before the first dash)."""
    return ref.split("-", 1)[0]


__all__ = [
    "entry_label_from_crid",
    "entry_tier_index_from_crid",
    "human_entry_label",
    "human_label_from_external_reference",
    "human_tp_label",
    "tp_label_from_tag",
]
