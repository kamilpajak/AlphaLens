"""T6 measurement harness (research, NON-GATING) — corpus-scale faithfulness.

Implements the LOCKED design memo
``docs/research/reading_quality_eval_design_2026_07_11.md`` §6.7 ("Measurement
harness (research, non-gating)"), §5 (metrics + Wilson CI), §6.6, §10.

The GATING pilot (:mod:`alphalens_research.eval.faithfulness`) scores the 4
frozen golden CASSETTES. This module runs the SAME :func:`score_brief` over the
real brief PARQUET corpus (``~/.alphalens/thematic_briefs/*.parquet``) to produce
distributional rates with Wilson confidence intervals — the memo's second
artifact, mirroring the live-probes "gating hermetic + non-gating measurement"
split.

**Fact index from row columns, not the rendered ``<facts>`` string.** The parquet
persists ``brief_template_facts_json`` as NULL on ~14/15 rows (the rendered
``<facts>`` string is not stored), so :func:`fact_index_from_brief_row` builds the
normalized fact index directly from the typed row COLUMNS via an explicit column
-> fact-index-key mapping (:data:`_COLUMN_TO_FACT_KEY`), applying the two ROIC
renames (``buffett_roic_latest`` -> ``buffett_roic_pct`` etc.) so
:func:`~alphalens_research.eval.faithfulness._fact_unit_kind` classifies them as
``%`` facts (the values ARE percentages).

**Doctrine (memo §2, §9): telemetry only.** This module imports NOTHING from any
outcome/return ledger, touches NO selection/ordering surface, and performs NO
network I/O — pure functions over in-memory rows. Every report is stamped with
:data:`~alphalens_research.eval.faithfulness.FAITHFULNESS_SCORER_VERSION` so
rates are never pooled across a scorer/lexicon change.
"""

from __future__ import annotations

import glob
import math
import os
from collections.abc import Iterable, Mapping
from typing import Any

from alphalens_research.eval.faithfulness import (
    FAITHFULNESS_SCORER_VERSION,
    FaithfulnessResult,
    score_brief,
)

# --- Column -> fact-index key mapping (memo §6.7 / task contract) ------------
# Each brief-parquet COLUMN maps to a fact-index KEY. NaN/None values are dropped
# by the builder. Key names carry the unit signal that
# faithfulness._fact_unit_kind reads (``*_pct`` / ``*_usd`` / ``market_cap`` /
# bare-ratio), so a numeric atom only grounds against a unit-compatible fact.
#
# The two ROIC columns are RENAMED to a ``*_pct`` key: the stored values are
# percentages (e.g. 18.58), so without the rename _fact_unit_kind would classify
# them as bare ratios and a brief citing "ROIC 18.6%" would false-fire FABRICATED.
#
# ``valuation_fcf_margin`` is the sole FRACTION column (corpus range -3.44..1.44,
# median 0.152) — every other numeric column is already a true percentage, a
# ratio, or dollars. Briefs cite it in BOTH styles: the FRACTION ("FCF margin
# 0.34", the golden-fixture form) AND the PERCENT ("34% FCF margin"). A bare-
# decimal atom only grounds against a ``ratio`` fact; a ``%`` atom only grounds
# against a ``%`` fact. So the column is DUAL-EMITTED (see
# :data:`_FRACTION_TO_PERCENT_DUAL_KEY` + :func:`fact_index_from_brief_row`): the
# raw fraction under ``valuation_fcf_margin`` (ratio) AND a ×100-scaled copy under
# ``valuation_fcf_margin_pct`` (%). A plain rename to ``*_pct`` would ground the
# percent style but BREAK the fraction style (false-fire FABRICATED on "0.34");
# dual-emit grounds both and the unit-aware matcher keeps them from cross-matching.
_COLUMN_TO_FACT_KEY: dict[str, str] = {
    "market_cap": "market_cap",
    "valuation_pe": "valuation_pe",
    "valuation_ps": "valuation_ps",
    "valuation_ev_rev": "valuation_ev_rev",
    "valuation_ev_ebitda": "valuation_ev_ebitda",
    # --- fraction, kept as a ratio fact; a %-scaled copy is dual-emitted below ---
    "valuation_fcf_margin": "valuation_fcf_margin",
    "fcff_yield_pct": "fcff_yield_pct",
    "insider_score_usd": "insider_score_usd",
    "technical_rsi": "technical_rsi",
    "technical_ma50_distance_pct": "technical_ma50_distance_pct",
    "technical_atr_pct": "technical_atr_pct",
    "technical_volume_zscore": "technical_volume_zscore",
    "technical_pct_off_52w_high": "technical_pct_off_52w_high",
    "technical_pct_off_52w_low": "technical_pct_off_52w_low",
    "technical_ma200_distance_pct": "technical_ma200_distance_pct",
    "technical_ma200_slope_pct_per_day": "technical_ma200_slope_pct_per_day",
    "buffett_owner_earnings_yield_pct": "buffett_owner_earnings_yield_pct",
    # --- ROIC renames: value IS a percentage, so key must carry _pct ---
    "buffett_roic_latest": "buffett_roic_pct",
    "buffett_roic_3y_avg": "buffett_roic_3y_avg_pct",
    "buffett_margin_of_safety_pct": "buffett_margin_of_safety_pct",
    "next_earnings_date": "next_earnings_date",
    "source_event_title": "source_event_title",
    "source_event_published_at": "source_event_published_at",
}

# DUAL-emit map: SOURCE column storing a FRACTION -> the extra ``*_pct`` fact key
# whose value is the fraction ×100. The raw fraction stays under its own ratio
# key (see :data:`_COLUMN_TO_FACT_KEY`); this adds a percent copy so a brief
# citing "34%" grounds too. Currently the single ``valuation_fcf_margin`` column.
_FRACTION_TO_PERCENT_DUAL_KEY: dict[str, str] = {
    "valuation_fcf_margin": "valuation_fcf_margin_pct",
}
_FRACTION_TO_PERCENT_SCALE = 100.0

# Brief TEXT column -> SCHEMA field name that score_brief expects (memo §6.3).
_TEXT_COLUMN_TO_SCHEMA_FIELD: dict[str, str] = {
    "brief_tldr": "tldr",
    "brief_supply_chain_md": "supply_chain_reasoning",
    "brief_bear_summary_md": "bear_summary",
    "brief_catalyst_failure_exit": "catalyst_failure_exit",
}

_SCHEMA_FIELDS: tuple[str, ...] = (
    "tldr",
    "supply_chain_reasoning",
    "bear_summary",
    "catalyst_failure_exit",
)

# Row columns scanned to derive a year-month stratum (memo §9 poolability).
_DATE_STRATUM_COLUMNS: tuple[str, ...] = (
    "brief_date",
    "date",
    "asof",
    "brief_generated_at",
    "source_event_published_at",
)

# 95% two-sided Wilson interval z-score.
_WILSON_Z_95 = 1.959963984540054


# ---------------------------------------------------------------------------
# NaN / value helpers
# ---------------------------------------------------------------------------


def _is_missing(value: Any) -> bool:
    """True for a missing scalar cell: ``None`` / float ``NaN`` / pandas ``NaT`` / ``NA``.

    ``pandas.isna`` is the single scalar predicate — it recognises ``None``,
    ``float('nan')``, ``pd.NaT`` (nullable/datetime columns) and ``pd.NA`` alike.
    The current live parquets store date columns as ``str`` (missing -> float
    NaN, already caught), but ``brief_generated_at`` is a real ``datetime64`` and
    ``next_earnings_date`` is ``object`` — a future typed date column would yield
    ``pd.NaT`` for a missing cell, which the old float-only check let leak into the
    fact index as the literal string ``"NaT"``. Guarded so a non-scalar value
    (``pd.isna`` returns an array) can never raise / return an array.
    """
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    try:
        import pandas as pd

        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return result is True


def _clean_text(value: Any) -> str:
    """A brief text cell coerced to a plain string ("" when missing)."""
    if _is_missing(value):
        return ""
    return str(value)


# ---------------------------------------------------------------------------
# Row -> fact index / brief fields
# ---------------------------------------------------------------------------


def fact_index_from_brief_row(row: Mapping[str, Any]) -> dict:
    """Build a normalized fact index from one brief-parquet ROW's columns.

    Applies :data:`_COLUMN_TO_FACT_KEY` (including the two ROIC ``*_pct``
    renames), dual-emits the fraction column(s) in
    :data:`_FRACTION_TO_PERCENT_DUAL_KEY` as an extra ×100 percent fact
    (``0.31 -> 31.0`` under ``*_pct``, raw fraction kept), drops NaN/None/NaT
    cells, and ignores any column not in the mapping. Numeric cells keep their
    native type; the text-grounding columns (``source_event_title`` /
    ``source_event_published_at`` / ``next_earnings_date``) are carried as
    strings. Returns the same index shape :func:`score_brief` expects (memo §6.4
    typed-source index).
    """
    index: dict = {}
    for column, key in _COLUMN_TO_FACT_KEY.items():
        if column not in row:
            continue
        value = row[column]
        if _is_missing(value):
            continue
        if column in ("source_event_title", "source_event_published_at", "next_earnings_date"):
            index[key] = str(value)
        else:
            index[key] = value
            pct_key = _FRACTION_TO_PERCENT_DUAL_KEY.get(column)
            if pct_key is not None:
                index[pct_key] = float(value) * _FRACTION_TO_PERCENT_SCALE
    return index


def brief_fields_from_row(row: Mapping[str, Any]) -> dict:
    """Map the 4 ``brief_*`` text columns to the SCHEMA field names.

    Missing / NaN text cells become ``""`` so :func:`score_brief` skips them
    (memo §6.3 field names)."""
    return {
        field: _clean_text(row.get(column))
        for column, field in _TEXT_COLUMN_TO_SCHEMA_FIELD.items()
    }


def score_row(row: Mapping[str, Any]) -> FaithfulnessResult:
    """Score one brief-parquet row: fact index + brief fields -> ``score_brief``."""
    facts_index = fact_index_from_brief_row(row)
    brief_fields = brief_fields_from_row(row)
    return score_brief(facts_index, brief_fields)


# ---------------------------------------------------------------------------
# Wilson interval (pure python, no scipy dependency, memo §5)
# ---------------------------------------------------------------------------


def wilson_interval(k: int, n: int, z: float = _WILSON_Z_95) -> tuple[float, float]:
    """Wilson score 95% confidence interval for a binomial proportion.

    Pure-python (no scipy) per the memo's "small N" small-dependency preference.
    ``(nan, nan)`` when ``n == 0`` (an empty stratum has no rate to bound).
    """
    if n <= 0:
        return (float("nan"), float("nan"))
    phat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(phat * (1.0 - phat) / n + z2 / (4 * n * n))
    lo = center - margin
    hi = center + margin
    return (max(0.0, lo), min(1.0, hi))


# ---------------------------------------------------------------------------
# Corpus report
# ---------------------------------------------------------------------------


def _year_month_of_row(row: Mapping[str, Any]) -> str:
    """A ``YYYY-MM`` stratum label from the first available date-ish column.

    Falls back to ``"unknown"`` when no column yields a parseable ``YYYY-MM``.
    """
    for column in _DATE_STRATUM_COLUMNS:
        value = row.get(column)
        if _is_missing(value):
            continue
        text = str(value).strip()
        # Accept YYYY-MM at the head (ISO dates, timestamps, "2026-05-24T...").
        if len(text) >= 7 and text[:4].isdigit() and text[4] == "-" and text[5:7].isdigit():
            return text[:7]
    return "unknown"


def _load_rows_from_parquet(parquet_paths: Iterable[str]) -> list[dict]:
    """Read parquet files into row dicts (import pandas lazily — pure-fn module)."""
    import pandas as pd

    rows: list[dict] = []
    for path in parquet_paths:
        frame = pd.read_parquet(path)
        rows.extend(frame.to_dict("records"))
    return rows


def _rate_block(k: int, n: int) -> dict:
    """A rate + Wilson CI + raw counts block (memo §5: report counts, not just rates)."""
    lo, hi = wilson_interval(k, n)
    return {
        "rate": (k / n) if n else float("nan"),
        "ci_low": lo,
        "ci_high": hi,
        "k": k,
        "n": n,
    }


def _stratum_rates(results: list[tuple[FaithfulnessResult, str]]) -> dict:
    """any_fabricated / any_violation rate blocks for a list of (result, _) pairs."""
    n = len(results)
    k_fab = sum(1 for r, _ in results if r.fabricated_numeric_date_atoms >= 1)
    k_vio = sum(1 for r, _ in results if r.characterization_violations >= 1)
    return {
        "any_fabricated": _rate_block(k_fab, n),
        "any_violation": _rate_block(k_vio, n),
    }


def measure_corpus(
    rows: Iterable[Mapping[str, Any]] | Iterable[str],
    *,
    strata_keys: tuple[str, ...] = ("brief_model_used",),
) -> dict:
    """Score a brief corpus and return a structured, poolability-stamped report.

    ``rows`` is either an iterable of row Mappings (dicts / pandas records) OR an
    iterable of parquet-file path strings — path strings are read into rows via
    pandas (lazy import). The report is TELEMETRY ONLY (memo §2/§9): it joins no
    outcome ledger and is stamped with ``FAITHFULNESS_SCORER_VERSION``.

    Report shape::

        {
          "scorer_version": str,
          "n_briefs": int,
          "corpus_rates": {                       # fraction of briefs with >=1 ...
            "any_fabricated": {rate, ci_low, ci_high, k, n},
            "any_violation":  {rate, ci_low, ci_high, k, n},
          },
          "per_brief_counts": {                   # totals across the corpus
            "fabricated_numeric_date_atoms": int,
            "characterization_violations": int,
            "distorted_atoms": int,
            "deferred_entity_atoms": int,         # memo §6.6 (0 in v1)
          },
          "diagnostic_means": {                   # memo §6.6: paired, never headline
            "groundedness_rate": float | None,    # green-while-broken, paired below
            "checkable_coverage": float | None,
            "checkable_atoms_per_brief": float,
            "out_of_scope_atoms_per_brief": float,
          },
          "per_field_fabrication": {              # expect supply_chain highest
            <schema_field>: {"fabricated_atoms": int, "distorted_atoms": int},
          },
          "per_stratum": {
            <stratum_key>: {<value>: {any_fabricated, any_violation}},
            "year_month":  {<YYYY-MM>: {any_fabricated, any_violation}},
          },
        }
    """
    materialized = list(rows)
    if materialized and isinstance(materialized[0], str):
        materialized = _load_rows_from_parquet(materialized)  # type: ignore[arg-type]

    # Score every row once; keep the row alongside for stratification.
    scored: list[tuple[FaithfulnessResult, Mapping[str, Any]]] = [
        (score_row(row), row)
        for row in materialized  # type: ignore[arg-type]
    ]
    n = len(scored)

    # --- corpus-level rates (fraction of briefs with >=1 hit) + Wilson CI ---
    k_fab = sum(1 for r, _ in scored if r.fabricated_numeric_date_atoms >= 1)
    k_vio = sum(1 for r, _ in scored if r.characterization_violations >= 1)
    corpus_rates = {
        "any_fabricated": _rate_block(k_fab, n),
        "any_violation": _rate_block(k_vio, n),
    }

    # --- corpus-wide atom totals ---
    per_brief_counts = {
        "fabricated_numeric_date_atoms": sum(r.fabricated_numeric_date_atoms for r, _ in scored),
        "characterization_violations": sum(r.characterization_violations for r, _ in scored),
        "distorted_atoms": sum(r.distorted_atoms for r, _ in scored),
        # memo §6.6 secondary metric. STRUCTURALLY 0 in v1 (entity/product atoms
        # are only routed to DEFERRED in Phase-2), emitted so the report is a
        # complete superset of the memo's secondary metrics and Phase-2
        # activation is visible without a report-shape change.
        "deferred_entity_atoms": sum(r.deferred_entity_atoms for r, _ in scored),
    }

    # --- diagnostic means (groundedness paired with coverage, memo §6.6) ---
    grounded = [r.groundedness_rate for r, _ in scored if r.groundedness_rate is not None]
    coverage = [r.checkable_coverage for r, _ in scored if r.checkable_coverage is not None]
    diagnostic_means = {
        "groundedness_rate": (sum(grounded) / len(grounded)) if grounded else None,
        "checkable_coverage": (sum(coverage) / len(coverage)) if coverage else None,
        "checkable_atoms_per_brief": (sum(r.checkable_atoms for r, _ in scored) / n) if n else 0.0,
        "out_of_scope_atoms_per_brief": (sum(r.out_of_scope_atoms for r, _ in scored) / n)
        if n
        else 0.0,
    }

    # --- per-field fabrication + distortion breakdown ---
    per_field_fabrication: dict[str, dict[str, int]] = {
        field: {"fabricated_atoms": 0, "distorted_atoms": 0} for field in _SCHEMA_FIELDS
    }
    for result, _ in scored:
        for atom in result.atoms:
            if atom.field not in per_field_fabrication:
                continue
            if atom.kind in ("numeric", "date") and atom.verdict == "FABRICATED":
                per_field_fabrication[atom.field]["fabricated_atoms"] += 1
            elif atom.kind == "numeric" and atom.verdict == "DISTORTED":
                per_field_fabrication[atom.field]["distorted_atoms"] += 1

    # --- per-stratum rates (by requested keys + always year-month) ---
    per_stratum: dict[str, dict] = {}
    for key in strata_keys:
        buckets: dict[str, list[tuple[FaithfulnessResult, str]]] = {}
        for result, row in scored:
            raw = row.get(key)
            label = "unknown" if _is_missing(raw) else str(raw)
            buckets.setdefault(label, []).append((result, label))
        per_stratum[key] = {label: _stratum_rates(pairs) for label, pairs in buckets.items()}

    ym_buckets: dict[str, list[tuple[FaithfulnessResult, str]]] = {}
    for result, row in scored:
        label = _year_month_of_row(row)
        ym_buckets.setdefault(label, []).append((result, label))
    per_stratum["year_month"] = {
        label: _stratum_rates(pairs) for label, pairs in ym_buckets.items()
    }

    return {
        "scorer_version": FAITHFULNESS_SCORER_VERSION,
        "n_briefs": n,
        "corpus_rates": corpus_rates,
        "per_brief_counts": per_brief_counts,
        "diagnostic_means": diagnostic_means,
        "per_field_fabrication": per_field_fabrication,
        "per_stratum": per_stratum,
    }


def default_corpus_paths(root: str | None = None) -> list[str]:
    """Sorted parquet paths of the live brief corpus (``~/.alphalens/thematic_briefs``).

    Convenience for the real-measurement deliverable; NOT used by the pure-fn
    scoring path. ``root`` overrides the default cache dir (for tests)."""
    base = root or os.path.expanduser("~/.alphalens/thematic_briefs")
    return sorted(glob.glob(os.path.join(base, "*.parquet")))


__all__ = [
    "brief_fields_from_row",
    "default_corpus_paths",
    "fact_index_from_brief_row",
    "measure_corpus",
    "score_row",
    "wilson_interval",
]
