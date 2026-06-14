"""Stamp the O'Neil numerics onto the Layer-4 scored frame (PR-7).

The ``score`` stage runs this right after the Buffett enrichment, which runs right
after :func:`~alphalens_pipeline.thematic.screening.scorer.score_candidates`. By
that point the frame already carries the screening scorer's ``technical_*`` columns
(52w-high proximity, MA200 slope / distance) — O'Neil REUSES them off the frame
(zero incremental yfinance calls) for its N + L terms, computes the C/A earnings
term from the shared ``EdgarFundamentalsStore``, runs a split screen over the cached
raw-close window the score pass wrote, and writes eight flat ``oneil_*`` columns.

The eight columns ride the existing merge chain into the brief parquet. They sit
present-but-unread until PR-8 surfaces them on the card (Django ``_EXPERT_COLUMNS``
+ coerce sets + the frozen pin must be extended in lockstep — see the design memo).

Fail-soft throughout: a store-build failure, a vendor hiccup, or a single bad ticker
yields ``None`` for that name's columns and never aborts the batch. The eight columns
are ALWAYS added (``None`` when unavailable) so the parquet schema stays stable. No
O'Neil column ever feeds the brief sort (the PR-6 allowlist enforces that).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

import pandas as pd

from alphalens_pipeline.experts.oneil.comparison import ONeilPanel, compute_oneil_panel
from alphalens_pipeline.experts.oneil.score import compute_oneil_score

logger = logging.getLogger(__name__)

# (ticker, theme, asof, technicals) -> ONeilPanel. Injected in tests; the default
# wires the real store + ohlcv cache reader. ``technicals`` carries the three
# frame-derived technical values for that ticker.
PanelFn = Callable[[str, str, dt.date, "_Technicals"], ONeilPanel | None]

# The eight flat columns this step stamps onto every scored row. The first six are
# float (None -> NaN); the last two booleans are emitted as float 0.0/1.0/NaN so the
# parquet keeps a single dtype and Django ``coerce_optional_bool`` restores the
# None/True/False tri-state.
ONEIL_COLUMNS: tuple[str, ...] = (
    "oneil_pct_off_52w_high",
    "oneil_ma200_slope_pct_per_day",
    "oneil_ma200_distance_pct",
    "oneil_earnings_growth_yoy_pct",
    "oneil_earnings_growth_near_zero_base",
    "oneil_new_high_split_suspected",
    "oneil_data_coverage",
    "oneil_score",
    "oneil_rs_approx_pct",
)

# The screening-scorer technical columns O'Neil reuses, keyed by the panel field.
_TECHNICAL_SOURCE: dict[str, str] = {
    "pct_off_52w_high": "technical_pct_off_52w_high",
    "ma200_slope_pct_per_day": "technical_ma200_slope_pct_per_day",
    "ma200_distance_pct": "technical_ma200_distance_pct",
}

# A per-ticker bundle of the three reused technical values.
_Technicals = dict[str, "float | None"]


def _bool_to_float(value: bool | None) -> float | None:
    """Encode a tri-state bool as 0.0 / 1.0 / None (-> NaN in the float column)."""
    if value is None:
        return None
    return 1.0 if value else 0.0


def _columns_for(panel: ONeilPanel | None) -> dict[str, float | None]:
    """The nine column values for one ticker; all ``None`` when no panel."""
    if panel is None:
        return dict.fromkeys(ONEIL_COLUMNS)
    return {
        "oneil_pct_off_52w_high": panel.pct_off_52w_high,
        "oneil_ma200_slope_pct_per_day": panel.ma200_slope_pct_per_day,
        "oneil_ma200_distance_pct": panel.ma200_distance_pct,
        "oneil_earnings_growth_yoy_pct": panel.earnings_growth_yoy_pct,
        "oneil_earnings_growth_near_zero_base": _bool_to_float(
            panel.earnings_growth_near_zero_base
        ),
        "oneil_new_high_split_suspected": _bool_to_float(panel.new_high_split_suspected),
        "oneil_data_coverage": panel.data_coverage,
        "oneil_score": compute_oneil_score(panel),
        "oneil_rs_approx_pct": panel.oneil_rs_approx_pct,
    }


def _coerce_float(value: object) -> float | None:
    """A single cell -> ``float`` or ``None`` (NaN / non-numeric become ``None``)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if pd.isna(out) else out


def _technicals_by_ticker(frame: pd.DataFrame) -> dict[str, _Technicals]:
    """First-seen technical bundle per ticker (same ticker -> same technicals
    regardless of theme; a column absent from the frame yields ``None`` for that
    term)."""
    out: dict[str, _Technicals] = {}
    for _, row in frame.iterrows():
        ticker = str(row["ticker"])
        if ticker in out:
            continue
        out[ticker] = {
            field: _coerce_float(row[col]) if col in frame.columns else None
            for field, col in _TECHNICAL_SOURCE.items()
        }
    return out


def _theme_by_ticker(frame: pd.DataFrame) -> dict[str, str]:
    """First-seen theme per ticker (panel metadata only; never affects numerics)."""
    if "theme" not in frame.columns:
        return {}
    mapping: dict[str, str] = {}
    for ticker, theme in zip(frame["ticker"], frame["theme"], strict=False):
        resolved = "" if theme is None or pd.isna(theme) else str(theme)
        mapping.setdefault(str(ticker), resolved)
    return mapping


def enrich(frame: pd.DataFrame, *, asof: dt.date, panel_fn: PanelFn | None = None) -> pd.DataFrame:
    """Return ``frame`` with the eight O'Neil columns appended.

    Computes one panel per UNIQUE ticker (a pre-dedup frame may repeat a ticker
    across themes) and maps it back to every row. Order and all pre-existing
    columns are preserved. With no rows, the eight columns are still added (zero
    length) so the schema is stable.
    """
    out = frame.copy()
    tickers = [str(t) for t in out["ticker"]] if "ticker" in out.columns else []
    unique = list(dict.fromkeys(tickers))

    if not unique:
        for col in ONEIL_COLUMNS:
            out[col] = pd.Series([], dtype="float64")
        return out

    fn = panel_fn if panel_fn is not None else build_default_panel_fn(unique)
    theme_by_ticker = _theme_by_ticker(out)
    technicals_by_ticker = _technicals_by_ticker(out)

    per_ticker: dict[str, dict[str, float | None]] = {}
    for ticker in unique:
        panel = _safe_panel(
            fn,
            ticker,
            theme_by_ticker.get(ticker, ""),
            asof,
            technicals_by_ticker.get(ticker, dict.fromkeys(_TECHNICAL_SOURCE)),
        )
        per_ticker[ticker] = _columns_for(panel)

    # Explicit float64 (None -> NaN) so the all-None degraded path keeps the SAME
    # dtype as the empty-frame branch (and the two bool-as-float columns stay
    # numeric for Django coerce_optional_bool to restore the tri-state).
    for col in ONEIL_COLUMNS:
        out[col] = pd.Series(
            [per_ticker[t][col] for t in tickers], index=out.index, dtype="float64"
        )
    return out


def _safe_panel(
    fn: PanelFn, ticker: str, theme: str, asof: dt.date, technicals: _Technicals
) -> ONeilPanel | None:
    """Call ``fn`` returning its panel, or ``None`` (logged) on any exception."""
    try:
        return fn(ticker, theme, asof, technicals)
    except Exception as exc:
        logger.warning("oneil quant enrichment: panel(%s) failed: %s", ticker, exc)
        return None


def build_default_panel_fn(tickers: list[str]) -> PanelFn:
    """Wire the real earnings store + ohlcv cache reader for production.

    Builds ``EdgarFundamentalsStore(with_prices=False)`` (O'Neil has no owner-
    earnings / market-cap term) and preloads ONLY ``tickers`` — those companyfacts
    were already fetched by the score + Buffett passes, so the preload is a shared
    disk-cache hit (no new SEC network). The split screen reads the cached raw-close
    window the score pass wrote (no new yfinance call). On any wiring failure
    returns a no-op fn so enrichment degrades to all-``None`` columns rather than
    crashing the score stage.
    """
    try:
        from alphalens_pipeline.data import rs_history
        from alphalens_pipeline.data.alt_data.yfinance_client import (
            get_default_yfinance_client,
        )
        from alphalens_pipeline.data.store.edgar_fundamentals import (
            EdgarFundamentalsStore,
        )

        store = EdgarFundamentalsStore(with_prices=False)
        store.preload(tickers)
        yf = get_default_yfinance_client()

        def ohlcv_fn(ticker: str, asof: dt.date) -> pd.DataFrame:
            return yf.cached_daily_ohlcv(ticker, asof=asof)

        # R (relative strength) — DISK ONLY: reads the split-adjusted grouped-daily
        # history store the nightly top-up maintains. NO in-pass Polygon call; a
        # store gap / candidate-absent yields None (tri-state, R simply drops out).
        def rs_fn(ticker: str, asof: dt.date) -> float | None:
            return rs_history.rs_percentile(rs_history.DEFAULT_RS_HISTORY_ROOT, ticker, asof)
    except Exception as exc:
        logger.warning("oneil quant enrichment: store wiring failed: %s", exc)
        return lambda ticker, theme, asof, technicals: None

    def fn(ticker: str, theme: str, asof: dt.date, technicals: _Technicals) -> ONeilPanel | None:
        return compute_oneil_panel(
            ticker,
            theme,
            asof,
            pct_off_52w_high=technicals.get("pct_off_52w_high"),
            ma200_slope_pct_per_day=technicals.get("ma200_slope_pct_per_day"),
            ma200_distance_pct=technicals.get("ma200_distance_pct"),
            store=store,
            ohlcv_fn=ohlcv_fn,
            rs_fn=rs_fn,
        )

    return fn


__all__ = ["ONEIL_COLUMNS", "build_default_panel_fn", "enrich"]
