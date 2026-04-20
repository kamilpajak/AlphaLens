"""Effective bid-ask spread estimators from daily OHLC data.

Primary estimator: **EDGE** (Ardia, Guidotti, Kroencke, JFE 2024, "Efficient
Estimation of Bid-Ask Spreads from Open, High, Low, and Close Prices"). It is
roughly 2x more accurate than Abdi-Ranaldo (2017) and Corwin-Schultz (2012) on
published benchmarks, and reliably estimates spreads as small as 0.10%. The
single-period and rolling implementations are ported from the authors' MIT-
licensed `bidask` Python package (https://github.com/eguidotti/bidask).

AR (2017) and CS (2012) ship as fallback sanity-check estimators. Roll (1984)
is intentionally omitted — it degenerates to ~40% zeros in practice and adds
no value as a fallback.

All functions return spreads in **decimal units** (0.02 = 2%). Negative
single-day estimates are clipped to 0 **before** any rolling aggregation, per
guidance in Corwin-Schultz follow-up work (excluding negatives prior to daily
averaging produces estimates closest to TAQ effective spreads).
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd


_SQRT_2 = np.sqrt(2.0)
_CS_DENOM = 3.0 - 2.0 * _SQRT_2  # == 3 - 2*sqrt(2), used in CS alpha formula


# ---------------------------------------------------------------------------
# EDGE — Ardia, Guidotti, Kroencke (JFE, 2024)
# ---------------------------------------------------------------------------


def edge_spread_single(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
) -> float:
    """Full-period EDGE spread estimate for one OHLC sample.

    Returns spread in decimal units (0.01 = 1%). NaN if fewer than 3
    observations, constant prices, or degenerate probabilities.
    """
    nobs = len(open_)
    if not (len(high) == nobs and len(low) == nobs and len(close) == nobs):
        raise ValueError("Open, high, low, and close must have the same length.")
    if nobs < 3:
        return float("nan")

    o = np.log(np.asarray(open_, dtype=float))
    h = np.log(np.asarray(high, dtype=float))
    l = np.log(np.asarray(low, dtype=float))
    c = np.log(np.asarray(close, dtype=float))
    m = (h + l) / 2.0

    h1, l1, c1, m1 = h[:-1], l[:-1], c[:-1], m[:-1]
    o, h, l, c, m = o[1:], h[1:], l[1:], c[1:], m[1:]

    r1 = m - o
    r2 = o - m1
    r3 = m - c1
    r4 = c1 - m1
    r5 = o - c1

    tau = np.where(np.isnan(h) | np.isnan(l) | np.isnan(c1), np.nan, (h != l) | (l != c1))
    po1 = tau * np.where(np.isnan(o) | np.isnan(h), np.nan, o != h)
    po2 = tau * np.where(np.isnan(o) | np.isnan(l), np.nan, o != l)
    pc1 = tau * np.where(np.isnan(c1) | np.isnan(h1), np.nan, c1 != h1)
    pc2 = tau * np.where(np.isnan(c1) | np.isnan(l1), np.nan, c1 != l1)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        pt = np.nanmean(tau)
        po = np.nanmean(po1) + np.nanmean(po2)
        pc = np.nanmean(pc1) + np.nanmean(pc2)

        if np.nansum(tau) < 2 or po == 0 or pc == 0 or np.isnan(pt):
            return float("nan")

        d1 = r1 - np.nanmean(r1) / pt * tau
        d3 = r3 - np.nanmean(r3) / pt * tau
        d5 = r5 - np.nanmean(r5) / pt * tau

        x1 = -4.0 / po * d1 * r2 + -4.0 / pc * d3 * r4
        x2 = -4.0 / po * d1 * r5 + -4.0 / pc * d5 * r4

        e1 = np.nanmean(x1)
        e2 = np.nanmean(x2)
        v1 = np.nanmean(x1 ** 2) - e1 ** 2
        v2 = np.nanmean(x2 ** 2) - e2 ** 2

    vt = v1 + v2
    s2 = (v2 * e1 + v1 * e2) / vt if vt > 0 else (e1 + e2) / 2.0
    s = float(np.sqrt(np.abs(s2)))
    return s


def edge_spread(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int = 21,
    min_periods: int | None = None,
) -> pd.Series:
    """Rolling EDGE spread estimate. Returns pd.Series aligned with input index.

    Implementation ported from `bidask.edge_rolling` — vectorised rolling mean
    over 34 moment accumulators rather than naive per-window `edge_spread_single`
    call (which would be O(n*window)). Accuracy-equivalent.

    The estimator is always non-negative (sqrt(abs(s2))), so no explicit
    negative-clipping step is needed for EDGE specifically — AR and CS do.
    """
    if min_periods is None:
        min_periods = window

    df = pd.DataFrame(
        {
            "open": np.log(open_.astype(float)),
            "high": np.log(high.astype(float)),
            "low": np.log(low.astype(float)),
            "close": np.log(close.astype(float)),
        }
    )
    o = df["open"]
    h = df["high"]
    l = df["low"]
    c = df["close"]
    m = (h + l) / 2.0

    h1 = h.shift(1)
    l1 = l.shift(1)
    c1 = c.shift(1)
    m1 = m.shift(1)

    r1 = m - o
    r2 = o - m1
    r3 = m - c1
    r4 = c1 - m1
    r5 = o - c1

    tau = np.where(
        h.isna() | l.isna() | c1.isna(), np.nan, (h != l) | (l != c1)
    )
    po1 = tau * np.where(o.isna() | h.isna(), np.nan, o != h)
    po2 = tau * np.where(o.isna() | l.isna(), np.nan, o != l)
    pc1 = tau * np.where(c1.isna() | h1.isna(), np.nan, c1 != h1)
    pc2 = tau * np.where(c1.isna() | l1.isna(), np.nan, c1 != l1)

    r12 = r1 * r2
    r15 = r1 * r5
    r34 = r3 * r4
    r45 = r4 * r5
    tr1 = tau * r1
    tr2 = tau * r2
    tr4 = tau * r4
    tr5 = tau * r5

    accum = pd.DataFrame(
        {
            1: r12,
            2: r34,
            3: r15,
            4: r45,
            5: tau,
            6: r1,
            7: tr2,
            8: r3,
            9: tr4,
            10: r5,
            11: r12 ** 2,
            12: r34 ** 2,
            13: r15 ** 2,
            14: r45 ** 2,
            15: r12 * r34,
            16: r15 * r45,
            17: tr2 * r2,
            18: tr4 * r4,
            19: tr5 * r5,
            20: tr2 * r12,
            21: tr4 * r34,
            22: tr5 * r15,
            23: tr4 * r45,
            24: tr4 * r12,
            25: tr2 * r34,
            26: tr2 * r4,
            27: tr1 * r45,
            28: tr5 * r45,
            29: tr4 * r5,
            30: tr5,
            31: po1,
            32: po2,
            33: pc1,
            34: pc2,
        },
        index=df.index,
    )
    accum.iloc[0] = np.nan

    # First bar has no t-1 companion, so window and min_periods get -1 to
    # align with the shifted series.
    w = max(0, int(window) - 1)
    mp = max(0, int(min_periods) - 1)
    roll_mean = accum.rolling(window=w, min_periods=mp).mean()

    pt = roll_mean[5]
    po = roll_mean[31] + roll_mean[32]
    pc = roll_mean[33] + roll_mean[34]

    nt = accum[5].rolling(window=w, min_periods=mp).sum()
    bad_mask = (nt < 2) | (po == 0) | (pc == 0)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        a1 = -4.0 / po
        a2 = -4.0 / pc
        a3 = roll_mean[6] / pt
        a4 = roll_mean[9] / pt
        a5 = roll_mean[8] / pt
        a6 = roll_mean[10] / pt
        a12 = 2 * a1 * a2
        a11 = a1 ** 2
        a22 = a2 ** 2
        a33 = a3 ** 2
        a55 = a5 ** 2
        a66 = a6 ** 2

        e1 = a1 * (roll_mean[1] - a3 * roll_mean[7]) + a2 * (roll_mean[2] - a4 * roll_mean[8])
        e2 = a1 * (roll_mean[3] - a3 * roll_mean[30]) + a2 * (roll_mean[4] - a4 * roll_mean[10])

        v1 = -(e1 ** 2) + (
            a11 * (roll_mean[11] - 2 * a3 * roll_mean[20] + a33 * roll_mean[17])
            + a22 * (roll_mean[12] - 2 * a5 * roll_mean[21] + a55 * roll_mean[18])
            + a12 * (roll_mean[15] - a3 * roll_mean[25] - a5 * roll_mean[24] + a3 * a5 * roll_mean[26])
        )
        v2 = -(e2 ** 2) + (
            a11 * (roll_mean[13] - 2 * a3 * roll_mean[22] + a33 * roll_mean[19])
            + a22 * (roll_mean[14] - 2 * a6 * roll_mean[23] + a66 * roll_mean[18])
            + a12 * (roll_mean[16] - a3 * roll_mean[28] - a6 * roll_mean[27] + a3 * a6 * roll_mean[29])
        )

    vt = v1 + v2
    s2 = pd.Series.where(
        cond=vt > 0,
        self=(v2 * e1 + v1 * e2) / vt,
        other=(e1 + e2) / 2.0,
    )
    s2 = s2.mask(bad_mask)
    s = np.sqrt(np.abs(s2))
    s.name = "edge_spread"
    return s


# ---------------------------------------------------------------------------
# Abdi-Ranaldo (2017) — fallback
# ---------------------------------------------------------------------------


def abdi_ranaldo_spread(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    window: int = 21,
    min_periods: int | None = None,
) -> pd.Series:
    """Abdi-Ranaldo (2017) effective spread estimator.

    Formula: S ≈ 2 * sqrt(max(0, -E[(c_t - m_t)(c_t - m_{t+1})])) where
    m_t = (h_t + l_t)/2 is the daily midrange in log-prices.

    Negative single-day contributions are clipped to 0 before the rolling
    average per Corwin-Schultz follow-up guidance.
    """
    if min_periods is None:
        min_periods = window

    lh = np.log(high.astype(float))
    ll = np.log(low.astype(float))
    lc = np.log(close.astype(float))
    m = (lh + ll) / 2.0

    term = (lc - m) * (lc - m.shift(-1))
    # Clip negative single-day terms to 0 before aggregation.
    contrib = (-term).clip(lower=0.0)
    rolling_mean = contrib.rolling(window=window, min_periods=min_periods).mean()
    s = 2.0 * np.sqrt(rolling_mean)
    s.name = "abdi_ranaldo_spread"
    return s


# ---------------------------------------------------------------------------
# Corwin-Schultz (2012) — fallback
# ---------------------------------------------------------------------------


def corwin_schultz_spread(
    high: pd.Series,
    low: pd.Series,
    window: int = 21,
    min_periods: int | None = None,
) -> pd.Series:
    """Corwin-Schultz (2012) high-low effective spread estimator.

    For each pair of consecutive days:
      β = ln(H_t/L_t)^2 + ln(H_{t+1}/L_{t+1})^2
      γ = ln(max(H_t,H_{t+1})/min(L_t,L_{t+1}))^2
      α = (sqrt(2β) - sqrt(β)) / (3 - 2*sqrt(2)) - sqrt(γ / (3 - 2*sqrt(2)))
      S = 2 * (e^α - 1) / (1 + e^α)

    Negative single-day spreads are clipped to 0 before rolling averaging.
    """
    if min_periods is None:
        min_periods = window

    h = high.astype(float)
    l = low.astype(float)
    h_next = h.shift(-1)
    l_next = l.shift(-1)

    with np.errstate(divide="ignore", invalid="ignore"):
        beta = np.log(h / l) ** 2 + np.log(h_next / l_next) ** 2
        combined_h = np.maximum(h, h_next)
        combined_l = np.minimum(l, l_next)
        gamma = np.log(combined_h / combined_l) ** 2
        alpha = (np.sqrt(2.0 * beta) - np.sqrt(beta)) / _CS_DENOM - np.sqrt(
            gamma / _CS_DENOM
        )
        s_daily = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))

    # Clip to non-negative before rolling aggregation.
    s_daily = pd.Series(s_daily, index=h.index).clip(lower=0.0)
    s = s_daily.rolling(window=window, min_periods=min_periods).mean()
    s.name = "corwin_schultz_spread"
    return s
