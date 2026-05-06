"""Form-4 opportunistic-insider scorer (Cohen-Malloy 2012 mechanism).

Pre-registered in
``docs/research/preregistration/params_insider_form4_opportunistic_2026_05_05.json``.

Two stages:

  1. :func:`aggregate_opportunistic_signal` — given a pre-fetched DataFrame of
     Form-4 records for a single ticker over the lookback window, filter and
     sum to a scalar ``net_oppor_usd_t``. Filters: code in {P, S}; officer or
     director (10% beneficial owners excluded unless also officer/director);
     reporting_owner classified OPPORTUNISTIC (routine + unclassified dropped).
     Sign convention: P (purchase) contributes +usd, S (sale) contributes -usd.

  2. :func:`score_opportunistic_form4` — given a wide feature DataFrame
     (asof × ticker × {signal_raw, reversal_1m, momentum_6m, rv_30d}), fit
     per-asof OLS of ``signal_raw`` on equity controls + intercept and return
     residuals. Mirror of v9D ``score_cross_sectional_residual`` with
     ``signal_raw`` in place of ``-ivp30``.

Sign of the score: positive = bullish (NET BUY direction). No sign flip — the
hypothesis is mechanically aligned with the observable.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Protocol

import numpy as np
import pandas as pd

from alphalens.screeners.insider_activity.cohen_malloy_classifier import (
    CohenMalloyLabel,
)

EQUITY_CONTROLS_FOR_RESIDUAL: tuple[str, ...] = (
    "reversal_1m",
    "momentum_6m",
    "rv_30d",
)
_MIN_ROWS_PER_ASOF = 4  # 3 regressors + 1 intercept

ELIGIBLE_TRANSACTION_CODES: frozenset[str] = frozenset({"P", "S"})


class _ClassifierCache(Protocol):
    def get(self, person_cik: str, classification_year: int) -> CohenMalloyLabel: ...


def _is_nan_price(value: object) -> bool:
    return value is None or (isinstance(value, float) and np.isnan(value))


def _is_eligible_record(
    row,
    classifier_cache: _ClassifierCache,
    classification_year: int,
) -> bool:
    """Filter chain: eligible transaction code + officer/director status +
    Cohen-Malloy OPPORTUNISTIC label."""
    if row.transaction_code not in ELIGIBLE_TRANSACTION_CODES:
        return False
    if not (row.is_officer or row.is_director):
        return False
    label = classifier_cache.get(row.reporting_owner_cik, classification_year)
    return label is CohenMalloyLabel.OPPORTUNISTIC


def _resolve_price(
    row,
    price_imputer: Callable[[date], float | None] | None,
) -> float | None:
    """Return record price, imputing from ``price_imputer`` if missing.

    Returns ``None`` when price is missing AND no imputer is provided OR the
    imputer returns NaN/None. Caller treats ``None`` as drop-record.
    """
    price = row.transaction_price_per_share
    if not _is_nan_price(price):
        return price
    if price_imputer is None:
        return None
    imputed = price_imputer(row.transaction_date)
    if _is_nan_price(imputed):
        return None
    return imputed


def aggregate_opportunistic_signal(
    records: pd.DataFrame,
    *,
    asof: date,
    classifier_cache: _ClassifierCache,
    price_imputer: Callable[[date], float | None] | None = None,
) -> float:
    """Aggregate a single ticker's Form-4 records into ``net_oppor_usd_t``.

    Parameters
    ----------
    records
        DataFrame with columns at least
        ``[reporting_owner_cik, transaction_date, transaction_code,
        transaction_shares, transaction_price_per_share, is_officer,
        is_director, is_ten_percent_owner]``. Already filtered to one ticker.
    asof
        The classification date. Used to derive ``classification_year`` for
        the Cohen-Malloy classifier (``asof.year``).
    classifier_cache
        Callable-like with ``get(person_cik, year) -> CohenMalloyLabel``.
    price_imputer
        Optional callable invoked when a record's ``transaction_price_per_share``
        is null. Passed the ``transaction_date``; returns a price or ``None``.
        If ``None`` (default), records with missing prices are dropped.

    Returns
    -------
    float
        Net opportunistic USD: sum of (sign × shares × price) where sign is
        +1 for code 'P' (purchase) and -1 for code 'S' (sale). Returns 0.0
        on empty input.
    """
    if records.empty:
        return 0.0

    classification_year = asof.year

    total = 0.0
    for row in records.itertuples(index=False):
        if not _is_eligible_record(row, classifier_cache, classification_year):
            continue
        price = _resolve_price(row, price_imputer)
        if price is None:
            continue

        usd = float(row.transaction_shares) * float(price)
        sign = 1.0 if row.transaction_code == "P" else -1.0
        total += sign * usd

    return total


def score_opportunistic_form4(features: pd.DataFrame) -> pd.Series:
    """Per-asof OLS residual of ``signal_raw`` on equity controls.

    Returns a Series aligned to ``features.index`` with name ``score``. NaN
    rows (any of ``signal_raw/reversal_1m/momentum_6m/rv_30d`` missing or
    asof too small) propagate to NaN scores.

    Sign convention: high score = bullish. ``signal_raw`` directly enters the
    regression (no negation), so positive net-buy magnitude → positive
    residual after orthogonalising against equity controls.
    """
    out = pd.Series(np.nan, index=features.index, name="score", dtype=float)

    required = ("signal_raw", *EQUITY_CONTROLS_FOR_RESIDUAL)
    valid_mask = features[list(required)].notna().all(axis=1)

    for _asof, group in features.loc[valid_mask].groupby("asof", sort=False):
        if len(group) < _MIN_ROWS_PER_ASOF:
            continue
        y = group["signal_raw"].to_numpy(dtype=float)
        X = group[list(EQUITY_CONTROLS_FOR_RESIDUAL)].to_numpy(dtype=float)
        ones = np.ones((X.shape[0], 1), dtype=float)
        Xb = np.hstack([ones, X])

        beta, *_ = np.linalg.lstsq(Xb, y, rcond=None)
        residuals = y - Xb @ beta
        out.loc[group.index] = residuals

    return out
