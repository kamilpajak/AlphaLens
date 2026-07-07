"""Pure feature construction over already-fetched option-chain frames.

No network in this module: every function takes yfinance-shaped chain
DataFrames (per-contract ``strike``, ``bid``, ``ask``, ``impliedVolatility``,
``openInterest``, ``volume``) plus scalars. The vendor IV field has
documented bugs (stale/zero-bid inversions), so every IV passes
:func:`sane_iv` before use — an insane leg degrades, never propagates.
"""

from __future__ import annotations

import datetime as dt
import math

import pandas as pd

OPTIONS_CONFIG_VERSION = "options-telemetry-v1-yf-snapshot"

IV_SANITY_MIN = 0.01
IV_SANITY_MAX = 5.0
ATM_MIN_OI = 50
ATM_MAX_SPREAD_PCT = 0.10
NEAR_LEG_MIN_DTE = 7
IV30_TARGET_DTE = 30
TERM_LEG_DTE_BAND = (120, 270)
TERM_LEG_TARGET_DTE = 180
SKEW_OTM_PUT_MONEYNESS = (0.80, 0.95)
SKEW_ATM_CALL_MONEYNESS = (0.95, 1.05)

CHAIN_QUALITY_NONE = "NONE"
CHAIN_QUALITY_THIN = "THIN"
CHAIN_QUALITY_OK = "OK"


def sane_iv(value: float | None) -> bool:
    """True when the vendor IV is inside the plausibility band."""
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if math.isnan(v):
        return False
    return IV_SANITY_MIN <= v <= IV_SANITY_MAX


def select_bracketing_expiries(
    expiries: list[dt.date], asof: dt.date
) -> tuple[dt.date | None, dt.date | None]:
    """``(near, far)`` legs bracketing 30 DTE.

    ``near`` is the latest expiry with ``NEAR_LEG_MIN_DTE <= dte <= 30``
    (sub-7-DTE legs are gamma-week noise, skipped). ``far`` is the first
    expiry strictly past 30 DTE. Either can be ``None``.
    """
    near = None
    far = None
    for e in sorted(expiries):
        dte = (e - asof).days
        if NEAR_LEG_MIN_DTE <= dte <= IV30_TARGET_DTE:
            near = e
        elif dte > IV30_TARGET_DTE and far is None:
            far = e
    return near, far


def select_term_expiry(expiries: list[dt.date], asof: dt.date) -> dt.date | None:
    """The expiry with DTE closest to 180 inside ``TERM_LEG_DTE_BAND``."""
    lo, hi = TERM_LEG_DTE_BAND
    in_band = [e for e in expiries if lo <= (e - asof).days <= hi]
    if not in_band:
        return None
    return min(in_band, key=lambda e: abs((e - asof).days - TERM_LEG_TARGET_DTE))


def _row_at_strike(frame: pd.DataFrame, strike: float) -> pd.Series | None:
    if frame is None or frame.empty or "strike" not in frame.columns:
        return None
    hits = frame[frame["strike"] == strike]
    if hits.empty:
        return None
    return hits.iloc[0]


def atm_strike(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> float | None:
    """Nearest-to-spot strike listed on BOTH legs (midpoint IV needs both)."""
    if calls is None or puts is None or calls.empty or puts.empty:
        return None
    common = set(calls["strike"]) & set(puts["strike"])
    if not common:
        return None
    return min(common, key=lambda k: abs(float(k) - spot))


def expiry_atm_iv(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> float | None:
    """Midpoint of the sane call/put IVs at the ATM strike.

    One insane leg degrades to the other; both insane -> ``None``.
    """
    strike = atm_strike(calls, puts, spot)
    if strike is None:
        return None
    legs = []
    for frame in (calls, puts):
        row = _row_at_strike(frame, strike)
        if row is not None and sane_iv(row.get("impliedVolatility")):
            legs.append(float(row["impliedVolatility"]))
    if not legs:
        return None
    return sum(legs) / len(legs)


def interpolate_iv30(
    iv_near: float | None,
    dte_near: int | None,
    iv_far: float | None,
    dte_far: int | None,
) -> float | None:
    """Linear-in-DTE interpolation to 30 DTE; a single leg is used flat.

    Telemetry-grade simplification (spec §4 / review §8): NOT a traded
    curve — the audit columns keep it recomputable at analysis time.
    """
    have_near = iv_near is not None and dte_near is not None
    have_far = iv_far is not None and dte_far is not None
    if have_near and have_far:
        if dte_far == dte_near:
            return iv_near
        w = (IV30_TARGET_DTE - dte_near) / (dte_far - dte_near)
        return iv_near + w * (iv_far - iv_near)
    if have_near:
        return iv_near
    if have_far:
        return iv_far
    return None


def _pick_in_moneyness(
    frame: pd.DataFrame, spot: float, window: tuple[float, float], anchor: float
) -> float | None:
    """Sane IV of the contract whose K/S is inside ``window``, closest to ``anchor``."""
    if frame is None or frame.empty:
        return None
    lo, hi = window
    best_iv = None
    best_dist = None
    for _, row in frame.iterrows():
        strike = float(row["strike"])
        m = strike / spot
        if not (lo <= m <= hi) or not sane_iv(row.get("impliedVolatility")):
            continue
        dist = abs(m - anchor)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_iv = float(row["impliedVolatility"])
    return best_iv


def skew_xzz(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> float | None:
    """Xing-Zhang-Zhao smirk: OTM-put IV minus ATM-call IV (moneyness-based)."""
    otm_put = _pick_in_moneyness(
        puts, spot, SKEW_OTM_PUT_MONEYNESS, anchor=SKEW_OTM_PUT_MONEYNESS[1]
    )
    atm_call = _pick_in_moneyness(calls, spot, SKEW_ATM_CALL_MONEYNESS, anchor=1.0)
    if otm_put is None or atm_call is None:
        return None
    return otm_put - atm_call


def _usable_quote(row: pd.Series | None) -> tuple[float, float] | None:
    """``(mid, spread_pct)`` from a contract row, or ``None`` when untradable."""
    if row is None:
        return None
    try:
        bid = float(row.get("bid"))
        ask = float(row.get("ask"))
    except (TypeError, ValueError):
        return None
    if math.isnan(bid) or math.isnan(ask) or bid <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return mid, (ask - bid) / mid


def atm_quote(
    calls: pd.DataFrame, puts: pd.DataFrame, spot: float
) -> tuple[float, float, float] | None:
    """``(strike, mid, spread_pct)`` at the ATM strike — call leg first,
    put leg as fallback; ``None`` when both quotes are unusable."""
    strike = atm_strike(calls, puts, spot)
    if strike is None:
        return None
    for frame in (calls, puts):
        quote = _usable_quote(_row_at_strike(frame, strike))
        if quote is not None:
            mid, spread_pct = quote
            return float(strike), mid, spread_pct
    return None


def chain_totals(legs: list[tuple[pd.DataFrame, pd.DataFrame]]) -> dict[str, float]:
    """Raw put/call volume + OI summed over the bracketing expiries.

    These are the *ingredients* of the abnormal-P/C construction — the raw
    P/C ratio itself is a validated null (Pan-Poteshman) and is deliberately
    not a column.
    """

    def _sum(frame: pd.DataFrame, col: str) -> float:
        if frame is None or frame.empty or col not in frame.columns:
            return 0.0
        return float(pd.to_numeric(frame[col], errors="coerce").fillna(0).sum())

    totals = {"call_vol": 0.0, "put_vol": 0.0, "call_oi": 0.0, "put_oi": 0.0}
    for calls, puts in legs:
        totals["call_vol"] += _sum(calls, "volume")
        totals["put_vol"] += _sum(puts, "volume")
        totals["call_oi"] += _sum(calls, "openInterest")
        totals["put_oi"] += _sum(puts, "openInterest")
    return totals


def classify_chain_quality(
    *,
    has_chain: bool,
    near: dt.date | None,
    far: dt.date | None,
    atm: float | None,
    atm_call_oi: float | None,
    atm_put_oi: float | None,
    atm_vol_total: float | None,
    spread_pct: float | None,
) -> str:
    """Spec §4 pinned dimensions: NONE / THIN / OK.

    OK needs both bracketing expiries, an ATM strike on both legs, per-leg
    OI >= ATM_MIN_OI, non-zero ATM volume on the asof session, and an ATM
    relative spread <= ATM_MAX_SPREAD_PCT. Anything less (but with a chain
    present) is THIN.
    """
    if not has_chain:
        return CHAIN_QUALITY_NONE
    if near is None or far is None or atm is None:
        return CHAIN_QUALITY_THIN
    if atm_call_oi is None or atm_call_oi < ATM_MIN_OI:
        return CHAIN_QUALITY_THIN
    if atm_put_oi is None or atm_put_oi < ATM_MIN_OI:
        return CHAIN_QUALITY_THIN
    if atm_vol_total is None or atm_vol_total <= 0:
        return CHAIN_QUALITY_THIN
    if spread_pct is None or spread_pct > ATM_MAX_SPREAD_PCT:
        return CHAIN_QUALITY_THIN
    return CHAIN_QUALITY_OK
