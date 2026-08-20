"""Deterministic check: prose may not assert a benefit the record cannot support.

WHY THIS EXISTS
---------------
The channel record is computed at stage B, persisted, carried through scoring —
and was then dropped one line before the brief prompt, which asked for "1
sentence thesis why this ticker benefits from the theme". The benefit was
PRESUPPOSED BY THE INSTRUCTION: the model was never asked *whether*, only *why*.
The status was attached afterwards, in a parquet column the Django ingest drops
by design. So the prose was the only channel-related artefact the operator ever
saw, and it was the one artefact that never saw the channel record.

The human-factors evidence says a badge beside confident prose does not fix
that. Bansal et al. (CHI 2021) measured that explanations raise acceptance
whether or not the recommendation is correct; Steyvers et al. (Nature Machine
Intelligence, 2025) find the intervention that narrows the calibration gap is
aligning the explanation's OWN hedging with the model's uncertainty. In
hazard-control terms a label is an administrative control; generating the prose
from the record, and making the unsupported shape unrenderable, is the
engineering control. This module is the second half of that.

WHY PIPELINE-SIDE AND NOT ``alphalens_research/eval/``
-------------------------------------------------------
``eval/faithfulness.py`` and ``eval/financing_claims.py`` are the right SHAPE but
the wrong TIER: they are research-side telemetry, the workspace DAG forbids
``alphalens_pipeline`` importing ``alphalens_research``, and this check has to
run inside ``generate_brief`` BEFORE a row ships. Research may later import this
module to compute corpus rates — that direction is allowed.

The clause / negation / quote primitives below are re-implemented rather than
imported, for that DAG reason, and the duplication is deliberate and noted. The
extract-on-second-use rule points at moving those primitives DOWN into the
pipeline and having research import them, as a separate refactor — never at
weakening the DAG.

SCOPE, AND WHY IT IS NARROW
---------------------------
Inert unless the record cannot support a benefit claim: bottom support level, no
record at all, or a grounding failure. For ``established`` / ``suggestive`` it
must NEVER fire. A guard that also policed well-grounded prose would start
rewriting it and would drift into an editorial filter, which is a different and
unauthorised thing.

WHAT IT IS NOT
--------------
It is NOT a deletion gate. When it trips the caller regenerates once and, if the
second draw also violates, withholds the four PROSE strings while the row ships
unchanged — same rank, same trade setup, same deterministic signals. Nothing
here removes a candidate.

Design memo: ``docs/research/grounding_and_prose_honesty_design_2026_08_20.md`` §5.6.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from alphalens_pipeline.thematic.mapping.channel_assessor import (
    CAUSAL_SUPPORT_NOT_A_FORECAST,
    GROUNDING_GROUNDED,
    SUPPORT_NOT_ESTABLISHED,
)

SUPPORT_GUARD_VERSION = "support-guard-v1-2026-08-20"

# The facts-level value for "the assessor never produced a record for this row"
# (an outage, or a row predating the columns). A FOURTH facts value, NOT a fourth
# taxonomy level: without it an outage — which by construction carries the lowest
# support level — would make the model write "no company-specific path was
# established", asserting a judgement no model ever made.
NO_RECORD = "no_record"

# The prose fields the guard scans. Deliberately the brief's four schema-required
# strings and nothing else.
GUARDED_FIELDS: tuple[str, ...] = (
    "tldr",
    "supply_chain_reasoning",
    "bear_summary",
    "catalyst_failure_exit",
)

# Tier 1 — affirmative benefit assertions. Matched whole-word / whole-phrase on
# hyphen-normalised, lower-cased text.
BANNED_BENEFIT_PHRASES: tuple[str, ...] = (
    "benefits",
    "benefit",
    "benefiting",
    "benefited",
    "will gain",
    "gains",
    "gaining",
    "stands to gain",
    "is positioned to",
    "positioned to win",
    "wins",
    "will win",
    "captures",
    "will capture",
    "capture share",
    "profits from",
    "profiting from",
    "boosts",
    "will boost",
    "lifts",
    "will lift",
    "drives revenue",
    "drives growth",
    "will drive revenue",
    "will drive growth",
    "translates into revenue",
    "translates to revenue",
    "flows through to earnings",
    "accrues to",
    "is a beneficiary",
    "direct beneficiary",
    "second-order beneficiary",
    "primary beneficiary",
    "upside from",
    "tailwind for",
    "poised to",
    "set to gain",
    "should see demand",
    "will see demand",
    "expands margins",
    "will expand margins",
)

# Tier 2 — polysemous stems that fire ONLY with a same-clause economic anchor,
# mirroring ``eval/financing_claims._TIER2``. Without the anchor "drive train"
# and "capture rate" would be violations.
_TIER2_TOKENS: tuple[str, ...] = ("drives", "drive", "captures", "capture", "lifts", "lift")
_TIER2_ANCHORS: tuple[str, ...] = ("revenue", "margin", "margins", "earnings", "demand", "share")

_CLAUSE_BOUNDARY = ".;:\n"

# Whole-word for alphabetic cues; literal for multi-word ones. Same compile idiom
# as the eval side, and the same reason: "now" contains "no", "announce" contains
# "no", and a substring match there would suppress real violations.
_NEGATION_CUES: tuple[str, ...] = (
    "not",
    "no",
    "never",
    "cannot",
    "without",
    "hardly",
    "n't",
    "far from",
    "rather than",
    "instead of",
)

# The explicit conditional qualification the contract requires. A forward
# statement is allowed exactly when it is conditional on the link the record says
# is missing: "if the reported contract is confirmed, XYZ would gain a customer"
# passes, "XYZ benefits from the theme" does not.
_CONDITIONAL_CUES: tuple[str, ...] = (
    "if",
    "were",
    "would",
    "could",
    "should",
    "may",
    "might",
    "only if",
    "conditional on",
    "unless",
    "the event does not state",
    "provided that",
)

SUPPRESSED_BY_NEGATION = "negation"
SUPPRESSED_BY_CONDITIONAL = "conditional"
SUPPRESSED_BY_QUOTED = "quoted"

_SPAN_CHARS = 120


def _compile_cues(cues: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    return tuple(
        re.compile(rf"\b{re.escape(cue)}\b" if cue.isalpha() else re.escape(cue)) for cue in cues
    )


_NEGATION_RES = _compile_cues(_NEGATION_CUES)
_CONDITIONAL_RES = _compile_cues(_CONDITIONAL_CUES)
_TIER2_ANCHOR_RE = re.compile("|".join(rf"\b{re.escape(a)}\b" for a in _TIER2_ANCHORS))


@dataclass(frozen=True, slots=True)
class SupportViolation:
    """One matched benefit phrase, fired or suppressed.

    A SUPPRESSED match is still returned. "The guard did not fire" and "the guard
    never looked" must not merge, and the first-weeks manual read needs to be
    able to check the suppressors themselves rather than trust them.
    """

    field: str
    span: str
    matched_phrase: str
    suppressed_by: str | None


def guard_applies(*, causal_support: str, grounding: str) -> bool:
    """True only when the record cannot support a benefit claim.

    Three in-scope conditions: the bottom support level, no record at all, and a
    grounding failure (which overlays ANY support level — an event that is not
    about the theme cannot support a benefit claim however confident the chain
    reads).
    """
    if causal_support in (SUPPORT_NOT_ESTABLISHED, NO_RECORD):
        return True
    return grounding != GROUNDING_GROUNDED


def _normalise(text: str) -> str:
    """Hyphen to space, so "second-order beneficiary" matches either spelling."""
    return text.replace("-", " ")


def _clause_before(text_lower: str, phrase_start: int) -> str:
    """The phrase's own clause, up to its start.

    Bounded left by the previous clause boundary, so a negation in an EARLIER
    sentence never licenses an unconditional claim in this one.
    """
    lo = 0
    for i in range(phrase_start - 1, -1, -1):
        if text_lower[i] in _CLAUSE_BOUNDARY:
            lo = i + 1
            break
    return text_lower[lo:phrase_start]


def _clause_around(text_lower: str, start: int, end: int) -> str:
    """The whole clause containing the phrase, both sides."""
    hi = len(text_lower)
    for i in range(end, len(text_lower)):
        if text_lower[i] in _CLAUSE_BOUNDARY:
            hi = i
            break
    return _clause_before(text_lower, start) + text_lower[start:hi]


def _is_quoted(text: str, phrase_start: int) -> bool:
    """True when the phrase sits inside quotation marks (a cited headline)."""
    return text.count('"', 0, phrase_start) % 2 == 1


def _suppressor(text: str, text_lower: str, start: int, end: int) -> str | None:
    if any(cue.search(_clause_before(text_lower, start)) for cue in _NEGATION_RES):
        return SUPPRESSED_BY_NEGATION
    if any(cue.search(_clause_around(text_lower, start, end)) for cue in _CONDITIONAL_RES):
        return SUPPRESSED_BY_CONDITIONAL
    if _is_quoted(text, start):
        return SUPPRESSED_BY_QUOTED
    return None


def _span(text: str, start: int, end: int) -> str:
    lo = max(0, start - _SPAN_CHARS // 2)
    hi = min(len(text), end + _SPAN_CHARS // 2)
    return text[lo:hi].strip()


def _tier2_is_anchored(text_lower: str, start: int, end: int) -> bool:
    return bool(_TIER2_ANCHOR_RE.search(_clause_around(text_lower, start, end)))


def _first_match(field: str, text: str) -> SupportViolation | None:
    """The FIRST fired match in one field, else the first suppressed one.

    Collapsed per field on purpose: a field asserting a benefit twice is one
    violation, mirroring ``financing_claims._collapse_per_subtype``.
    """
    normalised = _normalise(text)
    lowered = normalised.lower()
    suppressed: SupportViolation | None = None
    for phrase, tier2 in _candidate_phrases():
        for match in re.finditer(rf"\b{re.escape(phrase)}\b", lowered):
            start, end = match.start(), match.end()
            if tier2 and not _tier2_is_anchored(lowered, start, end):
                continue
            violation = SupportViolation(
                field=field,
                span=_span(normalised, start, end),
                matched_phrase=phrase,
                suppressed_by=_suppressor(normalised, lowered, start, end),
            )
            if violation.suppressed_by is None:
                return violation
            suppressed = suppressed or violation
    return suppressed


def _candidate_phrases() -> list[tuple[str, bool]]:
    """(phrase, is_tier2), longest first so "will gain" beats "gains"."""
    tier1 = sorted(BANNED_BENEFIT_PHRASES, key=len, reverse=True)
    return [(p, False) for p in tier1] + [(t, True) for t in _TIER2_TOKENS]


def check_support_language(
    brief: Mapping[str, str], *, causal_support: str, grounding: str
) -> list[SupportViolation]:
    """Scan a parsed brief for benefit claims the record cannot support.

    Returns ``[]`` WITHOUT scanning when :func:`guard_applies` is false, so an
    ``established`` row is never touched.
    """
    if not guard_applies(causal_support=causal_support, grounding=grounding):
        return []
    violations: list[SupportViolation] = []
    for field in GUARDED_FIELDS:
        text = brief.get(field) or ""
        if not isinstance(text, str) or not text.strip():
            continue
        found = _first_match(field, text)
        if found is not None:
            violations.append(found)
    return violations


def guard_violations(brief: Mapping[str, str], *, causal_support: str, grounding: str) -> int:
    """How many fields FIRED (suppressed matches do not count)."""
    return len(
        [
            v
            for v in check_support_language(
                brief, causal_support=causal_support, grounding=grounding
            )
            if v.suppressed_by is None
        ]
    )


__all__ = [
    "BANNED_BENEFIT_PHRASES",
    "CAUSAL_SUPPORT_NOT_A_FORECAST",
    "GUARDED_FIELDS",
    "NO_RECORD",
    "SUPPORT_GUARD_VERSION",
    "SupportViolation",
    "check_support_language",
    "guard_applies",
    "guard_violations",
]
