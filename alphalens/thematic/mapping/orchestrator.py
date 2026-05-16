"""Layer 3 orchestrator: propose candidates with Gemini 3 Pro, verify via 4 gates.

For each input theme, the orchestrator (a) asks the LLM for 5-15 candidate
small/mid-cap beneficiaries (see :mod:`gemini_mapper`) and (b) verifies each
candidate against four independent gates:

1. **ETF holdings** — is the ticker a constituent of any thematic ETF mapped
   to this theme? (NPORT-P parser, paradigm-independent.)
2. **10-K keyword grep** — does the company's most recent 10-K mention the
   theme keywords?
3. **Recent press** — has Polygon news in the last 30 days carried the theme
   keywords for this ticker?
4. **Form-4 insider activity** — net opportunistic buys above threshold over
   the last 90 days (paradigm #11 Cohen-Malloy reuse, αt +2.71 OOS validated).

A candidate is ``verified=True`` if **any** of the four gates passes. Output
is a parquet at ``~/.alphalens/thematic_candidates/{date}.parquet`` with one
row per (theme, ticker).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from alphalens.thematic.mapping import gemini_mapper
from alphalens.thematic.verification import etf_holdings, insider, recent_press, tenk_grep

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path.home() / ".alphalens" / "thematic_candidates"
GATE_NAMES = ("etf", "tenk", "press", "insider")


# Per-gate wrappers — keep tests patchable through `orchestrator.*` and let
# each gate fail closed if its underlying data path errors.


def _gate_etf(*, ticker: str, themes: Iterable[str], asof: dt.date) -> bool:
    return etf_holdings.is_in_thematic_etf(ticker=ticker, themes=themes)


def _gate_tenk(*, ticker: str, theme_keywords: Iterable[str], asof: dt.date) -> bool:
    return tenk_grep.has_theme_keywords_in_10k(ticker=ticker, keywords=theme_keywords)


def _gate_press(*, ticker: str, theme_keywords: Iterable[str], asof: dt.date, api_key: str) -> bool:
    return recent_press.has_theme_in_recent_press(
        ticker=ticker,
        asof=asof,
        keywords=theme_keywords,
        api_key=api_key,
    )


def _gate_insider(*, ticker: str, asof: dt.date) -> bool:
    return insider.has_opportunistic_buy(ticker=ticker, asof=asof)


def _safe(name: str, fn, **kwargs) -> bool:
    try:
        return bool(fn(**kwargs))
    except Exception as exc:
        logger.warning("verification gate %s raised: %s", name, exc)
        return False


def verify_candidate(
    *,
    ticker: str,
    themes: Iterable[str],
    asof: dt.date,
    api_key: str,
    theme_keywords: Iterable[str] | None = None,
) -> dict:
    """Run all four gates against ``(ticker, themes)`` and report which passed."""
    themes_list = list(themes)
    keywords = list(theme_keywords if theme_keywords is not None else themes_list)
    gates_passed: list[str] = []

    if _safe("etf", _gate_etf, ticker=ticker, themes=themes_list, asof=asof):
        gates_passed.append("etf")
    if _safe(
        "tenk",
        _gate_tenk,
        ticker=ticker,
        theme_keywords=keywords,
        asof=asof,
    ):
        gates_passed.append("tenk")
    if _safe(
        "press",
        _gate_press,
        ticker=ticker,
        theme_keywords=keywords,
        asof=asof,
        api_key=api_key,
    ):
        gates_passed.append("press")
    if _safe("insider", _gate_insider, ticker=ticker, asof=asof):
        gates_passed.append("insider")

    return {
        "ticker": ticker,
        "gates_passed": gates_passed,
        "verified": len(gates_passed) > 0,
    }


def map_themes(
    *,
    themes: Iterable[str],
    asof: dt.date,
    api_key: str | None = None,
    polygon_api_key: str | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    keep_unverified: bool = False,
) -> pd.DataFrame:
    """For each theme, propose candidates and run them through 4 gates.

    Writes a unified parquet to ``output_dir / {asof}.parquet`` and returns it.
    """
    api_key = api_key or os.environ.get("GOOGLE_API_KEY") or ""
    polygon_key = polygon_api_key or os.environ.get("POLYGON_API_KEY") or ""

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{asof.isoformat()}.parquet"

    rows: list[dict] = []
    for theme in themes:
        candidates = gemini_mapper.propose_candidates(theme=theme, api_key=api_key)
        for cand in candidates:
            verdict = verify_candidate(
                ticker=cand["ticker"],
                themes=[theme],
                asof=asof,
                api_key=polygon_key,
                theme_keywords=[theme.replace("_", " ")],
            )
            if not verdict["verified"] and not keep_unverified:
                continue
            rows.append(
                {
                    "theme": theme,
                    "ticker": cand["ticker"],
                    "company_name": cand.get("company_name", ""),
                    "rationale": cand.get("rationale", ""),
                    "gemini_confidence": cand.get("confidence", 0.0),
                    "gates_passed": verdict["gates_passed"],
                    "n_gates_passed": len(verdict["gates_passed"]),
                    "verified": verdict["verified"],
                }
            )

    if rows:
        df = (
            pd.DataFrame(rows)
            .sort_values(
                ["theme", "n_gates_passed", "gemini_confidence"],
                ascending=[True, False, False],
            )
            .reset_index(drop=True)
        )
    else:
        df = pd.DataFrame(
            columns=[
                "theme",
                "ticker",
                "company_name",
                "rationale",
                "gemini_confidence",
                "gates_passed",
                "n_gates_passed",
                "verified",
            ]
        )
    df.to_parquet(out_path, index=False)
    return df


__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "GATE_NAMES",
    "map_themes",
    "verify_candidate",
]
