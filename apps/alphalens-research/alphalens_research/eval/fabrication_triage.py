"""Fabrication-TRIAGE calibration harness — pure functions (telemetry only).

The T6 measurement harness (:mod:`alphalens_research.eval.measurement`) reports a
brief "fabrication rate" that is fidelity-to-the-``<facts>``-block, NOT
fidelity-to-truth. A number the model legitimately pulls from the catalyst
article (a contract size, a revenue figure) counts as FABRICATED only because it
is not in the injected ``<facts>``. This module CALIBRATES that rate: it triages
each FABRICATED numeric/date atom into a likely-SOURCE bucket — mostly
deterministically — and stages a small human worksheet for the ambiguous ones.

Supports the honest-limits framing of the LOCKED design memo
``docs/research/reading_quality_eval_design_2026_07_11.md`` §10.

Buckets (deterministic, precedence top-to-bottom — the FIRST that matches wins):

* ``in_catalyst_title`` — the atom's numeric digits appear in the row's
  ``source_event_title`` (normalize: strip ``$ , %`` and compare the digit
  string). ADAPTER COVERAGE GAP: groundable in principle, NOT a hallucination.
* ``near_miss_same_kind`` — a same-unit-kind fact exists within a WIDER relative
  band (``<= _NEAR_MISS_REL_BAND``) than the scorer's DISTORTED band, but was not
  matched. Likely derivation/rounding — a low-confidence fabrication.
* ``dollar_out_of_facts`` — a ``$``-magnitude atom (unit ``$`` OR an unglyphed
  magnitude word ``count`` like ``12 billion``) with no market_cap / insider
  match. Likely ARTICLE-DERIVED (contract / revenue / TAM), out-of-facts but
  plausibly true; needs the source to confirm.
* ``ungrounded_other`` — none of the above (bare ratios / ``%`` with no nearby
  fact and not in the title). STRONGEST hallucination candidate.

Public pure functions:

* :func:`triage_atom` — one FABRICATED atom + its row + fact index -> a bucket.
* :func:`triage_corpus` — rows or parquet paths -> a bucket-split report + the
  estimated true-hallucination band, stamped with ``FAITHFULNESS_SCORER_VERSION``.
* :func:`build_audit_worksheet` — the ambiguous buckets -> deterministic human
  -labeling records (empty ``human_label`` slot; NO invented labels).

Doctrine (memo §2 / §9): telemetry ONLY. Imports NOTHING from any outcome/return
ledger, touches NO selection/ordering surface, performs NO network I/O, and does
NOT reimplement scoring — it reuses the frozen scorer via
:func:`~alphalens_research.eval.measurement.score_row` /
:func:`~alphalens_research.eval.measurement.fact_index_from_brief_row`.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any, cast

# NOTE: this module depends on several PRIVATE scorer helpers below. That
# contract is pinned by tests/golden/test_fabrication_triage.py
# ::TestScorerHelperContract, which fails loudly in CI if any is renamed/dropped
# (kept private rather than promoted to public API per extract-on-2nd-use).
from alphalens_research.eval.faithfulness import (
    _ATOM_UNIT_TO_FACT_KINDS,
    _DIRECTIONAL_FACT_KEYS,
    _DISTORTED_REL_BAND,
    FAITHFULNESS_SCORER_VERSION,
    Atom,
    _atom_unit,
    _canonical_numeric_from_span,
    _fact_unit_kind,
    _numeric_fact_candidates,
)
from alphalens_research.eval.measurement import (
    _DATE_STRATUM_COLUMNS,
    _load_rows_from_parquet,
    fact_index_from_brief_row,
    score_row,
)

# --- Bucket names (precedence order top-to-bottom) ---------------------------
BUCKET_IN_CATALYST_TITLE = "in_catalyst_title"
BUCKET_NEAR_MISS_SAME_KIND = "near_miss_same_kind"
BUCKET_DOLLAR_OUT_OF_FACTS = "dollar_out_of_facts"
BUCKET_UNGROUNDED_OTHER = "ungrounded_other"

# Canonical bucket order — reports and worksheets iterate this so the output is
# stable and every bucket key is always present (even at count 0).
BUCKET_ORDER: tuple[str, ...] = (
    BUCKET_IN_CATALYST_TITLE,
    BUCKET_NEAR_MISS_SAME_KIND,
    BUCKET_DOLLAR_OUT_OF_FACTS,
    BUCKET_UNGROUNDED_OTHER,
)

# The ambiguous buckets that go on the human worksheet (memo §10: the ones a
# human must confirm against the source). in_catalyst_title is an adapter gap
# (mechanical, groundable) and near_miss is a rounding artifact — neither needs a
# source read, so only these two are staged.
_AMBIGUOUS_BUCKETS: tuple[str, ...] = (
    BUCKET_DOLLAR_OUT_OF_FACTS,
    BUCKET_UNGROUNDED_OTHER,
)

# Near-miss band: WIDER than the scorer's DISTORTED relative band. An atom that
# missed even DISTORTED but sits within this wider band of a same-kind fact is a
# likely derivation/rounding, not an invention. 0.75 per the task contract; kept
# strictly wider than _DISTORTED_REL_BAND so the two never overlap ambiguously.
_NEAR_MISS_REL_BAND = 0.75
assert _NEAR_MISS_REL_BAND > _DISTORTED_REL_BAND

# Text column carrying the catalyst headline used for the title-coverage check.
_TITLE_KEY = "source_event_title"

# Digit-run matcher: the significant-digit string of a numeric span, used for the
# title membership test after normalizing away $, commas and %.
_DIGITS_RE = re.compile(r"\d[\d.]*")

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bucket classification
# ---------------------------------------------------------------------------


def _digit_signature(span: str) -> str:
    """The normalized digit string of a numeric span for the title-membership
    test: strip ``$``, thousands commas, ``%`` and any sign, keep digits + a
    single decimal point, and drop a trailing ``.0`` / trailing ``.`` so
    ``$450`` and ``450`` and ``450.0`` all compare equal."""
    cleaned = span.replace(",", "").replace("$", "").replace("%", "").strip().lstrip("+-")
    m = _DIGITS_RE.search(cleaned)
    if not m:
        return ""
    digits = m.group(0).rstrip(".")
    if "." in digits:
        digits = digits.rstrip("0").rstrip(".")
    return digits


def _title_digit_signatures(title: str) -> set[str]:
    """The set of normalized digit signatures present in the catalyst title."""
    sigs: set[str] = set()
    for m in _DIGITS_RE.finditer(title.replace(",", "")):
        raw = m.group(0).rstrip(".")
        if "." in raw:
            raw = raw.rstrip("0").rstrip(".")
        if raw:
            sigs.add(raw)
    return sigs


def _is_in_catalyst_title(atom: Atom, title: str) -> bool:
    """True if the atom appears in the catalyst title.

    DATE atoms are matched on the FULL ``YYYY-MM-DD`` string, not the digit
    signature: ``_digit_signature`` stops at the ``-`` separator and collapses a
    date to its bare YEAR, so a year-only headline mention (headlines routinely
    name the current year) would otherwise route a genuine fabricated date into
    ``in_catalyst_title`` — the benign 'adapter gap' bucket — and bias the
    hallucination floor DOWNWARD. Requiring the whole date closes that leak. (A
    date whose FULL ISO string is in the title is already GROUNDED by the scorer,
    which scans the title free-text, so it never reaches triage as FABRICATED;
    this guard's job is purely to REJECT the year-only false positive.)

    NUMERIC atoms are matched on the unit-BLIND digit signature (strip ``$ , %``):
    ``12%`` and ``$12`` and ``12`` all compare equal on their digits. This can
    over-credit a coincidental same-digit headline number (a headcount ``12``
    matching a ``12%`` claim), so ``in_catalyst_title`` is an UPPER BOUND on the
    true adapter-gap share and the hallucination floor a lower bound. It never
    HIDES a fabrication (a coincidental match only re-labels one fabricated atom
    from a stronger bucket into the benign one); the mis-attribution is within
    the fabricated set only.
    """
    if not title:
        return False
    if atom.kind == "date":
        return atom.span in title
    sig = _digit_signature(atom.span)
    if not sig:
        return False
    return sig in _title_digit_signatures(title)


def _has_same_kind_fact_within_band(atom: Atom, facts: Mapping[str, Any], rel_band: float) -> bool:
    """True if a same-unit-kind numeric fact sits within ``rel_band`` relative of
    the atom's value. Mirrors the scorer's unit-awareness + directional sign
    strip so the near-miss test uses the SAME candidate set the scorer used."""
    value = _canonical_numeric_from_span(atom.span)
    if math.isnan(value):  # unparseable span — cannot near-miss
        return False
    allowed_kinds = _ATOM_UNIT_TO_FACT_KINDS.get(_atom_unit(atom), frozenset({"ratio"}))
    for key, fact_val in _numeric_fact_candidates(dict(facts)):
        if _fact_unit_kind(key) not in allowed_kinds:
            continue
        candidates = [fact_val]
        if key in _DIRECTIONAL_FACT_KEYS:
            candidates.append(abs(fact_val))
        for fv in candidates:
            if abs(value - fv) <= rel_band * max(abs(fv), 1e-9):
                return True
    return False


def triage_atom(atom: Atom, row: Mapping[str, Any], facts_index: Mapping[str, Any]) -> str:
    """Triage one FABRICATED numeric/date atom into a likely-source bucket.

    Precedence (first match wins): ``in_catalyst_title`` -> ``near_miss_same_kind``
    -> ``dollar_out_of_facts`` -> ``ungrounded_other``. ``facts_index`` is the
    row's fact index (from :func:`fact_index_from_brief_row`); ``row`` supplies
    the ``source_event_title`` text (the fact index also carries it, so either is
    an acceptable title source).
    """
    title = str(row.get(_TITLE_KEY) or facts_index.get(_TITLE_KEY) or "")

    # 1) In the catalyst title → adapter coverage gap (NOT a hallucination).
    if _is_in_catalyst_title(atom, title):
        return BUCKET_IN_CATALYST_TITLE

    # 2) Near-miss of a same-unit-kind fact within the WIDER band (derivation).
    #    A $-atom near a $-magnitude fact (market_cap / insider_score_usd) is a
    #    same-kind near-miss and is caught here — those facts are $-kind and a
    #    $-atom's allowed kinds are {$}, so this test already covers them with the
    #    SAME _NEAR_MISS_REL_BAND. A "count" magnitude word (e.g. "12 billion")
    #    allows both $ and ratio facts, so a count atom near either kind is also
    #    caught here before the article-derived route below.
    if _has_same_kind_fact_within_band(atom, facts_index, _NEAR_MISS_REL_BAND):
        return BUCKET_NEAR_MISS_SAME_KIND

    # 3) A $-magnitude atom with no matching fact → likely article-derived
    #    (contract / revenue / TAM). "count" (a bare magnitude word without the $
    #    glyph, e.g. "12 billion", "500 million") is treated the SAME as a glyphed
    #    $ figure: it is an out-of-facts magnitude, not a bare ratio, so it belongs
    #    with the article-derived bucket rather than the strongest-hallucination one.
    if _atom_unit(atom) in ("$", "count"):
        return BUCKET_DOLLAR_OUT_OF_FACTS

    # 4) Everything else (bare ratio / % with no nearby fact, not in the title).
    return BUCKET_UNGROUNDED_OTHER


# ---------------------------------------------------------------------------
# Row -> fabricated atoms (reuse the frozen scorer)
# ---------------------------------------------------------------------------


def _fabricated_atoms_of_row(row: Mapping[str, Any]) -> list[Atom]:
    """FABRICATED numeric/date atoms of one brief row via the frozen scorer.

    Reuses :func:`score_row` (which calls ``fact_index_from_brief_row`` +
    ``score_brief``) — this module NEVER reimplements scoring.
    """
    result = score_row(row)
    return [a for a in result.atoms if a.kind in ("numeric", "date") and a.verdict == "FABRICATED"]


def _coerce_rows(rows: Iterable[Mapping[str, Any]] | Iterable[str]) -> list[Mapping[str, Any]]:
    """Materialize rows: pass-through Mappings, or read parquet path strings via
    the measurement loader (reuse, do not duplicate)."""
    raw = list(rows)
    if raw and isinstance(raw[0], str):
        return cast("list[Mapping[str, Any]]", _load_rows_from_parquet(cast("list[str]", raw)))
    return cast("list[Mapping[str, Any]]", raw)


# ---------------------------------------------------------------------------
# Corpus triage report
# ---------------------------------------------------------------------------


def triage_corpus(rows: Iterable[Mapping[str, Any]] | Iterable[str]) -> dict:
    """Triage every FABRICATED numeric/date atom in a corpus into buckets.

    ``rows`` is an iterable of row Mappings OR parquet-path strings. Returns a
    telemetry-only report (memo §2 / §9), stamped with
    ``FAITHFULNESS_SCORER_VERSION``::

        {
          "scorer_version": str,
          "n_briefs": int,
          "n_briefs_with_fabrication": int,
          "total_fabricated_atoms": int,
          "buckets": {<bucket>: {"count": int, "share": float}},
          "estimated_true_hallucination_band": {
            "basis": str,                # "heuristic_source_guess_unvalidated"
            "floor_atoms": int,          # ungrounded_other only
            "ceiling_atoms": int,        # + dollar_out_of_facts + near_miss
            "floor_share_of_briefs": float,
            "ceiling_share_of_briefs": float,
          },
        }

    The band is the honest calibration (memo §10): the FLOOR counts only the
    strongest hallucination bucket; the CEILING adds the plausibly-article-derived
    and rounding buckets, which may or may not be true hallucinations.

    ``basis`` is stamped ``heuristic_source_guess_unvalidated`` so a downstream
    reader never quotes ``floor_share_of_briefs`` as a MEASURED rate: it is a
    heuristic residual (the bucket a source-less deterministic rule could not
    explain away), NOT a human-confirmed hallucination count. Human confirmation
    is the :func:`build_audit_worksheet` step.
    """
    row_maps = _coerce_rows(rows)
    n_briefs = len(row_maps)

    bucket_counts: dict[str, int] = dict.fromkeys(BUCKET_ORDER, 0)
    n_with_fab = 0
    for row in row_maps:
        fabricated = _fabricated_atoms_of_row(row)
        if fabricated:
            n_with_fab += 1
        facts = fact_index_from_brief_row(row)
        for atom in fabricated:
            bucket_counts[triage_atom(atom, row, facts)] += 1

    total = sum(bucket_counts.values())
    buckets = {
        name: {
            "count": bucket_counts[name],
            "share": (bucket_counts[name] / total) if total else 0.0,
        }
        for name in BUCKET_ORDER
    }

    floor = bucket_counts[BUCKET_UNGROUNDED_OTHER]
    ceiling = (
        bucket_counts[BUCKET_UNGROUNDED_OTHER]
        + bucket_counts[BUCKET_DOLLAR_OUT_OF_FACTS]
        + bucket_counts[BUCKET_NEAR_MISS_SAME_KIND]
    )
    band = {
        "basis": "heuristic_source_guess_unvalidated",
        "floor_atoms": floor,
        "ceiling_atoms": ceiling,
        "floor_share_of_briefs": (floor / n_briefs) if n_briefs else 0.0,
        "ceiling_share_of_briefs": (ceiling / n_briefs) if n_briefs else 0.0,
    }

    return {
        "scorer_version": FAITHFULNESS_SCORER_VERSION,
        "n_briefs": n_briefs,
        "n_briefs_with_fabrication": n_with_fab,
        "total_fabricated_atoms": total,
        "buckets": buckets,
        "estimated_true_hallucination_band": band,
    }


# ---------------------------------------------------------------------------
# Human audit worksheet (ambiguous buckets only)
# ---------------------------------------------------------------------------

_SENTENCE_WINDOW_HALF = 60  # ~120-char window centred on the span

# Column candidates for a brief-date-ish label on the worksheet record. The live
# brief parquet has NO brief_date column (the date is the filename), so this
# falls back through the same date-ish columns the measurement stratifier uses.
_BRIEF_DATE_COLUMNS: tuple[str, ...] = _DATE_STRATUM_COLUMNS

# The brief text columns mapped to their SCHEMA field name (mirror of the
# measurement mapping — the worksheet needs the raw text to build the window).
_TEXT_COLUMN_TO_SCHEMA_FIELD: dict[str, str] = {
    "brief_tldr": "tldr",
    "brief_supply_chain_md": "supply_chain_reasoning",
    "brief_bear_summary_md": "bear_summary",
    "brief_catalyst_failure_exit": "catalyst_failure_exit",
}
_SCHEMA_FIELD_TO_TEXT_COLUMN: dict[str, str] = {
    v: k for k, v in _TEXT_COLUMN_TO_SCHEMA_FIELD.items()
}


def _brief_date_of_row(row: Mapping[str, Any]) -> str:
    """A brief-date-ish label from the first available date-ish column ("" if
    none). Deterministic — never uses wall-clock time."""
    for column in _BRIEF_DATE_COLUMNS:
        value = row.get(column)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in ("nan", "nat", "none", "<na>"):
            return text
    return ""


def _sentence_window(text: str, span: str) -> str:
    """A ~120-char window of ``text`` centred on the first occurrence of ``span``.

    Falls back to the head of the field when the span is not found (should not
    happen — the atom span comes from this text — but keeps the record complete).
    """
    idx = text.find(span)
    if idx < 0:
        return text[: 2 * _SENTENCE_WINDOW_HALF].strip()
    lo = max(0, idx - _SENTENCE_WINDOW_HALF)
    hi = min(len(text), idx + len(span) + _SENTENCE_WINDOW_HALF)
    return text[lo:hi].strip()


def _stage_ambiguous_atoms(row: Mapping[str, Any], staged: dict[str, list[dict]]) -> None:
    """Append every AMBIGUOUS-bucket fabricated atom of ``row`` to ``staged``."""
    fabricated = _fabricated_atoms_of_row(row)
    if not fabricated:
        return
    facts = fact_index_from_brief_row(row)
    ticker = str(row.get("ticker") or "")
    brief_date = _brief_date_of_row(row)
    title = str(row.get(_TITLE_KEY) or facts.get(_TITLE_KEY) or "")
    url = str(row.get("source_event_url") or "")
    for atom in fabricated:
        bucket = triage_atom(atom, row, facts)
        if bucket not in _AMBIGUOUS_BUCKETS:
            continue
        text_column = _SCHEMA_FIELD_TO_TEXT_COLUMN.get(atom.field)
        field_text = str(row.get(text_column) or "") if text_column else ""
        staged[bucket].append(
            {
                "ticker": ticker,
                "brief_date": brief_date,
                "field": atom.field,
                "span": atom.span,
                "sentence_window": _sentence_window(field_text, atom.span),
                "source_event_title": title,
                "source_event_url": url,
                "bucket": bucket,
                "human_label": "",
            }
        )


def build_audit_worksheet(
    rows: Iterable[Mapping[str, Any]] | Iterable[str],
    *,
    per_bucket: int = 15,
) -> list[dict]:
    """Stage a deterministic human-labeling worksheet for the AMBIGUOUS buckets.

    Only ``dollar_out_of_facts`` + ``ungrounded_other`` atoms are staged (the
    ones a human must confirm against the source, memo §10). Each record carries:
    ``ticker``, ``brief_date``, ``field``, ``span``, a ~120-char
    ``sentence_window`` around the span, ``source_event_title``,
    ``source_event_url``, ``bucket``, and an empty ``human_label`` slot (NO
    invented labels).

    Ordering is DETERMINISTIC (no ``random`` / wall-clock): records are sorted by
    ``(bucket, ticker, brief_date, field, span)`` and truncated to ``per_bucket``
    per bucket via a stable fixed shuffle key (the sort itself). Calling twice on
    the same rows returns byte-identical records.
    """
    row_maps = _coerce_rows(rows)

    staged: dict[str, list[dict]] = {name: [] for name in _AMBIGUOUS_BUCKETS}
    for row in row_maps:
        _stage_ambiguous_atoms(row, staged)

    records: list[dict] = []
    for bucket in _AMBIGUOUS_BUCKETS:
        rows_for_bucket = sorted(
            staged[bucket],
            key=lambda r: (r["bucket"], r["ticker"], r["brief_date"], r["field"], r["span"]),
        )
        dropped = len(rows_for_bucket) - min(len(rows_for_bucket), per_bucket)
        if dropped:
            # Surface the cap so the worksheet is never mistaken for the full
            # ambiguous population (no silent truncation).
            _LOGGER.debug("worksheet bucket %s: kept %d, dropped %d", bucket, per_bucket, dropped)
        records.extend(rows_for_bucket[:per_bucket])
    return records


__all__ = [
    "BUCKET_DOLLAR_OUT_OF_FACTS",
    "BUCKET_IN_CATALYST_TITLE",
    "BUCKET_NEAR_MISS_SAME_KIND",
    "BUCKET_ORDER",
    "BUCKET_UNGROUNDED_OTHER",
    "build_audit_worksheet",
    "triage_atom",
    "triage_corpus",
]
