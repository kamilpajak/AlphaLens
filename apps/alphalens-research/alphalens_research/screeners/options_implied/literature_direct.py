"""v8 literature-direct scorer — model-free Xing 2010 replication.

Pre-registered as `v8_literature_direct_options_implied_2026_05_03` per
`docs/research/preregistration/params_v8_literature_direct_options_implied_2026_05_03.json`.

Built after v7 FAIL'd a 5-phase multi-phase audit on 2026-05-02. v7's Lasso
fit on 2018-2024 train learned POSITIVE coefficients on `ivx30`/`ivp30`
across all 5 phases — opposite of the NEGATIVE-sign Xing 2010 prior
committed ex-ante. The L/S decile spread came in at αt = -2.78, confirming
the Xing direction IS empirically present in the 2024-2026 holdout but the
train-fitted model was on the wrong side of the cross-section.

v8 removes the optimizer entirely:

  score(asof, ticker) = -features.loc[(asof, ticker), "ivp30"]

So `top decile by score` = `bottom decile by ivp30` = LOW-IV names = LONG
leg per Xing 2010 / Bali-Hovakimian 2009. No fit, no sign-flip surface.

Per perplexity adversarial review (Sonar Reasoning Pro 2026-05-03), this is
the highest-defensibility v8 redesign axis: it tests the literature
hypothesis deterministically without introducing a tunable optimizer that
can violate the prior under regime shift.
"""

from __future__ import annotations

import pandas as pd


def score_literature_direct(features: pd.DataFrame) -> pd.Series:
    """Return a per-row score = `-features["ivp30"]`.

    Series index is preserved so the scorer plugs into the same
    `_portfolio_returns` path used by v7 (which assigns the score onto the
    feature frame and sorts descending to pick the long leg).

    NaN `ivp30` propagates to NaN score (no silent zero-fill — downstream
    `dropna(subset=["_score"])` excludes the row from the cross-section).

    Raises
    ------
    KeyError
        If `features` does not contain an `ivp30` column. v7's
        `build_feature_frame` always emits `ivp30`; raising here surfaces
        upstream schema regressions immediately rather than scoring all
        rows as 0 by accident.
    """
    return (-features["ivp30"]).astype(float).rename("score")
