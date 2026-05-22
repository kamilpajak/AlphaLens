"""Mom + low-vol composite scorer (Frazzini-Pedersen / Asness-Frazzini-Pedersen).

Scores ticker by ``z(mom_12_1m) - vol_weight * z(vol_60d)``: high
12-1-month total return AND low realised volatility. Cross-sectional
z-scores are computed per rebalance over the eligible universe (after
ADV filter), then clipped to [-3, 3] before combining.

Configurable via ``scorer_config`` (passed by ``BacktestEngine``):

  - ``benchmark`` (str)         — ticker to skip from scoring
  - ``_adv_min_usd`` (float)    — median 60d dollar-volume floor
  - ``_adv_window`` (int=60)    — ADV lookback (trading days)
  - ``_vol_weight`` (float=1.0) — weight on z(vol)
  - ``_vol_window`` (int=60)    — vol lookback (trading days)
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

_MIN_BARS = 253
_MOM_LOOKBACK = 253  # 12 months back
_MOM_SKIP = 22  # skip last month


def _passes_adv_filter(
    closes: np.ndarray, volumes: np.ndarray, adv_window: int, adv_min: float
) -> bool:
    dollar_vol = closes[-adv_window:] * volumes[-adv_window:]
    if not (dollar_vol > 0).any():
        return adv_min <= 0
    adv = float(np.median(dollar_vol[dollar_vol > 0]))
    return adv >= adv_min


def _realised_vol_ann(closes: np.ndarray, vol_window: int) -> float | None:
    """Annualised std of daily log returns over the last ``vol_window`` days.

    Returns ``None`` for windows that can't yield a finite positive vol
    (NaN/Inf in the log returns, fewer than half-window observations,
    constant-price degenerate ticker).
    """
    rets = np.diff(np.log(closes[-vol_window - 1 :]))
    if len(rets) < vol_window // 2 or np.any(~np.isfinite(rets)):
        return None
    vol = float(np.std(rets, ddof=1) * np.sqrt(252))
    if not np.isfinite(vol) or vol <= 0:
        return None
    return vol


def _score_one_ticker(
    ticker: str,
    df: pd.DataFrame,
    *,
    benchmark: str | None,
    adv_min: float,
    adv_window: int,
    vol_window: int,
) -> dict | None:
    """Return ``{ticker, mom, vol}`` for a tradable ticker, or ``None``
    if any liveness/liquidity/finite-vol filter rejects it.

    Splitting the per-ticker filtering out of the universe loop keeps
    the outer function's cognitive complexity below the Sonar S3776
    threshold and gives each filter a name worth grepping for.
    """
    if ticker == benchmark or df is None or len(df) < _MIN_BARS:
        return None
    closes = df["close"].to_numpy(dtype=float)
    volumes = df["volume"].to_numpy(dtype=float)
    if closes[-1] <= 0 or closes[-_MOM_LOOKBACK] <= 0 or closes[-_MOM_SKIP] <= 0:
        return None
    if not _passes_adv_filter(closes, volumes, adv_window, adv_min):
        return None
    vol = _realised_vol_ann(closes, vol_window)
    if vol is None:
        return None
    mom = closes[-_MOM_SKIP] / closes[-_MOM_LOOKBACK] - 1.0
    return {"ticker": ticker, "mom": float(mom), "vol": vol}


def _z_clipped(s: pd.Series) -> pd.Series:
    """Cross-sectional z-score clipped to ±3σ (zero when σ is degenerate)."""
    std = s.std(ddof=0)
    if std <= 0:
        return pd.Series(0.0, index=s.index)
    return ((s - s.mean()) / std).clip(-3.0, 3.0)


def momentum_lowvol_adapter(
    histories: Mapping[str, pd.DataFrame],
    config: Mapping | None = None,
) -> pd.DataFrame:
    """Score = z(mom_12_1m) − vol_weight × z(vol_60d), filtered by ADV."""
    cfg = dict(config or {})
    benchmark = cfg.get("benchmark")
    adv_min = float(cfg.get("_adv_min_usd", 0.0))
    adv_window = int(cfg.get("_adv_window", 60))
    vol_weight = float(cfg.get("_vol_weight", 1.0))
    vol_window = int(cfg.get("_vol_window", 60))

    rows = [
        scored
        for ticker, df in histories.items()
        if (
            scored := _score_one_ticker(
                ticker,
                df,
                benchmark=benchmark,
                adv_min=adv_min,
                adv_window=adv_window,
                vol_window=vol_window,
            )
        )
        is not None
    ]
    if not rows:
        return pd.DataFrame(columns=["ticker", "score"])

    df = pd.DataFrame(rows)
    df["z_mom"] = _z_clipped(df["mom"])
    df["z_vol"] = _z_clipped(df["vol"])
    df["score"] = df["z_mom"] - vol_weight * df["z_vol"]
    return df.sort_values("score", ascending=False).reset_index(drop=True)


momentum_lowvol_adapter.MIN_BARS_REQUIRED = _MIN_BARS
