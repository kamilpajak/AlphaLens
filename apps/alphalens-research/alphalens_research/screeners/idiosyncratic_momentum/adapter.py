"""BacktestEngine adapter for the idiosyncratic-momentum scorer.

The engine calls ``scorer(histories, config)`` per rebalance with truncated
PIT daily-OHLCV dataframes. This adapter composes the pure
``score_idiosyncratic_momentum`` against:

- daily close → monthly returns (via ``monthly_returns_from_daily``)
- FF3 monthly factors derived from ``data.factors.load_carhart_daily``
- FF risk-free monthly rate from the same FF table

The FF3 monthly factors + RF series are passed in once via
``__init__`` so each rebalance avoids re-resampling the daily FF table.
The adapter's ``__call__`` matches the ``Scorer`` protocol from
``alphalens_research.backtest.engine``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import pandas as pd

from alphalens_research.screeners.idiosyncratic_momentum.scorer import (
    monthly_returns_from_daily,
    score_idiosyncratic_momentum,
)

logger = logging.getLogger(__name__)

_DEFAULT_REGRESSION_WINDOW = 36
_DEFAULT_FORMATION_LOOKBACK = 12
_DEFAULT_SKIP = 2
_DEFAULT_PRICE_FLOOR = 5.0


class IdiosyncraticMomentumScorer:
    """Composes monthly-return → rolling FF3 residualisation → top-decile ranking.

    Bar-warm-up requirement: 36 monthly observations × ~21 trading days
    plus a buffer for short-history tickers. The engine's
    ``MIN_BARS_REQUIRED`` attribute filters histories before any scorer
    is called.
    """

    # 36 months × ~21 trading days = ~756 bars. Bumped to 900 (~6 months
    # safety buffer over the 756-bar minimum) after zen review 2026-05-14
    # H1 follow-up — the prior 800 leaves only ~1 month buffer which can
    # silently drop tickers when Thanksgiving/Christmas holiday clusters
    # consume the cushion. Engine skips tickers with fewer bars.
    MIN_BARS_REQUIRED = 900

    def __init__(
        self,
        ff3_monthly: pd.DataFrame,
        rf_monthly: pd.Series,
        *,
        window: int = _DEFAULT_REGRESSION_WINDOW,
        formation_lookback: int = _DEFAULT_FORMATION_LOOKBACK,
        skip: int = _DEFAULT_SKIP,
        price_floor: float = _DEFAULT_PRICE_FLOOR,
    ) -> None:
        required = {"Mkt-RF", "SMB", "HML"}
        missing = required - set(ff3_monthly.columns)
        if missing:
            raise ValueError(f"ff3_monthly missing columns: {sorted(missing)}")
        self._ff3_monthly = ff3_monthly[["Mkt-RF", "SMB", "HML"]].copy()
        self._rf_monthly = rf_monthly.copy()
        self._window = window
        self._formation_lookback = formation_lookback
        self._skip = skip
        self._price_floor = price_floor

    def __call__(
        self,
        histories: Mapping[str, pd.DataFrame],
        config: Mapping | None = None,
    ) -> pd.DataFrame:
        cfg = dict(config or {})
        benchmark = cfg.get("benchmark")
        asof = cfg.get("asof") or _derive_asof(histories)
        if asof is None:
            return pd.DataFrame(columns=["ticker", "score"])
        asof_month = pd.Timestamp(asof) + pd.offsets.MonthEnd(0)

        monthly_by_ticker: dict[str, pd.Series] = {}
        for ticker, df in histories.items():
            if ticker == benchmark or df is None or df.empty:
                continue
            close = df["close"]
            if float(close.iloc[-1]) < self._price_floor:
                continue
            monthly = monthly_returns_from_daily(close)
            if monthly.empty:
                continue
            monthly_by_ticker[ticker] = monthly

        if not monthly_by_ticker:
            return pd.DataFrame(columns=["ticker", "score"])

        scores = score_idiosyncratic_momentum(
            monthly_by_ticker,
            self._ff3_monthly,
            self._rf_monthly,
            asof_month,
            window=self._window,
            formation_lookback=self._formation_lookback,
            skip=self._skip,
        )
        if scores.empty:
            return pd.DataFrame(columns=["ticker", "score"])

        out = pd.DataFrame({"ticker": scores.index.tolist(), "score": scores.values.astype(float)})
        return out.sort_values("score", ascending=False).reset_index(drop=True)


def _derive_asof(histories: Mapping[str, pd.DataFrame]) -> pd.Timestamp | None:
    latest: pd.Timestamp | None = None
    for df in histories.values():
        if df is None or len(df) == 0:
            continue
        candidate = df.index[-1]
        if latest is None or candidate > latest:
            latest = candidate
    return latest


def ff3_monthly_from_carhart_daily(carhart_daily: pd.DataFrame) -> pd.DataFrame:
    """Resample the daily Carhart factor table to month-end FF3 monthly factors.

    Compounds daily simple returns into monthly: ``(1 + r_d).prod() - 1`` for
    each of ``Mkt-RF``, ``SMB``, ``HML``. RF is reported as the same daily
    rate from FF; compounding it the same way gives the correct monthly RF.

    Note on convention: for ``Mkt-RF`` (and other long-short factor
    returns), ``(1 + r_d).prod() - 1`` yields compounded daily excess,
    which differs algebraically from ``Mkt_monthly - RF_monthly``. The
    difference is sub-bps/year on monthly windows and is the standard
    convention used in academic factor literature when daily series are
    aggregated up. Residualisation is unaffected because the same
    convention is used on both sides of the regression.
    """
    required = {"Mkt-RF", "SMB", "HML", "RF"}
    missing = required - set(carhart_daily.columns)
    if missing:
        raise ValueError(f"carhart_daily missing columns: {sorted(missing)}")
    cols = ["Mkt-RF", "SMB", "HML", "RF"]
    daily = carhart_daily[cols].copy()
    daily.index = pd.to_datetime(daily.index)
    monthly = (1.0 + daily).resample("ME").prod() - 1.0
    return monthly


def rf_monthly_from_carhart_daily(carhart_daily: pd.DataFrame) -> pd.Series:
    """Extract the month-end RF Series from a daily Carhart table."""
    return ff3_monthly_from_carhart_daily(carhart_daily)["RF"]
