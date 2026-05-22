"""Shared fixture builders for the API test suite.

Hermetic: no read from ``~/.alphalens``. Tests construct in-tmpdir parquet
files and rebuild the SQLite cache from them.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from alphalens.api.schema import CANDIDATE_COLUMNS

_DEFAULTS_BY_KIND: dict[str, Any] = {
    "str": "n/a",
    "float": 0.0,
    "int": 0,
    "bool": False,
    "list_str": [],
    "datetime": pd.Timestamp("2026-05-18T00:00:00", tz="UTC"),
}


def _default_row(ticker: str, theme: str, score: int) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for col in CANDIDATE_COLUMNS:
        row[col.name] = _DEFAULTS_BY_KIND[col.py_kind]
    row["ticker"] = ticker
    row["theme"] = theme
    row["company_name"] = f"{ticker} Inc."
    row["layer4_weighted_score"] = score
    row["rank_in_day"] = 1
    row["cohort_size_in_day"] = 1
    row["n_gates_passed"] = 4
    row["n_gates_failed"] = 0
    row["n_gates_unknown"] = 0
    row["valuation_financials_age_days"] = 60
    row["magic_formula_cohort_n"] = 12
    row["brief_time_exit_weeks"] = 6
    row["brief_time_exit_on_catalyst_failure_weeks"] = 2
    row["verified"] = True
    row["magic_formula_health_pass"] = True
    row["deep_drawdown_reversal"] = False
    row["gemini_confidence"] = 0.85
    row["market_cap"] = 1_500_000_000.0
    row["industry_id"] = 7370.0
    row["fcff_yield_pct"] = 4.2
    row["valuation_ps"] = 5.5
    row["technical_pct_off_52w_high"] = -15.0
    row["technical_ma200_distance_pct"] = -3.5
    row["gates_passed"] = ["polygon_news", "etf_holdings"]
    row["gates_failed"] = []
    row["gates_unknown"] = []
    row["theme_search_keywords"] = [theme, f"{theme} ETF"]
    row["also_in_themes"] = []
    row["brief_tldr"] = f"{ticker} is a play on {theme}."
    row["brief_model_used"] = "gemini-3-pro-preview"
    row["brief_generated_at"] = pd.Timestamp.now(tz="UTC")
    row["source_event_url"] = "https://example.com/article"
    row["source_event_title"] = f"{ticker} announces {theme} expansion"
    row["source_event_published_at"] = "2026-05-18T12:00:00+00:00"
    return row


def make_day(date: dt.date, rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Build a DataFrame matching the parquet schema for one day."""
    df = pd.DataFrame(rows)
    return df


def write_brief(briefs_dir: Path, date: dt.date, rows: list[dict[str, Any]]) -> Path:
    briefs_dir.mkdir(parents=True, exist_ok=True)
    df = make_day(date, rows)
    target = briefs_dir / f"{date.isoformat()}.parquet"
    df.to_parquet(target, index=False)
    return target


def seed_two_days(briefs_dir: Path) -> tuple[Path, Path]:
    """Write a small, realistic two-day brief set into ``briefs_dir``."""
    day_old = dt.date(2026, 5, 17)
    day_new = dt.date(2026, 5, 18)

    older = [
        _default_row("AAA", "quantum_computing", 5),
        _default_row("BBB", "quantum_computing", 4),
        _default_row("CCC", "robotics", 3),
    ]
    newer = [
        _default_row("AAA", "quantum_computing", 6),
        _default_row("DDD", "robotics", 5),
        _default_row("EEE", "weight_loss_drugs", 4),
        _default_row("FFF", "weight_loss_drugs", 2),
    ]
    # Add an explicit NaN to exercise float coercion.
    newer[1]["technical_rsi"] = np.float64("nan")
    return write_brief(briefs_dir, day_old, older), write_brief(briefs_dir, day_new, newer)


def seed_day_with_distinct_rank_order(briefs_dir: Path) -> Path:
    """Write a day where ``rank_in_day`` deliberately disagrees with
    ``(layer4_weighted_score DESC, ticker ASC)``.

    Models the post-2026-05-18 orchestrator's 7-key sort: within a layer4
    tier, tiebreakers (catalyst_strength, insider_score_usd, …) can elevate
    a ticker above its alphabetical neighbour. The route handler must serve
    candidates in ``rank_in_day`` order so the displayed cards match the
    ``NN/NN`` rank chips the renderer prints.
    """
    day = dt.date(2026, 5, 21)
    # Layer-4 + alphabetical would give: AAA(5)→1, BBB(4)→2, CCC(4)→3, DDD(4)→4, EEE(3)→5
    # But the orchestrator's 7-key sort (with assumed tiebreakers) places
    # rank_in_day as: AAA=1, DDD=2, BBB=3, CCC=4, EEE=5.
    rows = [
        _default_row("AAA", "quantum_computing", 5),
        _default_row("BBB", "quantum_computing", 4),
        _default_row("CCC", "quantum_computing", 4),
        _default_row("DDD", "quantum_computing", 4),
        _default_row("EEE", "robotics", 3),
    ]
    ranks = {"AAA": 1, "DDD": 2, "BBB": 3, "CCC": 4, "EEE": 5}
    for r in rows:
        r["rank_in_day"] = ranks[r["ticker"]]
        r["cohort_size_in_day"] = len(rows)
    return write_brief(briefs_dir, day, rows)


def seed_min_schema_day(briefs_dir: Path) -> Path:
    """Write a parquet with only the required (ticker, theme) columns + score.

    Mirrors the 2023-era briefs in the real cache and proves the lenient
    schema handler doesn't blow up on subset columns.
    """
    day = dt.date(2023, 1, 23)
    df = pd.DataFrame(
        {
            "ticker": ["LEGACY1", "LEGACY2"],
            "theme": ["solar", "solar"],
            "company_name": ["Legacy One", "Legacy Two"],
            "layer4_weighted_score": [3, 2],
        }
    )
    briefs_dir.mkdir(parents=True, exist_ok=True)
    target = briefs_dir / f"{day.isoformat()}.parquet"
    df.to_parquet(target, index=False)
    return target
