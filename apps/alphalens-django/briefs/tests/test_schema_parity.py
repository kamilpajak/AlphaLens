"""Schema parity gate against the frozen legacy contract.

The legacy ``alphalens/api/schema.py`` ``CANDIDATE_COLUMNS`` tuple was
removed in F8 of the Django migration (ADR 0009). To preserve the
"a future model change can't silently break the contract" guarantee
that test originally provided, the column list is inlined here as a
**frozen baseline**. Every entry below is either:

* a field on ``Brief`` (1:1 carry-over from the legacy contract), OR
* in ``INTENTIONALLY_DROPPED`` below — denormalisations the greenfield
  model replaces with computed serializer output (or in frontend code).

To extend the contract intentionally: add the column to ``Brief``,
add the name to ``LEGACY_CONTRACT_COLUMNS``, and refresh the OpenAPI
parity snapshot. If you only do one of those steps this test fails.
"""

from __future__ import annotations

from briefs.models import Brief

# Snapshot of the legacy ``CANDIDATE_COLUMN_NAMES`` tuple at the F8
# decommission point. Frozen; do not edit without bumping
# ``docs/openapi-parity/legacy.json`` alongside.
LEGACY_CONTRACT_COLUMNS: tuple[str, ...] = (
    "theme",
    "ticker",
    "company_name",
    "rationale",
    "gemini_confidence",
    "market_cap",
    "gates_passed",
    "gates_passed_str",
    "n_gates_passed",
    "gates_failed",
    "gates_failed_str",
    "n_gates_failed",
    "gates_unknown",
    "gates_unknown_str",
    "n_gates_unknown",
    "verified",
    "source_event_url",
    "source_event_title",
    "source_event_published_at",
    "theme_search_keywords",
    "industry_id",
    "industry_name",
    "sector_name",
    # Issue #197 (post-legacy extension): cohort-resolution level surfaced
    # so the UI can swap the percentile bar for a thin-cohort badge.
    "peer_cohort_level",
    "insider_score_usd",
    "insider_score_sector_percentile",
    "fcff_yield_pct",
    "fcff_yield_sector_percentile",
    "valuation_pe",
    "valuation_ps",
    "valuation_ev_rev",
    "valuation_ev_ebitda",
    "valuation_fcf_margin",
    "valuation_composite_sector_percentile",
    "valuation_financials_publish_date",
    "valuation_financials_age_days",
    "roic_pct",
    "roe_pct",
    "magic_formula_health_pass",
    "technical_rsi",
    "technical_ma50_distance_pct",
    "technical_atr_pct",
    "technical_volume_zscore",
    "technical_pct_off_52w_high",
    "technical_pct_off_52w_low",
    "technical_ma200_distance_pct",
    "technical_ma200_slope_pct_per_day",
    "technicals_summary_str",
    "catalyst_strength",
    "catalyst_event_type",
    "catalyst_confidence",
    "magic_formula_rank",
    "magic_formula_cohort_n",
    "deep_drawdown_reversal",
    "layer4_weighted_score",
    "also_in_themes",
    "rank_in_day",
    "cohort_size_in_day",
    "next_earnings_date",
    "brief_model_used",
    "brief_tldr",
    "brief_supply_chain_md",
    "brief_bear_summary_md",
    "brief_catalyst_failure_exit",
    "brief_entry_price_note",
    "brief_position_pct",
    "brief_time_exit_weeks",
    "brief_time_exit_on_catalyst_failure_weeks",
    "brief_disaster_stop_pct",
    "brief_generated_at",
)

# Denormalised concat columns from the legacy SQLite cache. The list[str]
# canonical columns (gates_passed, gates_failed, gates_unknown, also_in_themes,
# theme_search_keywords) are kept as JSONField; the joined-string sibling
# columns are recomputed in DRF serializers rather than stored.
INTENTIONALLY_DROPPED: frozenset[str] = frozenset(
    {
        "gates_passed_str",
        "gates_failed_str",
        "gates_unknown_str",
        "technicals_summary_str",
    }
)


def _brief_field_names() -> frozenset[str]:
    return frozenset(f.name for f in Brief._meta.get_fields())


def test_every_contract_column_is_modeled_or_dropped() -> None:
    model_fields = _brief_field_names()
    missing: list[str] = []
    for col in LEGACY_CONTRACT_COLUMNS:
        if col in INTENTIONALLY_DROPPED:
            continue
        if col not in model_fields:
            missing.append(col)
    assert missing == [], (
        f"Legacy-contract columns missing from Brief model: {missing}. "
        "Either add a field or add the name to INTENTIONALLY_DROPPED."
    )


def test_no_orphan_brief_fields() -> None:
    """Every non-trivial Brief field exists in the frozen contract.

    Excludes the composite-pk descriptor and the explicit ``date`` column
    which is a brief-level grouping key (not a candidate-row attribute in
    the contract).
    """
    contract = frozenset(LEGACY_CONTRACT_COLUMNS) | INTENTIONALLY_DROPPED
    structural_exempt = frozenset({"pk", "date"})
    orphans = [
        f.name
        for f in Brief._meta.get_fields()
        if f.name not in contract and f.name not in structural_exempt
    ]
    assert orphans == [], (
        f"Brief has fields with no contract counterpart: {orphans}. "
        "Either add them to LEGACY_CONTRACT_COLUMNS (with a contract bump) "
        "or remove from the model."
    )
