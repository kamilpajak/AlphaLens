"""Equal-weight z-score average compound (memo Section 3.1).

Locked formula: per-asof, cross-sectionally z-score each component
independently (ddof=1, no clipping), then average over the strict
intersection of tickers (BOTH components produce a finite score).

Degenerate handling (per zen review 2026-05-10):
  When a component has sigma=0 (all valid scores identical) or n<2 valid
  observations, it returns zeros for finite tickers. Returning all-NaN
  would propagate through the strict intersection and discard the asof,
  which would silently throw away the OTHER component's signal. Zero
  ("neutral cross-sectional preference") preserves the asof and lets the
  surviving component drive selection.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _xsec_zscore(s: pd.Series) -> pd.Series:
    s = s.astype(float)
    out = pd.Series(np.nan, index=s.index, dtype=float)
    finite_mask = np.isfinite(s)
    valid = s[finite_mask]
    if len(valid) < 2:
        out.loc[finite_mask] = 0.0
        return out
    sigma = valid.std(ddof=1)
    if not np.isfinite(sigma) or sigma == 0:
        out.loc[finite_mask] = 0.0
        return out
    out.loc[finite_mask] = (valid - valid.mean()) / sigma
    return out


def compound_score_from_components(
    form4_scores: pd.Series,
    pc_scores: pd.Series,
) -> pd.Series:
    """Equal-weight z-score average on the strict intersection of tickers."""
    z_form4 = _xsec_zscore(form4_scores)
    z_pc = _xsec_zscore(pc_scores)
    common = z_form4.dropna().index.intersection(z_pc.dropna().index)
    if len(common) == 0:
        return pd.Series(dtype=float, name="score")
    out = (z_form4.loc[common] + z_pc.loc[common]) / 2.0
    out.name = "score"
    return out
