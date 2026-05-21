"""Shared column registry for the SQLite cache and the API response models.

One source of truth for the 70-column brief schema lets ``cache.py`` build the
``CREATE TABLE`` and the row serializer from the same list that ``models.py``
uses to declare its Pydantic fields. Adding a column means one edit here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PyKind = Literal["str", "float", "int", "bool", "list_str", "datetime"]
SqlType = Literal["TEXT", "REAL", "INTEGER"]

SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Column:
    """One column in the brief row.

    ``name`` is the JSON / parquet / SQL identifier.
    ``py_kind`` drives both the Pydantic field type and the row serializer in
    ``cache.py``. ``sql_type`` is the storage type; SQLite is dynamic so this is
    mostly documentation, but it also makes the schema migration self-evident.
    """

    name: str
    py_kind: PyKind
    sql_type: SqlType


# Order mirrors the parquet schema in ~/.alphalens/thematic_briefs/*.parquet.
# Bool columns are stored as INTEGER 0/1; list[str] columns as JSON TEXT;
# datetime as ISO 8601 TEXT.
CANDIDATE_COLUMNS: tuple[Column, ...] = (
    Column("theme", "str", "TEXT"),
    Column("ticker", "str", "TEXT"),
    Column("company_name", "str", "TEXT"),
    Column("rationale", "str", "TEXT"),
    Column("gemini_confidence", "float", "REAL"),
    Column("market_cap", "float", "REAL"),
    Column("gates_passed", "list_str", "TEXT"),
    Column("gates_passed_str", "str", "TEXT"),
    Column("n_gates_passed", "int", "INTEGER"),
    Column("gates_failed", "list_str", "TEXT"),
    Column("gates_failed_str", "str", "TEXT"),
    Column("n_gates_failed", "int", "INTEGER"),
    Column("gates_unknown", "list_str", "TEXT"),
    Column("gates_unknown_str", "str", "TEXT"),
    Column("n_gates_unknown", "int", "INTEGER"),
    Column("verified", "bool", "INTEGER"),
    Column("source_event_url", "str", "TEXT"),
    Column("source_event_title", "str", "TEXT"),
    Column("source_event_published_at", "str", "TEXT"),
    Column("theme_search_keywords", "list_str", "TEXT"),
    Column("industry_id", "float", "REAL"),
    Column("industry_name", "str", "TEXT"),
    Column("sector_name", "str", "TEXT"),
    Column("insider_score_usd", "float", "REAL"),
    Column("insider_score_sector_percentile", "float", "REAL"),
    Column("fcff_yield_pct", "float", "REAL"),
    Column("fcff_yield_sector_percentile", "float", "REAL"),
    Column("valuation_pe", "float", "REAL"),
    Column("valuation_ps", "float", "REAL"),
    Column("valuation_ev_rev", "float", "REAL"),
    Column("valuation_ev_ebitda", "float", "REAL"),
    Column("valuation_fcf_margin", "float", "REAL"),
    Column("valuation_composite_sector_percentile", "float", "REAL"),
    Column("valuation_financials_publish_date", "str", "TEXT"),
    Column("valuation_financials_age_days", "int", "INTEGER"),
    Column("roic_pct", "float", "REAL"),
    Column("roe_pct", "float", "REAL"),
    Column("magic_formula_health_pass", "bool", "INTEGER"),
    Column("technical_rsi", "float", "REAL"),
    Column("technical_ma50_distance_pct", "float", "REAL"),
    Column("technical_atr_pct", "float", "REAL"),
    Column("technical_volume_zscore", "float", "REAL"),
    Column("technical_pct_off_52w_high", "float", "REAL"),
    Column("technical_pct_off_52w_low", "float", "REAL"),
    Column("technical_ma200_distance_pct", "float", "REAL"),
    Column("technical_ma200_slope_pct_per_day", "float", "REAL"),
    Column("technicals_summary_str", "str", "TEXT"),
    Column("catalyst_strength", "float", "REAL"),
    Column("catalyst_event_type", "str", "TEXT"),
    Column("catalyst_confidence", "float", "REAL"),
    Column("magic_formula_rank", "float", "REAL"),
    Column("magic_formula_cohort_n", "int", "INTEGER"),
    Column("deep_drawdown_reversal", "bool", "INTEGER"),
    Column("layer4_weighted_score", "int", "INTEGER"),
    Column("also_in_themes", "list_str", "TEXT"),
    Column("rank_in_day", "int", "INTEGER"),
    Column("cohort_size_in_day", "int", "INTEGER"),
    Column("next_earnings_date", "str", "TEXT"),
    Column("brief_model_used", "str", "TEXT"),
    Column("brief_tldr", "str", "TEXT"),
    Column("brief_supply_chain_md", "str", "TEXT"),
    Column("brief_bear_summary_md", "str", "TEXT"),
    Column("brief_catalyst_failure_exit", "str", "TEXT"),
    Column("brief_entry_price_note", "str", "TEXT"),
    Column("brief_position_pct", "float", "REAL"),
    Column("brief_time_exit_weeks", "int", "INTEGER"),
    Column("brief_time_exit_on_catalyst_failure_weeks", "int", "INTEGER"),
    Column("brief_disaster_stop_pct", "float", "REAL"),
    Column("brief_full_md", "str", "TEXT"),
    Column("brief_generated_at", "datetime", "TEXT"),
)

CANDIDATE_COLUMN_NAMES: tuple[str, ...] = tuple(c.name for c in CANDIDATE_COLUMNS)
COLUMN_BY_NAME: dict[str, Column] = {c.name: c for c in CANDIDATE_COLUMNS}

PRIMARY_KEY = ("date", "ticker")
