"""Stamp the cheap Buffett numerics onto the Layer-4 scored frame (PR-1).

The ``score`` stage runs this right after
:func:`~alphalens_pipeline.thematic.screening.scorer.score_candidates`. For each
candidate it computes the Buffett quantitative panel (owner-earnings yield, ROIC,
margin of safety, coverage) — the SAME numbers ``buffett lens`` prints, minus the
expensive qualitative LLM layer — and the 0-100 quality score, then writes six
flat columns. They ride the existing merge chain into the brief parquet, Django,
and the card chip.

Why a separate step (not inside ``score_candidates``): it keeps the scorer's
golden replay untouched and makes the Buffett enrichment independently testable
with an injected ``panel_fn``. The default ``panel_fn`` builds its own
``EdgarFundamentalsStore`` and preloads ONLY the candidate tickers — those
companyfacts were already fetched by the scoring pass, so the preload is a shared
disk-cache hit (no new SEC network); the only incremental network is one market-
cap + one dividends lookup per unique ticker.

Fail-soft throughout: a store-build failure, a vendor hiccup, or a single bad
ticker yields ``None`` for that name's columns and never aborts the batch. The
six columns are ALWAYS added (``None`` when unavailable) so the parquet schema
stays stable day to day. The qualitative verdict is NOT computed here and NEVER
feeds the score — that stays display-only until Buffett×EDGE is validated.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

import pandas as pd

from alphalens_pipeline.buffett.comparison import BuffettPanel, compute_panel
from alphalens_pipeline.buffett.quality_score import compute_quality_score

logger = logging.getLogger(__name__)

# (ticker, theme, asof) -> BuffettPanel. Injected in tests; the default builds
# the real store + market-cap + dividends callables.
PanelFn = Callable[[str, str, dt.date], BuffettPanel | None]

# The six flat columns this step stamps onto every scored row.
BUFFETT_COLUMNS: tuple[str, ...] = (
    "buffett_owner_earnings_yield_pct",
    "buffett_roic_latest",
    "buffett_roic_3y_avg",
    "buffett_margin_of_safety_pct",
    "buffett_data_coverage",
    "buffett_quality_score",
)


def _columns_for(panel: BuffettPanel | None) -> dict[str, float | None]:
    """The six column values for one ticker; all ``None`` when no panel."""
    if panel is None:
        return dict.fromkeys(BUFFETT_COLUMNS)
    return {
        "buffett_owner_earnings_yield_pct": panel.owner_earnings_yield_pct,
        "buffett_roic_latest": panel.roic_latest,
        "buffett_roic_3y_avg": panel.roic_3y_avg,
        "buffett_margin_of_safety_pct": panel.margin_of_safety_pct,
        "buffett_data_coverage": panel.data_coverage,
        "buffett_quality_score": compute_quality_score(panel),
    }


def _theme_by_ticker(frame: pd.DataFrame) -> dict[str, str]:
    """First-seen theme per ticker (panel metadata only; never affects numerics)."""
    if "theme" not in frame.columns:
        return {}
    mapping: dict[str, str] = {}
    for ticker, theme in zip(frame["ticker"], frame["theme"], strict=False):
        mapping.setdefault(str(ticker), "" if theme is None else str(theme))
    return mapping


def enrich(frame: pd.DataFrame, *, asof: dt.date, panel_fn: PanelFn | None = None) -> pd.DataFrame:
    """Return ``frame`` with the six Buffett columns appended.

    Computes one panel per UNIQUE ticker (a pre-dedup frame may repeat a ticker
    across themes) and maps it back to every row. Order and all pre-existing
    columns are preserved. With no rows, the six columns are still added (zero
    length) so the schema is stable.
    """
    out = frame.copy()
    tickers = [str(t) for t in out["ticker"]] if "ticker" in out.columns else []
    unique = list(dict.fromkeys(tickers))

    if not unique:
        for col in BUFFETT_COLUMNS:
            out[col] = pd.Series([], dtype="float64")
        return out

    fn = panel_fn if panel_fn is not None else _build_default_panel_fn(unique)
    theme_by_ticker = _theme_by_ticker(out)

    per_ticker: dict[str, dict[str, float | None]] = {}
    for ticker in unique:
        panel = _safe_panel(fn, ticker, theme_by_ticker.get(ticker, ""), asof)
        per_ticker[ticker] = _columns_for(panel)

    for col in BUFFETT_COLUMNS:
        out[col] = [per_ticker[t][col] for t in tickers]
    return out


def _safe_panel(fn: PanelFn, ticker: str, theme: str, asof: dt.date) -> BuffettPanel | None:
    """Call ``fn`` returning its panel, or ``None`` (logged) on any exception."""
    try:
        return fn(ticker, theme, asof)
    except Exception as exc:
        logger.warning("buffett quant enrichment: panel(%s) failed: %s", ticker, exc)
        return None


def _build_default_panel_fn(tickers: list[str]) -> PanelFn:
    """Wire the real store + market-cap + dividends callables for production.

    Preloads ONLY ``tickers`` (already-fetched companyfacts -> disk-cache hit).
    On any wiring failure returns a no-op fn so enrichment degrades to all-``None``
    columns rather than crashing the score stage.
    """
    try:
        from alphalens_pipeline.data.alt_data.yfinance_client import (
            get_default_yfinance_client,
        )
        from alphalens_pipeline.data.store.edgar_fundamentals import (
            EdgarFundamentalsStore,
        )
        from alphalens_pipeline.thematic.verification.mcap_filter import fetch_mcap

        store = EdgarFundamentalsStore(with_prices=True)
        store.preload(tickers)
        dividends_fn = get_default_yfinance_client().dividends
    except Exception as exc:
        logger.warning("buffett quant enrichment: store wiring failed: %s", exc)
        return lambda ticker, theme, asof: None

    def fn(ticker: str, theme: str, asof: dt.date) -> BuffettPanel | None:
        return compute_panel(
            ticker,
            theme,
            asof,
            store=store,
            mcap_fn=fetch_mcap,
            dividends_fn=dividends_fn,
        )

    return fn


__all__ = ["BUFFETT_COLUMNS", "enrich"]
