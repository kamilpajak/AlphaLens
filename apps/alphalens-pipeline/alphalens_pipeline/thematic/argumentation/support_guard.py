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
from the record, and checking the rendered shape against it, is the engineering
control. This module is the second half of that.

WHAT THIS CHECK CAN AND CANNOT CLAIM
------------------------------------
It is a LEXICAL detector with a bounded, English-only phrase list, so its
``clean`` rate measures the LIST'S RECALL and not the prose's honesty. It cannot
be described as making the unsupported shape "unrenderable" — a paraphrase
outside the list renders exactly as before. That is why the suppressed matches
are stamped beside the fired ones, why the first weeks are a manual read, and
why nothing downstream may gate on the result.

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
#
# RECALL IS MEASURED, NOT ASSUMED. This list was built by enumeration, so its
# near neighbours were the first thing missing from it ("stands to profit" was
# absent while "stands to gain" was present). A miss is indistinguishable from
# compliance in the stamped columns, so the ``clean`` rate is the LEXICON'S
# RECALL and not the guard's accuracy — which is why the suppressed matches are
# stamped alongside (``brief_support_guard_suppressed``) and why the first-weeks
# read is a manual one. The list is also ENGLISH-ONLY; the pre-guard drift check
# rejects CJK only, so a Latin-script non-English brief passes both.
#
# Entries must be hyphen-free: :func:`_normalise` maps a hyphen to a space on
# BOTH sides, and ``EveryLexiconEntryMustBeAbleToFire`` enforces that every
# entry can still fire on a minimal carrier sentence.
BANNED_BENEFIT_PHRASES: tuple[str, ...] = (
    "benefits",
    "benefit",
    "benefiting",
    "benefited",
    "will gain",
    "gaining",
    "stands to gain",
    "stands to benefit",
    "stands to profit",
    "is positioned to",
    "positioned to win",
    "wins",
    "will win",
    "will capture",
    "capture share",
    "takes share",
    "profits from",
    "profiting from",
    "boosts",
    "will boost",
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
    "second order beneficiary",
    "primary beneficiary",
    "upside from",
    "tailwind for",
    "windfall",
    "levered to",
    "pricing power",
    "margin expansion",
    "top line growth",
    "poised to",
    "set to gain",
    "should see demand",
    "will see demand",
    "expands margins",
    "will expand margins",
)

# Tier 2 — polysemous stems that fire ONLY with a same-clause economic anchor,
# mirroring ``eval/financing_claims._TIER2``. Without the anchor "drive train",
# "capture rate" and "the stock gains 3%" would be violations.
#
# A stem here must NOT also sit in tier 1: tier 1 is scanned first and returns
# on the first FIRED match, so a duplicated stem silently bypasses the anchor
# requirement. Pinned by ``PolysemousStemsStayOnTheAnchoredPath.test_no_stem_sits_in_both_tiers``.
_TIER2_TOKENS: tuple[str, ...] = (
    "drives",
    "drive",
    "captures",
    "capture",
    "lifts",
    "lift",
    "gains",
    "gain",
)
_TIER2_ANCHORS: tuple[str, ...] = ("revenue", "margin", "margins", "earnings", "demand", "share")

_CLAUSE_BOUNDARY = ".;:\n"

# Coordinators that open a NEW independent clause. A cue on the far side of one
# of these does not govern the phrase: in "momentum could fade, even though the
# company benefits", the modal belongs to "fade".
_COORDINATORS: tuple[str, ...] = (
    " but ",
    " though ",
    " although ",
    " yet ",
    " while ",
    " whereas ",
    " however ",
    " still ",
    " since ",
    " because ",
    " so ",
    " and ",
)

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

# Generic ways the prose refers to the candidate without naming it. Bare "it" is
# deliberately absent — it corefers with anything ("incumbents capture share
# because it is a commodity") and would re-open the subject hole.
_SELF_REFERENCES: tuple[str, ...] = (
    "the company",
    "this company",
    "the issuer",
    "the shares",
    "the stock",
    "its",
)

# Corporate suffixes dropped from a company name before it is used as a subject
# term: "Inc" would match any sentence mentioning any incorporated party.
_NAME_STOPWORDS = frozenset(
    {
        "inc",
        "corp",
        "corporation",
        "company",
        "co",
        "ltd",
        "limited",
        "plc",
        "holdings",
        "group",
        "the",
        "and",
    }
)
_MIN_NAME_TOKEN = 4

SUPPRESSED_BY_NEGATION = "negation"
SUPPRESSED_BY_CONDITIONAL = "conditional"
SUPPRESSED_BY_QUOTED = "quoted"
SUPPRESSED_BY_NO_SUBJECT = "no_subject"

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


def _clause_after(text_lower: str, end: int) -> str:
    """The rest of the phrase's clause, from its end."""
    for i in range(end, len(text_lower)):
        if text_lower[i] in _CLAUSE_BOUNDARY:
            return text_lower[end:i]
    return text_lower[end:]


def _clause_around(text_lower: str, start: int, end: int) -> str:
    """The whole clause containing the phrase, both sides."""
    return (
        _clause_before(text_lower, start) + text_lower[start:end] + _clause_after(text_lower, end)
    )


def _last_coordinator(fragment: str) -> int:
    """Index just past the LAST coordinator in ``fragment``, else 0."""
    cut = 0
    for coordinator in _COORDINATORS:
        found = fragment.rfind(coordinator)
        if found != -1:
            cut = max(cut, found + len(coordinator))
    return cut


def _governing_scope(text_lower: str, start: int) -> str:
    """What precedes the phrase and can still GOVERN it.

    A subordinating conditional governs the main clause that follows it across a
    comma ("if the contract is confirmed, XYZ benefits"), so a bare comma is NOT
    a boundary here. A coordinator IS: it opens a new independent clause, and a
    modal on its far side belongs to that clause, not to this phrase. Without
    this cut, "momentum could fade, even though the company benefits" and "exit
    if the buildout stalls, since XYZ benefits" both read as hedged — which is
    the exact shape the bear-case and exit-line instructions teach the model to
    write.
    """
    before = _clause_before(text_lower, start)
    return before[_last_coordinator(before) :]


def _segment_before(text_lower: str, start: int) -> str:
    """What precedes the phrase inside its own comma/coordinator segment.

    Tighter than :func:`_governing_scope` because a negation does NOT reach
    across a comma: in "no cash-flow path was established, though the buildout
    benefits its optics line", the negation belongs to "was established".
    """
    fragment = _governing_scope(text_lower, start)
    comma = fragment.rfind(",")
    return fragment[comma + 1 :] if comma != -1 else fragment


def _segment_after(text_lower: str, end: int) -> str:
    """The phrase's own segment, from its end to the next segment boundary."""
    after = _clause_after(text_lower, end)
    comma = after.find(",")
    if comma != -1:
        after = after[:comma]
    for coordinator in _COORDINATORS:
        found = after.find(coordinator)
        if found != -1:
            after = after[:found]
    return after


def _segment_around(text_lower: str, start: int, end: int) -> str:
    """The phrase's own segment, both sides. The scope of the subject test."""
    return (
        _segment_before(text_lower, start) + text_lower[start:end] + _segment_after(text_lower, end)
    )


def subject_terms(ticker: str, company_name: str) -> tuple[str, ...]:
    """Every way the prose can name THIS company, lower-cased and normalised.

    The ticker, the significant tokens of the registered name, and the generic
    self-references. Short and boilerplate name tokens are dropped so "Inc" or
    "Group" cannot anchor a sentence about somebody else.
    """
    terms = {t for t in (_normalise(ticker).strip().lower(),) if t}
    for token in re.split(r"[^a-z0-9]+", _normalise(company_name).lower()):
        if len(token) >= _MIN_NAME_TOKEN and token not in _NAME_STOPWORDS:
            terms.add(token)
    return tuple(sorted(terms | set(_SELF_REFERENCES)))


def _names_the_candidate(segment: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(term)}\b", segment) for term in terms)


def _is_quoted(text: str, phrase_start: int) -> bool:
    """True when the phrase sits inside quotation marks (a cited headline)."""
    return text.count('"', 0, phrase_start) % 2 == 1


def _suppressor(
    text: str, text_lower: str, start: int, end: int, terms: tuple[str, ...]
) -> str | None:
    """Why this match does NOT count, or ``None`` when it fires.

    Ordered most-fundamental first: a sentence that is not about this company at
    all cannot assert that this company benefits, whatever its mood.

    Known residual, in the SAFE direction for a detector that may not gate: a
    segment naming both a rival and the candidate ("larger rivals benefit more
    than XYZ") still fires. Tightening that needs real parsing, and a false
    NEGATIVE only under-counts a telemetry gauge, whereas a false POSITIVE
    withholds honest prose from precisely the honest-uncertainty rows this
    increment exists to keep visible.
    """
    if not _names_the_candidate(_segment_around(text_lower, start, end), terms):
        return SUPPRESSED_BY_NO_SUBJECT
    if any(cue.search(_segment_before(text_lower, start)) for cue in _NEGATION_RES):
        return SUPPRESSED_BY_NEGATION
    # The matched phrase itself is EXCLUDED from the conditional scan: an entry
    # carrying its own modal ("should see demand") would otherwise suppress
    # itself on every possible input.
    if any(cue.search(_governing_scope(text_lower, start)) for cue in _CONDITIONAL_RES):
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


def _first_match(field: str, text: str, terms: tuple[str, ...]) -> SupportViolation | None:
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
                suppressed_by=_suppressor(normalised, lowered, start, end, terms),
            )
            if violation.suppressed_by is None:
                return violation
            suppressed = suppressed or violation
    return suppressed


def _candidate_phrases() -> list[tuple[str, bool]]:
    """(phrase, is_tier2), longest first so "will gain" beats "gaining".

    Both tiers are :func:`_normalise`-d here, exactly as the scanned text is.
    Compiling the raw entry instead is why the hyphenated entry could never
    match in EITHER spelling: the text became "second order beneficiary" while
    the pattern stayed "second\\-order beneficiary".
    """
    tier1 = sorted((_normalise(p) for p in BANNED_BENEFIT_PHRASES), key=len, reverse=True)
    return [(p, False) for p in tier1] + [(_normalise(t), True) for t in _TIER2_TOKENS]


def check_support_language(
    brief: Mapping[str, str],
    *,
    causal_support: str,
    grounding: str,
    ticker: str,
    company_name: str = "",
) -> list[SupportViolation]:
    """Scan a parsed brief for benefit claims the record cannot support.

    Returns ``[]`` WITHOUT scanning when :func:`guard_applies` is false, so an
    ``established`` row is never touched.

    ``ticker`` and ``company_name`` are what "this company" MEANS to the scan.
    They are required because the contract is about the SUBJECT of the benefit,
    not about the vocabulary: ``bear_summary`` is a mandatory field whose own
    instruction asks for competitor and momentum risks, so "larger rivals
    benefit more from any category spend" is the prose working as designed and
    must not be read as a violation.
    """
    if not guard_applies(causal_support=causal_support, grounding=grounding):
        return []
    terms = subject_terms(ticker, company_name)
    violations: list[SupportViolation] = []
    for field in GUARDED_FIELDS:
        text = brief.get(field) or ""
        if not isinstance(text, str) or not text.strip():
            continue
        found = _first_match(field, text, terms)
        if found is not None:
            violations.append(found)
    return violations


__all__ = [
    "BANNED_BENEFIT_PHRASES",
    "CAUSAL_SUPPORT_NOT_A_FORECAST",
    "GUARDED_FIELDS",
    "NO_RECORD",
    "SUPPORT_GUARD_VERSION",
    "SUPPRESSED_BY_CONDITIONAL",
    "SUPPRESSED_BY_NEGATION",
    "SUPPRESSED_BY_NO_SUBJECT",
    "SUPPRESSED_BY_QUOTED",
    "SupportViolation",
    "check_support_language",
    "guard_applies",
    "subject_terms",
]
