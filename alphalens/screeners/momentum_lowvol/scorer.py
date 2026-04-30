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

    rows: list[dict] = []
    for ticker, df in histories.items():
        if ticker == benchmark or df is None or len(df) < 253:
            continue
        closes = df["close"].to_numpy(dtype=float)
        volumes = df["volume"].to_numpy(dtype=float)
        if closes[-1] <= 0 or closes[-253] <= 0 or closes[-22] <= 0:
            continue
        # ADV filter
        dollar_vol = closes[-adv_window:] * volumes[-adv_window:]
        adv = float(np.median(dollar_vol[dollar_vol > 0])) if (dollar_vol > 0).any() else 0.0
        if adv < adv_min:
            continue
        # 12-1m momentum
        mom = closes[-22] / closes[-253] - 1.0
        # 60d realized vol (annualized std of daily log returns)
        rets = np.diff(np.log(closes[-vol_window - 1 :]))
        if len(rets) < vol_window // 2 or np.any(~np.isfinite(rets)):
            continue
        vol = float(np.std(rets, ddof=1) * np.sqrt(252))
        if not np.isfinite(vol) or vol <= 0:
            continue
        rows.append({"ticker": ticker, "mom": float(mom), "vol": vol})

    if not rows:
        return pd.DataFrame(columns=["ticker", "score"])

    df = pd.DataFrame(rows)
    for col in ("mom", "vol"):
        std = df[col].std(ddof=0)
        if std <= 0:
            df[f"z_{col}"] = 0.0
            continue
        z = (df[col] - df[col].mean()) / std
        df[f"z_{col}"] = z.clip(-3.0, 3.0)
    df["score"] = df["z_mom"] - vol_weight * df["z_vol"]
    return df.sort_values("score", ascending=False).reset_index(drop=True)


momentum_lowvol_adapter.MIN_BARS_REQUIRED = 253
