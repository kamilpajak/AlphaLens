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
from alphalens.thematic.verification import (
    etf_holdings,
    insider,
    mcap_filter,
    recent_press,
    tenk_grep,
)

DEFAULT_MCAP_RANGE = (500_000_000, 10_000_000_000)

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path.home() / ".alphalens" / "thematic_candidates"
GATE_NAMES = ("etf", "tenk", "press", "insider")


# Per-gate wrappers — keep tests patchable through `orchestrator.*` and let
# each gate fail closed if its underlying data path errors.


def _gate_etf(*, ticker: str, themes: Iterable[str], asof: dt.date) -> bool:
    return etf_holdings.is_in_thematic_etf(ticker=ticker, themes=themes)


def _gate_tenk(*, ticker: str, theme_keywords: Iterable[str], asof: dt.date) -> bool:
    return tenk_grep.has_theme_keywords_in_10k(ticker=ticker, keywords=theme_keywords)


def _gate_press(
    *,
    ticker: str,
    theme_keywords: Iterable[str],
    asof: dt.date,
    api_key: str,
    press_df: pd.DataFrame | None = None,
) -> bool:
    if press_df is not None:
        return recent_press.has_theme_in_press_frame(
            ticker=ticker, keywords=theme_keywords, press_df=press_df
        )
    return recent_press.has_theme_in_recent_press(
        ticker=ticker, asof=asof, keywords=theme_keywords, api_key=api_key
    )


def _gate_insider(*, ticker: str, asof: dt.date) -> bool:
    return insider.has_opportunistic_buy(ticker=ticker, asof=asof)


def _safe(name: str, fn, **kwargs) -> bool | None:
    """Run a gate function returning tri-state ``bool | None``.

    - ``True``  — gate ran and the candidate qualifies.
    - ``False`` — gate ran and concluded "no" (real negative signal).
    - ``None``  — gate could not determine (missing data, network error,
      unresolved CIK, etc.). Distinct from False so the orchestrator can
      record ``gates_unknown`` instead of silently failing closed.
    """
    try:
        result = fn(**kwargs)
    except Exception as exc:
        logger.warning("verification gate %s raised: %s", name, exc, exc_info=True)
        return None
    if result is None:
        return None
    return bool(result)


def _theme_keywords(theme: str) -> list[str]:
    """Expand a theme name into search keywords for the gates.

    Handles the common ``snake_case`` -> ``snake case`` swap so a theme like
    ``quantum_computing`` matches a 10-K passage that says "quantum computing"
    without underscores. Both forms are passed so the gate can substring-match
    against either representation.
    """
    raw = str(theme).strip()
    spaced = raw.replace("_", " ")
    return [v for v in {raw, spaced} if v]


def verify_candidate(
    *,
    ticker: str,
    themes: Iterable[str],
    asof: dt.date,
    api_key: str,
    theme_keywords: Iterable[str] | None = None,
    press_df: pd.DataFrame | None = None,
) -> dict:
    """Run all four gates against ``(ticker, themes)`` and report which passed.

    ``press_df``, when supplied, is the orchestrator's pre-fetched
    window-wide Polygon news frame; the press gate then runs purely in-memory.
    """
    themes_list = list(themes)
    if theme_keywords is None:
        expanded: list[str] = []
        for t in themes_list:
            expanded.extend(_theme_keywords(t))
        keywords = list(dict.fromkeys(expanded))
    else:
        keywords = list(theme_keywords)

    def _record(name: str, result: bool | None):
        if result is True:
            gates_passed.append(name)
        elif result is False:
            gates_failed.append(name)
        else:
            gates_unknown.append(name)

    gates_passed: list[str] = []
    gates_failed: list[str] = []
    gates_unknown: list[str] = []

    _record("etf", _safe("etf", _gate_etf, ticker=ticker, themes=themes_list, asof=asof))
    _record(
        "tenk",
        _safe(
            "tenk",
            _gate_tenk,
            ticker=ticker,
            theme_keywords=keywords,
            asof=asof,
        ),
    )
    _record(
        "press",
        _safe(
            "press",
            _gate_press,
            ticker=ticker,
            theme_keywords=keywords,
            asof=asof,
            api_key=api_key,
            press_df=press_df,
        ),
    )
    _record("insider", _safe("insider", _gate_insider, ticker=ticker, asof=asof))

    return {
        "ticker": ticker,
        "gates_passed": gates_passed,
        "gates_failed": gates_failed,
        "gates_unknown": gates_unknown,
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
    market_cap_range: tuple[int, int] = DEFAULT_MCAP_RANGE,
) -> pd.DataFrame:
    """For each theme, propose candidates, post-filter by real-time mcap, then verify.

    The Gemini Pro client is built ONCE for the whole batch (avoid per-theme
    handshake), and the Polygon news window is fetched ONCE for all
    candidates (avoid per-candidate 5-req/min rate-limit sleep). After Pro
    returns candidates, ``mcap_filter.filter_by_mcap`` drops anything outside
    ``market_cap_range`` via yfinance — the LLM cannot do this reliably
    because its mcap snapshot is stuck at training-cutoff prices. Writes a
    unified parquet to ``output_dir / {asof}.parquet`` and returns it.
    """
    api_key = api_key or os.environ.get("GOOGLE_API_KEY") or ""
    polygon_key = polygon_api_key or os.environ.get("POLYGON_API_KEY") or ""

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{asof.isoformat()}.parquet"

    # Hoist Gemini Pro client out of the per-theme loop.
    pro_client = None
    pro_types_mod = None
    if api_key:
        try:
            from google import genai as _genai
            from google.genai import types as _types

            pro_client = _genai.Client(api_key=api_key)
            pro_types_mod = _types
        except ImportError:
            logger.warning("google-genai SDK missing; mapper will lazy-init per call")

    # Pre-fetch one window-wide press frame for all candidates; falls back to
    # an empty frame on failure (gate then fails closed per-ticker).
    press_df = pd.DataFrame()
    if polygon_key:
        try:
            press_df = recent_press.fetch_window_universe(asof=asof, api_key=polygon_key)
        except Exception as exc:
            logger.warning("press window fetch failed: %s", exc, exc_info=True)

    min_cap, max_cap = market_cap_range
    rows: list[dict] = []
    for theme in themes:
        candidates = gemini_mapper.propose_candidates(
            theme=theme,
            api_key=api_key,
            client=pro_client,
            types_mod=pro_types_mod,
        )
        if not candidates:
            continue
        in_bracket = mcap_filter.filter_by_mcap(
            [c["ticker"] for c in candidates], min_cap=min_cap, max_cap=max_cap
        )
        candidates = [c for c in candidates if c["ticker"] in in_bracket]
        keywords = _theme_keywords(theme)
        for cand in candidates:
            verdict = verify_candidate(
                ticker=cand["ticker"],
                themes=[theme],
                asof=asof,
                api_key=polygon_key,
                theme_keywords=keywords,
                press_df=press_df,
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
                    "market_cap": in_bracket[cand["ticker"]],
                    "gates_passed": verdict["gates_passed"],
                    "gates_passed_str": ",".join(verdict["gates_passed"]),
                    "n_gates_passed": len(verdict["gates_passed"]),
                    "gates_failed": verdict["gates_failed"],
                    "gates_failed_str": ",".join(verdict["gates_failed"]),
                    "n_gates_failed": len(verdict["gates_failed"]),
                    "gates_unknown": verdict["gates_unknown"],
                    "gates_unknown_str": ",".join(verdict["gates_unknown"]),
                    "n_gates_unknown": len(verdict["gates_unknown"]),
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
