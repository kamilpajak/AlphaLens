"""The O'Neil panel: one candidate's CANSLIM-reduced numerics + assembly helpers.

``ONeilPanel`` carries the cheap momentum/technical numbers O'Neil scores on, plus
the two audit booleans (split-suspected, near-zero earnings base). The technical
terms (N proximity-to-high, L MA200 trend) are REUSED from the screening scorer's
``technical_*`` columns already on the score-stage frame — O'Neil never recomputes
them. The earnings term comes from the shared ``EdgarFundamentalsStore``. The
split screen reads the SAME cached raw-close window the score pass wrote (no new
yfinance call). See ``docs/research/oneil_expert_design_2026_06_13.md``.

Every input degrades to ``None`` honestly (tri-state), never a fake zero.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import pandas as pd

logger = logging.getLogger(__name__)

# ``cached_daily_ohlcv``-shaped reader: (ticker, asof) -> raw-close OHLCV frame.
# Injected in tests; the default is the yfinance client's disk-cache reader. Used
# ONLY for the split screen — never to recompute a technical the frame already has.
OhlcvFn = Callable[[str, dt.date], "pd.DataFrame"]

# (ticker, asof) -> 0-100 relative-strength percentile, or None. Injected in tests;
# the default reads the split-adjusted grouped-daily history store (DISK ONLY — never
# an in-pass Polygon call). None on any store gap / candidate-absent (tri-state).
RsFn = Callable[[str, dt.date], "float | None"]


@runtime_checkable
class _AnnualStatement(Protocol):
    # Read-only property (not a bare attribute) so it is COVARIANT — a concrete
    # ``AnnualStatement`` with a ``net_income: float | None`` field satisfies it and
    # ``list[AnnualStatement]`` is a ``Sequence[_AnnualStatement]`` (a settable
    # Protocol attribute would be invariant and reject the real store).
    @property
    def net_income(self) -> float | None: ...


@runtime_checkable
class EarningsStore(Protocol):
    """The slice of ``EdgarFundamentalsStore`` O'Neil reads — a newest-first
    annual series with ``.net_income`` per fiscal year. A Protocol so a test can
    inject a fake without the whole store."""

    def annual_series_as_of(
        self, ticker: str, asof: dt.date, *, max_years: int = ...
    ) -> Sequence[_AnnualStatement]: ...


# A consecutive day-over-day close ratio further than this from 1.0 marks a
# suspected split in the raw-close window (auto_adjust=False), contaminating the
# 52w-high peak. 0.35 is looser than the monitor's 0.18 consecutive band so a
# legitimate large gap is not nulled, but tight enough to catch a >=2:1 split.
# Unvalidated heuristic — see the design memo's open risks.
_SPLIT_JUMP_THRESHOLD = 0.35

# When the prior fiscal year's net income is this small relative to the latest,
# the YoY growth % explodes into an uninformative artifact: the earnings term is
# EXCLUDED (not damped) and the near-zero-base flag records why. Unvalidated.
_NEAR_ZERO_BASE_RATIO = 0.05


@dataclass(frozen=True)
class ONeilPanel:
    """One candidate's O'Neil-reduced numerics. Every numeric is nullable."""

    ticker: str
    theme: str
    # N — proximity to the 52w high (0.0 @ high, negative % below). REUSED from
    # technical_pct_off_52w_high; stamped for display even when split-excluded.
    pct_off_52w_high: float | None
    # L — MA200 slope (per-day %); a falling/zero slope earns zero scoring credit.
    ma200_slope_pct_per_day: float | None
    # Trend CONTEXT — distance from MA200; DISPLAY-ONLY (not scored, not in coverage).
    ma200_distance_pct: float | None
    # C/A — latest-FY net-income YoY growth %; ``None`` when not usable.
    earnings_growth_yoy_pct: float | None
    # True when the earnings YoY was suppressed by a near-zero prior-year base
    # (the growth % would explode); ``None`` when earnings data is absent entirely.
    earnings_growth_near_zero_base: bool | None
    # True when the raw-close window shows a suspected split (so the N term is
    # treated as absent for scoring); ``None`` when the window is unavailable.
    new_high_split_suspected: bool | None
    # Fraction of the 3 OPTIONAL scoring terms (R, trend, earnings) that resolved.
    # The mandatory N term is a hard gate, not part of this fraction.
    data_coverage: float
    # R — relative-strength percentile (0-100) from the grouped-daily history store;
    # ``None`` on any store gap / candidate-absent. Optional scoring term. Defaulted
    # so pre-R-reactivation constructions stay valid.
    oneil_rs_approx_pct: float | None = None


def _detect_split(closes: pd.Series) -> bool | None:
    """``True`` if any consecutive close ratio is further than the threshold from
    1.0, ``False`` if the window is clean, ``None`` when it is too short to judge."""
    series = pd.to_numeric(closes, errors="coerce").dropna()
    if len(series) < 2:
        return None
    ratios = series / series.shift(1)
    jumps = (ratios - 1.0).abs()
    return bool((jumps > _SPLIT_JUMP_THRESHOLD).any())


def _split_suspected(ticker: str, asof: dt.date, ohlcv_fn: OhlcvFn | None) -> bool | None:
    """Run the split screen over the cached raw-close window; ``None`` when the
    window is unavailable (the ohlcv reader missing, empty, or raising)."""
    if ohlcv_fn is None:
        return None
    try:
        frame = ohlcv_fn(ticker, asof)
    except Exception as exc:
        logger.warning("oneil split screen: ohlcv(%s) failed: %s", ticker, exc)
        return None
    if frame is None or getattr(frame, "empty", True) or "close" not in frame.columns:
        return None
    return _detect_split(frame["close"])


def _earnings_growth_yoy(
    store: EarningsStore | None, ticker: str, asof: dt.date
) -> tuple[float | None, bool | None]:
    """Latest-FY net-income YoY growth %, and the near-zero-base flag.

    Returns ``(None, None)`` when earnings data is absent (< 2 fiscal years, or
    either net income missing). Returns ``(None, False)`` on a non-positive prior
    year (sign flip — uninformative, but NOT a near-zero artifact). Returns
    ``(None, True)`` when the prior base is near zero (exploding ratio excluded).
    Otherwise ``((latest - prior) / |prior| * 100, False)``.
    """
    if store is None:
        return None, None
    try:
        series = store.annual_series_as_of(ticker, asof, max_years=2)
    except Exception as exc:
        logger.warning("oneil earnings: annual_series(%s) failed: %s", ticker, exc)
        return None, None
    if series is None or len(series) < 2:
        return None, None
    latest = series[0].net_income
    prior = series[1].net_income
    if latest is None or prior is None:
        return None, None
    if prior <= 0:
        # Sign flip / loss-making base: the growth % is uninformative. Excluded,
        # but this is NOT the near-zero artifact, so the flag is False.
        return None, False
    if abs(prior) < _NEAR_ZERO_BASE_RATIO * abs(latest):
        return None, True
    return (latest - prior) / abs(prior) * 100.0, False


def compute_oneil_panel(
    ticker: str,
    theme: str,
    asof: dt.date,
    *,
    pct_off_52w_high: float | None,
    ma200_slope_pct_per_day: float | None,
    ma200_distance_pct: float | None,
    store: EarningsStore | None = None,
    ohlcv_fn: OhlcvFn | None = None,
    rs_fn: RsFn | None = None,
) -> ONeilPanel:
    """Assemble the O'Neil panel for one candidate from frame technicals + stores.

    The three technical inputs are passed in (read off the score-stage frame by
    the caller); the earnings term + split screen come from the injected store +
    ohlcv reader; R (relative strength) comes from the injected ``rs_fn`` (a DISK-ONLY
    read of the grouped-daily history store — never an in-pass Polygon call).
    ``data_coverage`` counts the three OPTIONAL terms (R, trend, earnings) that
    resolved — the mandatory N term is a gate, not a fraction.
    """
    earnings_growth, near_zero_base = _earnings_growth_yoy(store, ticker, asof)
    split_suspected = _split_suspected(ticker, asof, ohlcv_fn)
    rs_approx = _rs_approx(ticker, asof, rs_fn)

    trend_present = ma200_slope_pct_per_day is not None
    earnings_present = earnings_growth is not None
    rs_present = rs_approx is not None
    data_coverage = (int(trend_present) + int(earnings_present) + int(rs_present)) / 3.0

    return ONeilPanel(
        ticker=ticker,
        theme=theme,
        pct_off_52w_high=pct_off_52w_high,
        ma200_slope_pct_per_day=ma200_slope_pct_per_day,
        ma200_distance_pct=ma200_distance_pct,
        earnings_growth_yoy_pct=earnings_growth,
        earnings_growth_near_zero_base=near_zero_base,
        new_high_split_suspected=split_suspected,
        data_coverage=data_coverage,
        oneil_rs_approx_pct=rs_approx,
    )


def _rs_approx(ticker: str, asof: dt.date, rs_fn: RsFn | None) -> float | None:
    """The relative-strength percentile from the injected reader; ``None`` when the
    reader is missing or raises (mirror the split-screen fail-soft, tri-state None)."""
    if rs_fn is None:
        return None
    try:
        return rs_fn(ticker, asof)
    except Exception as exc:
        logger.warning("oneil rs: rs_fn(%s) failed: %s", ticker, exc)
        return None


__all__ = ["ONeilPanel", "RsFn", "compute_oneil_panel"]
