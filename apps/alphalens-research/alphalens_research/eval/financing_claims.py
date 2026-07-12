"""T6.5 financing-claims detector — deterministic (NO-LLM), telemetry only.

A standalone detector that flags a brief text field asserting a financing EVENT
(capital raise / dilution / buyback / secondary or convertible offering / share
issuance / proceeds) when the row's fact index carries NO grounding financing
fact — which, by construction of the current
``prompts.py::_format_facts_block``, is ALWAYS true today (the facts block renders
ticker / company / theme / mcap / valuation / FCFF / insider / technicals /
durability / fundamentals-freshness / earnings only; no financing or
shares-outstanding line, and :data:`~alphalens_research.eval.measurement._COLUMN_TO_FACT_KEY`
carries no financing key). Pinned by
``tests/golden/test_financing_claims.py::TestFinancingPrecondition``.

**Separate module, own version.** This is NOT folded into the frozen
``score_brief`` / ``Atom`` / ``FaithfulnessResult`` (folding would change the gate's
atom-list shape and couple poolability). It carries its own
:data:`FINANCING_DETECTOR_VERSION` and reuses the frozen scorer's private
negation/quote/clause machinery + the measurement / triage helpers — it does NOT
reimplement scoring. The private-helper reuse is pinned by
``TestScorerHelperContract`` (mirrors the fabrication_triage meta-test).

**Diagnostic-only in v1.** Telemetry ONLY (memo §2 / §9): joins no outcome/return
ledger, touches no selection/ordering surface, performs no network I/O, and is
NOT joined to ``FaithfulnessResult.is_clean``. The first job is a MEASURED
retroactive baseline (a pre/post prompt-ban rate with Wilson CIs), not a third
gate — a gate would be green day-one on the 4 all-clean golden cassettes with no
live positive control.

**Grounding has two arms.** (1) :data:`_FINANCING_FACT_KEYS` over the fact index
(EMPTY today — structurally always False; a forward hook so the detector
self-suppresses once a shares_outstanding / offering_proceeds column is added to
the column map). (2) a ``source_event_title`` SUBTYPE-MATCHED escape: suppress a
financing assertion ONLY when the catalyst headline names a financing event of
the SAME subtype (DILUTIVE vs RETURN_OF_CAPITAL) the brief asserts. A revenue /
buyback headline that carries a ``$`` but names no matching-subtype financing
event does NOT escape → the assertion fires.

HONEST NOTE (memo §10): this is fidelity-to-facts, not truth. A real raise not in
``<facts>`` is still ungrounded; the title escape exists so a brief faithfully
reflecting a catalyst-announced raise of the matching subtype is not penalized.
The ``$`` figure of a fabricated raise is ALREADY caught as a numeric FABRICATED
atom by the frozen scorer — this detector adds the EVENT-framing catch as a
STRICTLY SEPARATE metric; never sum the two.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from alphalens_research.eval.fabrication_triage import _coerce_rows, _sentence_window

# NOTE: this module depends on several PRIVATE helpers across the three frozen
# eval modules. That contract is pinned by
# tests/golden/test_financing_claims.py::TestScorerHelperContract, which fails
# loudly in CI if any is renamed/dropped (kept private rather than promoted to
# public API per extract-on-2nd-use).
from alphalens_research.eval.faithfulness import (
    FAITHFULNESS_SCORER_VERSION,
    _clause_before,
    _is_negated,
    _is_quoted,
)
from alphalens_research.eval.measurement import (
    _rate_block,
    _year_month_of_row,
    fact_index_from_brief_row,
)

# --- Poolability key (memo §9) -----------------------------------------------
# Bump whenever the financing lexicon, the Tier-2 anchor set, the subtype map,
# the guard set, or the report shape changes. Distinct from
# FAITHFULNESS_SCORER_VERSION: the report is DUAL-stamped so a reader partitions
# on both (the detector reuses score_row's row->fact-index adapter, so the
# scorer version is a co-poolability key).
FINANCING_DETECTOR_VERSION = "t6.5-financing-v1.1-2026-07-12"

# --- Subtypes ----------------------------------------------------------------
SUBTYPE_DILUTIVE = "DILUTIVE"
SUBTYPE_RETURN_OF_CAPITAL = "RETURN_OF_CAPITAL"

# --- Grounding arm 1: financing fact keys (EMPTY today, forward hook) ---------
# No column in _COLUMN_TO_FACT_KEY maps to a financing fact today (pinned by
# TestFinancingPrecondition), so this arm is structurally a 0. Once a
# shares_outstanding / offering_proceeds column is added upstream, add its
# fact-index key here and the detector self-suppresses a grounded assertion.
_FINANCING_FACT_KEYS: frozenset[str] = frozenset()

# --- Brief TEXT column -> SCHEMA field (memo §6.3, mirror of measurement) -----
# The detector scans these four fields; catalyst_failure_exit is scanned too but
# its forward-conditional "exit if ... secondary offering" is suppressed by the
# hypothetical guard (a legit future exit trigger, not a present-tense assertion).
_TEXT_COLUMN_TO_SCHEMA_FIELD: dict[str, str] = {
    "brief_tldr": "tldr",
    "brief_supply_chain_md": "supply_chain_reasoning",
    "brief_bear_summary_md": "bear_summary",
    "brief_catalyst_failure_exit": "catalyst_failure_exit",
}

_TITLE_KEY = "source_event_title"


def _normalize(text: str) -> str:
    """Hyphen -> space so 'capital-raise' and 'capital raise' both match; a
    trailing lowercase pass is applied by the caller via re.IGNORECASE."""
    return text.replace("-", " ")


# --- Tier-1 lexicon: fire on an affirmative match (subject to guards) ---------
# (phrase, subtype). Phrases are matched whole-word / literal (re.escape + \b),
# reusing the faithfulness.py negation-cue compile idiom. The text is normalized
# (hyphen->space) before matching so hyphenated variants hit.
_TIER1: tuple[tuple[str, str], ...] = (
    ("capital raise", SUBTYPE_DILUTIVE),
    ("raise capital", SUBTYPE_DILUTIVE),
    ("raises capital", SUBTYPE_DILUTIVE),
    ("raising capital", SUBTYPE_DILUTIVE),
    ("equity raise", SUBTYPE_DILUTIVE),
    ("equity offering", SUBTYPE_DILUTIVE),
    ("secondary offering", SUBTYPE_DILUTIVE),
    ("follow on offering", SUBTYPE_DILUTIVE),  # follow-on normalized to follow on
    ("public offering", SUBTYPE_DILUTIVE),
    ("stock offering", SUBTYPE_DILUTIVE),
    ("at the money offering", SUBTYPE_DILUTIVE),  # rare; kept for completeness
    ("at the market offering", SUBTYPE_DILUTIVE),
    ("atm offering", SUBTYPE_DILUTIVE),
    ("share issuance", SUBTYPE_DILUTIVE),
    ("issue shares", SUBTYPE_DILUTIVE),
    ("issuing shares", SUBTYPE_DILUTIVE),
    ("issues shares", SUBTYPE_DILUTIVE),
    ("convertible note", SUBTYPE_DILUTIVE),
    ("convertible notes", SUBTYPE_DILUTIVE),
    ("convertible offering", SUBTYPE_DILUTIVE),
    ("convertible debt", SUBTYPE_DILUTIVE),
    ("convertible bond", SUBTYPE_DILUTIVE),
    ("convertible bonds", SUBTYPE_DILUTIVE),
    ("rights offering", SUBTYPE_DILUTIVE),
    ("private placement", SUBTYPE_DILUTIVE),
    ("pipe deal", SUBTYPE_DILUTIVE),
    ("dilution", SUBTYPE_DILUTIVE),
    ("dilutive", SUBTYPE_DILUTIVE),
    ("share repurchase", SUBTYPE_RETURN_OF_CAPITAL),
    ("stock repurchase", SUBTYPE_RETURN_OF_CAPITAL),
    ("share buyback", SUBTYPE_RETURN_OF_CAPITAL),
    ("stock buyback", SUBTYPE_RETURN_OF_CAPITAL),
    ("buyback", SUBTYPE_RETURN_OF_CAPITAL),
    ("repurchase program", SUBTYPE_RETURN_OF_CAPITAL),
    ("tender offer", SUBTYPE_RETURN_OF_CAPITAL),
)

# --- Tier-2 polysemous tokens: fire ONLY with a same-clause HARD anchor -------
# (token, subtype). A bare match over-fires ('product offering', 'raise
# guidance', 'raise prices', 'the theme proceeds'), so a hard financing anchor
# must co-occur in the SAME clause (the token's clause, back to the previous
# ./;/:/newline via the reused _clause_before).
_TIER2: tuple[tuple[str, str], ...] = (
    ("raise", SUBTYPE_DILUTIVE),
    ("raised", SUBTYPE_DILUTIVE),
    ("raising", SUBTYPE_DILUTIVE),
    ("offering", SUBTYPE_DILUTIVE),
    ("repurchase", SUBTYPE_RETURN_OF_CAPITAL),
    ("proceeds", SUBTYPE_DILUTIVE),
    ("issuance", SUBTYPE_DILUTIVE),
    ("placement", SUBTYPE_DILUTIVE),
)

# Hard financing anchors that must co-occur in a Tier-2 token's clause. A '$' +
# magnitude co-occurrence also anchors (a financing figure). 'dilut'/'financ' are
# stem anchors (dilution/dilutive, financing/finance) matched as substrings.
_TIER2_WORD_ANCHOR_RE = re.compile(
    r"\b(?:shares?|equity|stock|capital)\b|dilut|financ",
    re.IGNORECASE,
)
# A "$<number>" or "<number> million/billion/M/B" magnitude near the token.
_TIER2_MAGNITUDE_RE = re.compile(
    r"\$\s?\d|\b\d[\d,.]*\s?(?:million|billion|m|b|bn)\b",
    re.IGNORECASE,
)

# --- Guards ------------------------------------------------------------------
# Financing-specific hypothetical / forward-conditional cues. Kept SEPARATE from
# the shared _NEGATION_CUES (those flip a present-tense assertion; these flag a
# conditional/future one). A cue anywhere in the token's clause suppresses.
_HYPOTHETICAL_CUE_RES: tuple[re.Pattern, ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bif\b",
        r"\bshould\b",
        r"\bwere\b",
        r"\bcould\b",
        r"\bmay\s+need\s+to\b",
        r"\bmay\b",
        r"\brisk\s+of\b",
        r"\bpotential\b",
        r"\bwould\b",
        r"\bfuture\b",
    )
)

# Business-model / revenue-context cues (v1.1). A financing TOKEN inside one of
# these constructions is NOT a corporate financing EVENT of the subject company
# — it is the firm's revenue model or a third-party funding description. Two
# families:
#   1. the subject PROVIDES/lends/deploys capital to others (litigation finance,
#      BDCs, asset managers) — inverts the 'capital' anchor, so a co-located
#      Tier-2 'proceeds'/'raise' must not fire (post-deploy over-fire: ticker BUR
#      "provides capital to plaintiffs ... a portion of judgment proceeds").
#   2. '<noun> proceeds' where the noun is a recovery/sale, not an offering
#      (judgment / settlement / litigation / sale / asset / disposal / insurance).
# A genuine raise ("raise capital via a secondary offering") matches NEITHER.
_BUSINESS_CONTEXT_RES: tuple[re.Pattern, ...] = (
    re.compile(
        r"\b(?:provid(?:e|es|ed|ing)|lend(?:s|ing)?|lent|deploy(?:s|ed|ing)?"
        r"|advanc(?:e|es|ed|ing)|commit(?:s|ted|ting)?|inject(?:s|ed|ing)?)\s+capital\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:judg(?:e)?ment|settlement|litigation|sale|asset|disposal"
        r"|divestiture|insurance|liquidation|foreclosure)\s+proceeds\b",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class FinancingFlag:
    """One financing-claim detection (fired or suppressed) for a brief field."""

    field: str
    span: str  # ~120-char sentence window (reused _sentence_window)
    matched_phrase: str
    subtype: str  # DILUTIVE | RETURN_OF_CAPITAL
    kind: str = "financing"
    verdict: str = "FINANCING_FABRICATION"
    suppressed_by: str | None = None  # negation|hypothetical|quoted|title_escape|financing_fact


# ---------------------------------------------------------------------------
# Title-escape: subtype-matched financing event in the catalyst headline
# ---------------------------------------------------------------------------


def _title_subtypes(title: str) -> set[str]:
    """The financing subtypes NAMED in the catalyst title.

    A title that carries a ``$`` but names no financing verb (a revenue / TAM
    headline) yields an EMPTY set → no escape. A title naming a matching-subtype
    financing event (a dilutive offering, a buyback) yields that subtype so the
    escape can suppress a brief assertion of the SAME subtype.
    """
    if not title:
        return set()
    norm = _normalize(title)
    subtypes: set[str] = set()
    for phrase, subtype in _TIER1:
        if re.search(r"\b" + re.escape(phrase) + r"\b", norm, re.IGNORECASE):
            subtypes.add(subtype)
    # Tier-2 tokens also name a titled financing event when a hard anchor is in
    # the same title (e.g. "prices a $300M offering of common stock" -> equity
    # anchor). Scan the whole title as one clause (headlines are short).
    for token, subtype in _TIER2:
        if not re.search(r"\b" + re.escape(token) + r"\b", norm, re.IGNORECASE):
            continue
        if _TIER2_WORD_ANCHOR_RE.search(norm) or _TIER2_MAGNITUDE_RE.search(norm):
            subtypes.add(subtype)
    return subtypes


# ---------------------------------------------------------------------------
# Field scan
# ---------------------------------------------------------------------------


def _tier2_anchored(clause: str) -> bool:
    """True if the Tier-2 token's clause carries a hard financing anchor."""
    return bool(_TIER2_WORD_ANCHOR_RE.search(clause) or _TIER2_MAGNITUDE_RE.search(clause))


def _is_hypothetical(clause: str) -> bool:
    """True if a forward-conditional/hypothetical cue sits in the token's clause."""
    return any(cue_re.search(clause) for cue_re in _HYPOTHETICAL_CUE_RES)


def _is_business_context(clause: str) -> bool:
    """True if the token's clause is a revenue/business-model construction (the
    subject provides capital to others, or names recovery/sale proceeds) rather
    than a corporate financing EVENT of the subject company."""
    return any(cue_re.search(clause) for cue_re in _BUSINESS_CONTEXT_RES)


def _suppressor(
    text: str,
    norm_low: str,
    start: int,
    phrase_len: int,
    subtype: str,
    facts: Mapping[str, Any],
    title_subtypes: set[str],
) -> str | None:
    """The first applicable suppressor for a matched phrase, or None if it fires.

    Precedence: financing_fact (grounded) -> negation -> hypothetical ->
    business_context -> quoted -> title_escape. Grounding by a real fact and an
    explicit negation come first (the assertion is not fabricated at all);
    business_context means the token is not a financing EVENT at all (so it
    precedes the title escape, which only excuses an otherwise-ungrounded
    assertion the catalyst backs).
    """
    # Arm 1: a real financing fact grounds the assertion (empty today).
    if any(key in facts for key in _FINANCING_FACT_KEYS):
        return "financing_fact"
    # Reused clause-scoped negation (cue BEFORE the phrase in its clause).
    if _is_negated(norm_low, start):
        return "negation"
    # Financing-specific forward-conditional/hypothetical guard (whole clause).
    clause = _clause_before(norm_low, start + phrase_len)
    if _is_hypothetical(clause):
        return "hypothetical"
    # v1.1 revenue/business-model context (capital provider, recovery proceeds).
    if _is_business_context(clause):
        return "business_context"
    # Reused quotation guard (a cite of the guidance).
    if _is_quoted(text, start, phrase_len):
        return "quoted"
    # Arm 2: subtype-matched catalyst-title escape.
    if subtype in title_subtypes:
        return "title_escape"
    return None


def _scan_field(
    field: str, text: str, facts: Mapping[str, Any], title_subtypes: set[str]
) -> list[FinancingFlag]:
    """All financing flags (fired + suppressed) for one brief field's text."""
    if not text:
        return []
    norm = _normalize(text)
    norm_low = norm.lower()
    flags: list[FinancingFlag] = []
    # Character ranges already claimed by an emitted match. A Tier-2 token whose
    # span overlaps a claimed range is a DUPLICATE of a broader phrase already
    # counted ("raise" inside "capital raise", "offering" inside "secondary
    # offering") and is dropped so one assertion counts once.
    claimed: list[tuple[int, int]] = []

    def _overlaps(start: int, end: int) -> bool:
        return any(start < c_end and end > c_start for c_start, c_end in claimed)

    def _emit(start: int, phrase: str, subtype: str) -> None:
        end = start + len(phrase)
        claimed.append((start, end))
        # The span uses the RAW text window (not normalized) for readability.
        span = _sentence_window(text, text[start:end])
        suppressed = _suppressor(text, norm_low, start, len(phrase), subtype, facts, title_subtypes)
        flags.append(
            FinancingFlag(
                field=field,
                span=span,
                matched_phrase=phrase,
                subtype=subtype,
                suppressed_by=suppressed,
            )
        )

    # Tier-1: affirmative phrases (claim their ranges first so Tier-2 sub-tokens
    # of a Tier-1 phrase are absorbed, not double-counted).
    for phrase, subtype in _TIER1:
        for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", norm_low):
            if _overlaps(m.start(), m.end()):
                continue
            _emit(m.start(), phrase, subtype)

    # Tier-2: polysemous tokens with a same-clause hard anchor.
    for token, subtype in _TIER2:
        for m in re.finditer(r"\b" + re.escape(token) + r"\b", norm_low):
            start = m.start()
            end = start + len(token)
            if _overlaps(start, end):
                continue
            clause = _clause_before(norm_low, end)
            # clause_before ends AT the token; include a small window AFTER the
            # token too so "$500M offering" (anchor before) and "offering of
            # $500M" (anchor after) both anchor. Scan the sentence window.
            window = norm_low[max(0, start - 120) : end + 120]
            if not (_tier2_anchored(clause) or _tier2_anchored(window)):
                continue
            _emit(start, token, subtype)

    return _collapse_per_subtype(flags)


def _collapse_per_subtype(flags: list[FinancingFlag]) -> list[FinancingFlag]:
    """Collapse a field's flags to ONE per subtype (one financing ASSERTION per
    subtype per field). A field that mentions a raise twice ("the equity raise …
    will drive dilution") is one DILUTIVE assertion, not two. Prefers a FIRED
    representative over a suppressed one so a real fire is never hidden behind a
    suppressed duplicate; otherwise keeps the first-seen flag (deterministic)."""
    chosen: dict[str, FinancingFlag] = {}
    for flag in flags:
        existing = chosen.get(flag.subtype)
        if existing is None:
            chosen[flag.subtype] = flag
        elif existing.suppressed_by is not None and flag.suppressed_by is None:
            # Upgrade: a fired flag replaces a suppressed representative.
            chosen[flag.subtype] = flag
    # Preserve first-appearance order for stable output.
    seen: set[str] = set()
    out: list[FinancingFlag] = []
    for flag in flags:
        if flag.subtype in seen:
            continue
        seen.add(flag.subtype)
        out.append(chosen[flag.subtype])
    return out


def detect_financing_claims(row: Mapping[str, Any]) -> list[FinancingFlag]:
    """All financing flags (fired + suppressed) for one brief-parquet row.

    Reuses :func:`fact_index_from_brief_row` to build the row's fact index (arm-1
    grounding + the catalyst title) — never reimplements the adapter. A fired
    flag has ``suppressed_by is None``; a suppressed flag names its suppressor so
    over-suppression is visible on the audit worksheet.
    """
    facts = fact_index_from_brief_row(row)
    title = str(row.get(_TITLE_KEY) or facts.get(_TITLE_KEY) or "")
    title_subtypes = _title_subtypes(title)
    flags: list[FinancingFlag] = []
    for column, field in _TEXT_COLUMN_TO_SCHEMA_FIELD.items():
        text = row.get(column)
        if text is None:
            continue
        flags.extend(_scan_field(field, str(text), facts, title_subtypes))
    return flags


# ---------------------------------------------------------------------------
# Corpus report
# ---------------------------------------------------------------------------

_SCHEMA_FIELDS: tuple[str, ...] = (
    "tldr",
    "supply_chain_reasoning",
    "bear_summary",
    "catalyst_failure_exit",
)


def _brief_date_of_path_or_row(row: Mapping[str, Any]) -> str:
    """A brief-date label for a row, preferring a threaded-in filename stem.

    The live parquet has NO brief_date column, so :func:`measure_financing_fabrication`
    stamps ``brief_date`` from the parquet filename stem at load. This reads that
    stamp; if absent, falls back to the row's own year-month is NOT used here (the
    per-stratum year_month bucket already covers that) — it returns "" so the
    brief_date stratum only carries rows that actually have a date.
    """
    value = row.get("brief_date")
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in ("nan", "nat", "none", "<na>"):
        return ""
    return text


def _load_rows_with_brief_date(paths: Iterable[str]) -> list[dict]:
    """Read parquet paths into row dicts, stamping ``brief_date`` = the filename
    stem so a per-day pre/post stratum exists (the live parquet has no such
    column). Reuses the measurement loader indirectly via pandas."""
    import pandas as pd

    rows: list[dict] = []
    for path in paths:
        stem = Path(path).stem
        frame = pd.read_parquet(path)
        for record in frame.to_dict("records"):
            record.setdefault("brief_date", stem)
            rows.append(record)
    return rows


def measure_financing_fabrication(
    rows: Iterable[Mapping[str, Any]] | Iterable[str],
    *,
    strata_keys: tuple[str, ...] = ("brief_model_used",),
) -> dict:
    """Score a brief corpus for financing-fabrication and return a report.

    ``rows`` is an iterable of row Mappings OR parquet-path strings. Path strings
    are read with a ``brief_date`` stamp = the filename stem so a per-day
    pre/post-cutover stratum exists (the live parquet has no brief_date column).

    TELEMETRY ONLY (memo §2 / §9): joins no outcome ledger. DUAL-stamped with
    ``FAITHFULNESS_SCORER_VERSION`` (the reused row->fact-index adapter) AND
    ``FINANCING_DETECTOR_VERSION``. ``corpus_rate`` counts briefs with >=1 FIRED
    flag; ``total_suppressed_spans`` (by suppressor) keeps over-suppression
    visible.
    """
    raw = list(rows)
    if raw and isinstance(raw[0], str):
        row_maps: list[Mapping[str, Any]] = _load_rows_with_brief_date([str(p) for p in raw])  # type: ignore[assignment]
    else:
        row_maps = _coerce_rows(cast("list[Mapping[str, Any]]", raw))

    n = len(row_maps)
    detections: list[tuple[list[FinancingFlag], Mapping[str, Any]]] = [
        (detect_financing_claims(row), row) for row in row_maps
    ]

    def _fired(flags: list[FinancingFlag]) -> list[FinancingFlag]:
        return [f for f in flags if f.suppressed_by is None]

    k_briefs = sum(1 for flags, _ in detections if _fired(flags))
    total_fired = sum(len(_fired(flags)) for flags, _ in detections)
    total_suppressed = sum(
        1 for flags, _ in detections for f in flags if f.suppressed_by is not None
    )
    suppressed_by: dict[str, int] = {}
    for flags, _ in detections:
        for f in flags:
            if f.suppressed_by is not None:
                suppressed_by[f.suppressed_by] = suppressed_by.get(f.suppressed_by, 0) + 1

    # --- per-field fired-brief rates ---
    per_field: dict[str, dict] = {}
    for field in _SCHEMA_FIELDS:
        k = sum(1 for flags, _ in detections if any(f.field == field for f in _fired(flags)))
        per_field[field] = _rate_block(k, n)

    # --- per-stratum fired-brief rates (requested keys + brief_date + year_month) ---
    def _bucketed(label_of) -> dict:  # local helper
        buckets: dict[str, list[list[FinancingFlag]]] = {}
        for flags, row in detections:
            label = label_of(row)
            buckets.setdefault(label, []).append(flags)
        out: dict[str, dict] = {}
        for label, flag_lists in buckets.items():
            bn = len(flag_lists)
            bk = sum(1 for fl in flag_lists if _fired(fl))
            out[label] = _rate_block(bk, bn)
        return out

    per_stratum: dict[str, dict] = {}
    for key in strata_keys:
        per_stratum[key] = _bucketed(
            lambda row, key=key: (
                "unknown" if _is_missing_scalar(row.get(key)) else str(row.get(key))
            )
        )
    # brief_date stratum: only rows carrying a date (filename stem stamp or a
    # real column) fall into a dated bucket; undated rows go to "unknown".
    per_stratum["brief_date"] = _bucketed(lambda row: _brief_date_of_path_or_row(row) or "unknown")
    per_stratum["year_month"] = _bucketed(_year_month_of_row)

    return {
        "scorer_version": FAITHFULNESS_SCORER_VERSION,
        "financing_detector_version": FINANCING_DETECTOR_VERSION,
        "n_briefs": n,
        "corpus_rate": _rate_block(k_briefs, n),
        "per_field": per_field,
        "per_stratum": per_stratum,
        "total_fired_spans": total_fired,
        "total_suppressed_spans": total_suppressed,
        "suppressed_by": suppressed_by,
    }


def _is_missing_scalar(value: Any) -> bool:
    """True for a missing scalar (None / NaN / NaT / NA) — thin local guard so the
    stratifier does not import a private measurement helper it doesn't otherwise
    need."""
    if value is None:
        return True
    if isinstance(value, float):
        import math

        return math.isnan(value)
    try:
        import pandas as pd

        return pd.isna(value) is True
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Human audit worksheet
# ---------------------------------------------------------------------------

_BUCKET_ASSERTED = "asserted_financing"
_BUCKET_SUPPRESSED = "suppressed_sample"

_TEXT_COLUMN_BY_FIELD: dict[str, str] = {v: k for k, v in _TEXT_COLUMN_TO_SCHEMA_FIELD.items()}


def build_financing_audit_worksheet(
    rows: Iterable[Mapping[str, Any]] | Iterable[str],
    *,
    per_bucket: int = 25,
) -> list[dict]:
    """Stage a deterministic human-labeling worksheet for financing flags.

    Two buckets:

    * ``asserted_financing`` — the FIRED flags (the Perplexity-confirm candidates:
      revenue / buyback / TAM reframed as a raise vs a real raise).
    * ``suppressed_sample`` — a sample of SUPPRESSED flags, so a human can CHECK
      the guards are not over-suppressing (a real fabrication silenced by a wrong
      title escape / negation window).

    Each record carries ``ticker``, ``brief_date``, ``field``, ``span``,
    ``sentence_window``, ``source_event_title``, ``source_event_url``,
    ``matched_phrase``, ``subtype``, ``suppressed_by``, ``bucket``, and an empty
    ``human_label`` slot (NO invented labels). Ordering is DETERMINISTIC (sorted
    by ``(bucket, ticker, brief_date, field, span)`` and truncated to
    ``per_bucket`` per bucket); calling twice returns byte-identical records.
    """
    raw = list(rows)
    if raw and isinstance(raw[0], str):
        row_maps: list[Mapping[str, Any]] = _load_rows_with_brief_date([str(p) for p in raw])  # type: ignore[assignment]
    else:
        row_maps = _coerce_rows(cast("list[Mapping[str, Any]]", raw))

    staged: dict[str, list[dict]] = {_BUCKET_ASSERTED: [], _BUCKET_SUPPRESSED: []}
    for row in row_maps:
        flags = detect_financing_claims(row)
        if not flags:
            continue
        facts = fact_index_from_brief_row(row)
        ticker = str(row.get("ticker") or "")
        brief_date = _brief_date_of_path_or_row(row)
        title = str(row.get(_TITLE_KEY) or facts.get(_TITLE_KEY) or "")
        url = str(row.get("source_event_url") or "")
        for flag in flags:
            bucket = _BUCKET_ASSERTED if flag.suppressed_by is None else _BUCKET_SUPPRESSED
            text_column = _TEXT_COLUMN_BY_FIELD.get(flag.field)
            field_text = str(row.get(text_column) or "") if text_column else ""
            staged[bucket].append(
                {
                    "ticker": ticker,
                    "brief_date": brief_date,
                    "field": flag.field,
                    "span": flag.span,
                    "sentence_window": _sentence_window(field_text, flag.span)
                    if field_text
                    else flag.span,
                    "source_event_title": title,
                    "source_event_url": url,
                    "matched_phrase": flag.matched_phrase,
                    "subtype": flag.subtype,
                    "suppressed_by": flag.suppressed_by,
                    "bucket": bucket,
                    "human_label": "",
                }
            )

    records: list[dict] = []
    for bucket in (_BUCKET_ASSERTED, _BUCKET_SUPPRESSED):
        rows_for_bucket = sorted(
            staged[bucket],
            key=lambda r: (r["bucket"], r["ticker"], r["brief_date"], r["field"], r["span"]),
        )
        records.extend(rows_for_bucket[:per_bucket])
    return records


__all__ = [
    "FINANCING_DETECTOR_VERSION",
    "SUBTYPE_DILUTIVE",
    "SUBTYPE_RETURN_OF_CAPITAL",
    "FinancingFlag",
    "build_financing_audit_worksheet",
    "detect_financing_claims",
    "measure_financing_fabrication",
]
