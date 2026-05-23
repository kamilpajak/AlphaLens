"""Cross-sectional scoring helpers shared by Layer 2 screeners.

These primitives are pure-function utilities used by multiple screener
modules (currently ``ev_fcff_yield``, ``idiosyncratic_momentum``). Extracted
here to avoid duplication that triggers SonarCloud's duplicated-lines
quality gate while keeping each screener module otherwise independent of
its siblings.

Sign / index conventions: NaN rows propagate unchanged through both
functions. Empty / constant input returns an all-NaN Series.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def winsorize(
    series: pd.Series,
    *,
    lower_pct: float = 0.01,
    upper_pct: float = 0.99,
) -> pd.Series:
    """Cap values at the ``[lower_pct, upper_pct]`` percentile range.

    NaN values are preserved unchanged. Empty input returns empty.
    """
    if series.empty:
        return series
    valid = series.dropna()
    if valid.empty:
        return series
    lo = float(valid.quantile(lower_pct))
    hi = float(valid.quantile(upper_pct))
    return series.clip(lower=lo, upper=hi)


def rank_zscore(series: pd.Series) -> pd.Series:
    """Cross-sectional z-score (population std-dev, ``ddof=0``).

    NaN rows propagate. Empty / constant input returns all-NaN.
    """
    if series.empty:
        return series
    valid = series.dropna()
    if valid.empty:
        return pd.Series(np.nan, index=series.index)
    mean = float(valid.mean())
    std = float(valid.std(ddof=0))
    if not np.isfinite(std) or std == 0:
        return pd.Series(np.nan, index=series.index)
    return (series - mean) / std
