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
Inert unless the record cannot support a benefit claim. Four ways it cannot:
bottom support level, no record at all, a grounding failure, and a record that
describes HARM to this company. The guard still never polices
prose that agrees with a well-evidenced, benefit-direction record — it would
drift into an editorial filter, which is a different and unauthorised thing.

The fourth condition is the only one that reads the chain TEXT rather than a
graded label, and it exists because the support level answers "how well is this
chain evidenced", never "which way does it point". A ``suggestive`` +
``grounded`` record reading "potential negative impact on revenue for small-cap
retailers like Grocery Outlet" is a well-formed record of an ADVERSE mechanism,
and prose asserting a benefit beside it contradicts the row's own record
(issue #1070). Direction is tested lexically over the channel text, and asks WHO is harmed —
a harm phrase counts only where the segment names the candidate and is not
negated — with the same measured-recall caveat as the benefit lexicon below.

Deliberately NOT done here: adding a ``channel_direction`` field to the
assessor's schema. It is the better instrument — a label on the model's own
chain rather than a lexical test over it — but it would move
``channel_config_version`` and open a third pre-registered cohort, which
``docs/research/channel_feature_forward_prereg_2026_08_19.md`` A1.3 forbids and
its Amendment 2 §A2.3 records as rejected for that reason.

WHAT IT IS NOT
--------------
It is NOT a deletion gate. When it trips the caller re-rolls and, if the next
draw also violates, withholds the four PROSE strings while the row ships
unchanged — same rank, same trade setup, same deterministic signals. Nothing
here removes a candidate.

The re-roll comes out of the brief's ONE SHARED retry budget, not a budget of
its own, so a row whose first draw came back empty and whose retry then violates
is withheld after a single guard-evaluated draw. That is a deliberate cost bound
(each draw is a paid Pro call) and it is why the stamped status distinguishes
``fired_unrecovered`` and ``no_prose`` from ``clean``: the number of draws the
guard actually saw has to be recoverable from the record.

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

SUPPORT_GUARD_VERSION = "support-guard-v2-2026-08-21"

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

# Harm-direction phrases, matched over the channel record's LAST LINK only (see
# :func:`channel_describes_harm`). Same normalisation, same whole-phrase rule and the
# same hyphen-free requirement as the benefit lexicon above.
#
# RECALL IS MEASURED, NOT ASSUMED — and here the measurement is far thinner in
# one direction than the other. The true-positive evidence is N=2: the two
# channel records in issue #1070, and nothing else. Both were overwritten by a
# later pipeline slot, so they survive only as quotations. The FALSE-positive
# side is stronger: every entry below was measured against 32 distinct real
# benefit-direction channel records (14 from the committed golden fixtures, 9
# from the assessor cassettes, 19 from the live store, deduplicated) and none of
# them fires on any of those, at terminal-link scope or over the whole text.
#
# Entries are grouped by what motivates them, because the motivation is uneven
# and hiding that would repeat the failure the benefit lexicon confesses to:
#   * OBSERVED     — quoted from the two harm records.
#   * INVERTED     — the antonym of a terminal arm that DOES occur in the 32
#                    benefit records ("boosting revenue", "increases demand",
#                    "improving margins", and the lexicon's own "takes share").
# No entry is here on plausibility alone.
HARM_DIRECTION_PHRASES: tuple[str, ...] = (
    # OBSERVED (issue #1070).
    "negative impact",
    "reduced customer demand",
    "lower revenue",
    # INVERTED — revenue / sales arms.
    "lower revenues",
    "reduced revenue",
    "declining revenue",
    "revenue declines",
    "lower sales",
    "declining sales",
    # INVERTED — demand arms.
    "reduced demand",
    "lower demand",
    "weaker demand",
    "softer demand",
    "declining demand",
    # INVERTED — margin arms.
    "margin compression",
    "margin pressure",
    "pressure on margins",
    # INVERTED — share arms (mirrors "takes share" / "capture share" above).
    "lose share",
    "loses share",
    # BASE FORMS. The assessor writes suggestive chains in modal language, so
    # "could reduce customer demand" is at least as natural as "reduced customer
    # demand" — and the participle-only list missed the real #1070 record the
    # moment it was reworded that way. This is the same near-neighbour gap the
    # benefit lexicon shipped with ("stands to profit" absent, "stands to gain"
    # present), found the same way: by a test written from a real record rather
    # than from the list.
    "reduce customer demand",
    "reduce demand",
    "reduce revenue",
    "reduce sales",
    "lowers revenue",
    "lowers demand",
    "weigh on revenue",
    "weighs on revenue",
    "weigh on margins",
    "weighs on margins",
)

# Chain separators the assessor actually writes. The record is an arrow chain by
# convention ("event -> mechanism -> effect on this company"), and the unicode
# arrow occurs in the live store beside the ASCII one.

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


def _normalise(text: str) -> str:
    """Hyphen to space, so "second-order beneficiary" matches either spelling."""
    return text.replace("-", " ")


def _compile_cues(cues: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    return tuple(
        re.compile(rf"\b{re.escape(cue)}\b" if cue.isalpha() else re.escape(cue)) for cue in cues
    )


_NEGATION_RES = _compile_cues(_NEGATION_CUES)
_CONDITIONAL_RES = _compile_cues(_CONDITIONAL_CUES)
_TIER2_ANCHOR_RE = re.compile("|".join(rf"\b{re.escape(a)}\b" for a in _TIER2_ANCHORS))
# Whole-phrase on both ends, unlike ``_compile_cues``: every harm entry is a
# phrase, and a bare substring would match "lower revenues" inside a longer
# token. Entries are normalised here exactly as the scanned text is.
_HARM_RE = re.compile("|".join(rf"\b{re.escape(_normalise(p))}\b" for p in HARM_DIRECTION_PHRASES))


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


# Arrows are clause separators inside a channel record — see
# :func:`channel_describes_harm`. Longest first so "-->" is not left with a
# dangling "-".
_ARROW_RE = re.compile(r"-->|->|=>|\u2192")


def _named_subject_terms(ticker: str, company_name: str) -> tuple[str, ...]:
    """Subject terms for the DIRECTION test: the NAME only, no generic pronouns.

    :func:`subject_terms` also returns "its", "the company", "the stock" and the
    other generic self-references, which is right for the prose arm — a brief is
    about one company, so a pronoun in it refers to that company.

    A channel record is not. It routinely names a rival, a customer or the macro
    economy, and a clause about any of them can borrow exactly those pronouns:
    "full-price retailers see lower revenue at ITS stores" would otherwise read
    as harm to the candidate. So the direction test insists on the NAME.

    The cost is recall — a final arm that says only "its revenue falls" is a
    miss. That is the cheaper error here by this module's own ranking, and it is
    the same trade the prose arm makes in the opposite direction.
    """
    generic = set(subject_terms("", ""))
    return tuple(t for t in subject_terms(ticker, company_name) if t not in generic)


def channel_describes_harm(channel_text: str, *, ticker: str, company_name: str) -> bool:
    """True when the record describes harm TO THIS COMPANY.

    The question is WHO is harmed, so the test asks that directly: a harm phrase
    counts only when the segment carrying it NAMES THE CANDIDATE and is not
    negated. Both checks reuse the prose arm's own primitives
    (:func:`_names_the_candidate`, :data:`_NEGATION_RES`), in its order —
    subject first, because a segment that is not about this company cannot say
    this company is harmed, whatever its mood.

    An earlier revision answered the same question by POSITION, scanning only
    the chain's last link on the theory that earlier links describe the world
    and the final one describes the candidate. Adversarial review killed it, and
    the measurements are worth keeping because they are the reason this file
    does not scope:

    * It fired on harm to a RIVAL inside the final arm — "Ollie's gains share
      while full-price retailers lose share". That is the substitution /
      trade-down family, whose mechanism IS a rival losing, and it is ~21% of
      live records. Whether the loser lands in the middle arm or the last one is
      a wording coin flip, so position cannot separate them.
    * It fired on an explicit DENIAL of harm — "sees no negative impact on
      revenue and gains share" — because it skipped the negation suppressor the
      prose arm has always had.
    * It MISSED the real #1070 record reworded so the harm clause was no longer
      the last comma segment, and truncated at abbreviation periods, reducing
      one committed golden record to the fragment "'s offerings".
    * And it bought nothing: over every real benefit record then in the corpus,
      the lexicon fired zero times at last-link scope AND zero times over the
      whole text. The scoping was defended by a hand-authored fixture, not by
      any record the assessor ever wrote.

    Hedging in the chain is deliberately NOT suppressed, unlike the prose arm.
    The assessor writes ``suggestive`` chains in modal language by construction,
    so a conditional cue carries no information here; "could see reduced
    customer demand" is still a record of an adverse mechanism. That asymmetry
    is a decision, not an oversight.

    Residual, inherited from :func:`_suppressor` and on the same terms: a
    segment naming BOTH the candidate and a rival still fires. Separating two
    subjects in one clause needs real parsing. The failure is bounded and
    visible — a withheld brief is stamped, counted and regenerated once — and is
    only tolerable while the guard cannot gate selection.

    An empty ``channel_text`` is ABSENT EVIDENCE, not evidence of absence: it
    returns False, and the grounding / support conditions cover that row.
    """
    if not channel_text:
        return False
    terms = _named_subject_terms(ticker, company_name)
    if not terms:
        # Nothing to anchor the subject test on, so no segment can be shown to
        # be about this company. The least-claiming answer is "not harm".
        return False
    # Arrows separate the arms of a chain, so they separate CLAUSES. Without
    # this an arm about a competitor bleeds into the next arm about the
    # candidate, and "-> competitors face margin pressure -> this company gains
    # share" reads as harm to this company.
    #
    # BEFORE :func:`_normalise`, which maps a hyphen to a space and would turn
    # "->" into " >" — an arrow the pattern can no longer see. Getting this
    # order wrong is silent: the subject test then reads the whole chain as one
    # segment and every arm names the candidate.
    lowered = _normalise(_ARROW_RE.sub(". ", channel_text)).lower()
    for match in _HARM_RE.finditer(lowered):
        segment = _segment_around(lowered, match.start(), match.end())
        if not _names_the_candidate(segment, terms):
            continue
        if any(cue.search(_segment_before(lowered, match.start())) for cue in _NEGATION_RES):
            continue
        return True
    return False


def guard_applies(
    *,
    causal_support: str,
    grounding: str,
    channel_text: str,
    ticker: str,
    company_name: str,
) -> bool:
    """True only when the record cannot support a benefit claim.

    Four in-scope conditions: the bottom support level, no record at all, a
    grounding failure (which overlays ANY support level — an event that is not
    about the theme cannot support a benefit claim however confident the chain
    reads), and a record that describes harm TO THIS COMPANY.

    The last three arguments are REQUIRED, deliberately. An empty string is a
    real value the facts projection carries — the assessor blanks the text at
    ``not_established``, and the ``no_record`` shape projects "" — and those rows
    are already in scope on the first two conditions, so an empty text can only
    make the direction arm inert, never wrong. A MISSING argument is a different
    thing: a call site that forgot. Defaulting it would turn that mistake into
    silence, and silence here reads exactly like "this record describes no harm".
    So the caller must say.
    """
    if causal_support in (SUPPORT_NOT_ESTABLISHED, NO_RECORD):
        return True
    if grounding != GROUNDING_GROUNDED:
        return True
    return channel_describes_harm(channel_text, ticker=ticker, company_name=company_name)


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

    Known residual, and it is a FALSE POSITIVE — the direction this file
    otherwise treats as the worse one: a segment naming both a rival and the
    candidate ("larger rivals benefit more than XYZ") still fires, as does a
    company whose name carries a generic token ("entertainment") that a sentence
    about a competitor may reuse. Both withhold prose that should ship.

    It is accepted rather than fixed because separating the two subjects needs
    real parsing, and the failure is bounded and visible: a withheld brief is
    stamped, counted and re-generated once, so it shows up in the guard
    telemetry rather than silently altering a card. That is only tolerable while
    the guard cannot gate selection; if it ever does, this residual must be
    fixed first.
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
    channel_text: str = "",
) -> list[SupportViolation]:
    """Scan a parsed brief for benefit claims the record cannot support.

    Returns ``[]`` WITHOUT scanning when :func:`guard_applies` is false, so an
    ``established`` row with a benefit-direction chain is never touched.

    ``channel_text`` is the RECORD's own chain and is read only to decide scope,
    via :func:`channel_describes_harm`. The harm lexicon is never applied to the
    four prose fields: the prompt instructs the model to render the record's
    FALSIFIER into ``bear_summary`` and ``catalyst_failure_exit`` ("if Maravai's
    revenue does not increase, the suggested channel is invalidated"), so harm
    vocabulary in the prose is the contract working as designed.

    ``ticker`` and ``company_name`` are what "this company" MEANS to the scan.
    They are required because the contract is about the SUBJECT of the benefit,
    not about the vocabulary: ``bear_summary`` is a mandatory field whose own
    instruction asks for competitor and momentum risks, so "larger rivals
    benefit more from any category spend" is the prose working as designed and
    must not be read as a violation.
    """
    if not guard_applies(
        causal_support=causal_support,
        grounding=grounding,
        channel_text=channel_text,
        ticker=ticker,
        company_name=company_name,
    ):
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
    "HARM_DIRECTION_PHRASES",
    "NO_RECORD",
    "SUPPORT_GUARD_VERSION",
    "SUPPRESSED_BY_CONDITIONAL",
    "SUPPRESSED_BY_NEGATION",
    "SUPPRESSED_BY_NO_SUBJECT",
    "SUPPRESSED_BY_QUOTED",
    "SupportViolation",
    "channel_describes_harm",
    "check_support_language",
    "guard_applies",
    "subject_terms",
]
