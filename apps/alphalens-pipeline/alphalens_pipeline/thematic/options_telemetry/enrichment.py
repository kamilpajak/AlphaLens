"""Frame enricher: stamp the 16 ``options_*`` telemetry columns.

Mirrors the Buffett/O'Neil ``enrich(frame, *, asof)`` pattern (per-unique-
ticker computation, tri-state None -> NaN in float64 columns, fail-soft per
ticker). The §3.1 snapshot-window rule and first-success freeze live here.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pandas as pd

from alphalens_pipeline.thematic.options_telemetry import features as f

logger = logging.getLogger(__name__)

_FLOAT_COLUMNS: tuple[str, ...] = (
    "options_ivx30",
    "options_term_slope",
    "options_vrp_ratio",
    "options_skew_xzz",
    "options_put_vol",
    "options_call_vol",
    "options_put_oi",
    "options_call_oi",
    "options_spread_pct_atm",
    "options_atm_strike",
    "options_atm_mid",
    "options_spot",
)
_STR_COLUMNS: tuple[str, ...] = (
    "options_chain_quality",
    "options_asof_expiry_near",
    "options_snapshot_utc",
    "options_config_version",
)
OPTIONS_COLUMNS: tuple[str, ...] = _FLOAT_COLUMNS + _STR_COLUMNS

RV_SESSIONS_NEEDED = f.RV_WINDOW_RETURNS + 1  # 21 closes -> 20 returns

_DEFAULT_GROUPED_ROOT = Path.home() / ".alphalens" / "grouped_daily_history"


@dataclass(frozen=True)
class TickerSnapshot:
    """Already-fetched chain state for one ticker (no network past here)."""

    spot: float | None
    expiries: list[dt.date]
    chains: dict[dt.date, tuple[pd.DataFrame, pd.DataFrame]] = field(default_factory=dict)
    fetch_failed: bool = False


SnapshotFn = Callable[[str, dt.date], TickerSnapshot]


def stamp_window_utc(asof: dt.date, exchange: str = "XNYS") -> tuple[dt.datetime, dt.datetime]:
    """``(session_close, next_open)`` for the newest session <= ``asof``.

    Snapshots inside this window see the asof session's FINAL daily volume,
    the day's cleared OI, and at-close quotes — the only state valid to
    attribute to ``asof`` (spec §3.1).
    """
    from alphalens_pipeline.paper.calendar import (
        is_trading_day,
        next_trading_open,
        previous_trading_day,
        session_close_utc,
    )

    session = asof if is_trading_day(asof, exchange) else previous_trading_day(asof, exchange)
    close = session_close_utc(session, exchange)
    return close, next_trading_open(close, exchange)


def _null_values() -> dict[str, object]:
    values: dict[str, object] = dict.fromkeys(OPTIONS_COLUMNS)
    values["options_config_version"] = f.OPTIONS_CONFIG_VERSION
    return values


def _compute_values(
    snapshot: TickerSnapshot,
    *,
    asof: dt.date,
    now_utc: dt.datetime,
    rv20: float | None,
) -> dict[str, object]:
    values = _null_values()
    values["options_snapshot_utc"] = now_utc.isoformat()
    values["options_spot"] = snapshot.spot

    near, far = f.select_bracketing_expiries(snapshot.expiries, asof)
    term = f.select_term_expiry(snapshot.expiries, asof)
    has_chain = (
        not snapshot.fetch_failed
        and bool(snapshot.expiries)
        and (near is not None or far is not None)
    )
    if not has_chain or snapshot.spot is None:
        values["options_chain_quality"] = f.CHAIN_QUALITY_NONE
        return values

    spot = float(snapshot.spot)
    legs: list[tuple[pd.DataFrame, pd.DataFrame]] = []
    iv_near = dte_near = iv_far = dte_far = None
    quote = None
    atm_call_oi = atm_put_oi = atm_vol_total = None

    near_leg = None
    far_leg = None
    if near is not None:
        near_leg = snapshot.chains.get(near)
        if near_leg is not None:
            legs.append(near_leg)
            iv_near = f.expiry_atm_iv(near_leg[0], near_leg[1], spot)
            dte_near = (near - asof).days
    if far is not None:
        far_leg = snapshot.chains.get(far)
        if far_leg is not None:
            legs.append(far_leg)
            iv_far = f.expiry_atm_iv(far_leg[0], far_leg[1], spot)
            dte_far = (far - asof).days
    # The quote/skew/OI reference leg: near when present, else far.
    ref_leg = near_leg or far_leg

    ivx30 = f.interpolate_iv30(iv_near, dte_near, iv_far, dte_far)
    values["options_ivx30"] = ivx30
    values["options_vrp_ratio"] = f.vrp_ratio(ivx30, rv20)
    if near is not None:
        values["options_asof_expiry_near"] = near.isoformat()
    elif far is not None:
        values["options_asof_expiry_near"] = far.isoformat()

    term_leg = snapshot.chains.get(term) if term is not None else None
    if term_leg is not None and ivx30 is not None:
        iv_term = f.expiry_atm_iv(term_leg[0], term_leg[1], spot)
        if iv_term is not None:
            values["options_term_slope"] = iv_term - ivx30

    if ref_leg is not None:
        calls, puts = ref_leg
        skew = f.skew_xzz(calls, puts, spot)
        values["options_skew_xzz"] = skew
        quote = f.atm_quote(calls, puts, spot)
        if quote is not None:
            strike, mid, spread_pct = quote
            values["options_atm_strike"] = strike
            values["options_atm_mid"] = mid
            values["options_spread_pct_atm"] = spread_pct
            call_row = calls[calls["strike"] == strike]
            put_row = puts[puts["strike"] == strike]
            if not call_row.empty:
                atm_call_oi = float(call_row.iloc[0].get("openInterest") or 0)
            if not put_row.empty:
                atm_put_oi = float(put_row.iloc[0].get("openInterest") or 0)
            atm_vol_total = float(
                (0 if call_row.empty else call_row.iloc[0].get("volume") or 0)
                + (0 if put_row.empty else put_row.iloc[0].get("volume") or 0)
            )

    if legs:
        totals = f.chain_totals(legs)
        values["options_put_vol"] = totals["put_vol"]
        values["options_call_vol"] = totals["call_vol"]
        values["options_put_oi"] = totals["put_oi"]
        values["options_call_oi"] = totals["call_oi"]

    values["options_chain_quality"] = f.classify_chain_quality(
        has_chain=True,
        near=near,
        far=far,
        atm=values["options_atm_strike"],
        atm_call_oi=atm_call_oi,
        atm_put_oi=atm_put_oi,
        atm_vol_total=atm_vol_total,
        spread_pct=values["options_spread_pct_atm"],
    )
    return values


def _previous_by_ticker(previous: pd.DataFrame | None) -> dict[str, dict[str, object]]:
    """Ticker -> stamped 16-column dict from a previous same-asof output.

    Only rows with a non-null ``options_snapshot_utc`` count as stamped —
    that is the freeze marker (spec §3.1: first successful stamp wins).
    """
    if previous is None or "options_snapshot_utc" not in getattr(previous, "columns", ()):
        return {}
    stamped: dict[str, dict[str, object]] = {}
    for _, row in previous.iterrows():
        marker = row.get("options_snapshot_utc")
        if marker is None or pd.isna(marker):
            continue
        ticker = str(row.get("ticker", "")).upper()
        if ticker and ticker not in stamped:
            stamped[ticker] = {
                col: (
                    None
                    if (col in row.index and isinstance(row[col], float) and pd.isna(row[col]))
                    else row[col]
                )
                if col in row.index
                else None
                for col in OPTIONS_COLUMNS
            }
    return stamped


def _default_snapshot_fn(asof: dt.date) -> SnapshotFn:
    """Production wiring: canonical yfinance client, max 4 HTTP calls/ticker."""
    from alphalens_pipeline.data.alt_data.yfinance_client import (
        get_default_yfinance_client,
    )

    client = get_default_yfinance_client()

    def _fetch(ticker: str, asof_date: dt.date) -> TickerSnapshot:
        expiries = client.option_expiries(ticker)
        if expiries is None:
            return TickerSnapshot(spot=None, expiries=[], fetch_failed=True)
        spot = client.last_price(ticker)
        near, far = f.select_bracketing_expiries(expiries, asof_date)
        term = f.select_term_expiry(expiries, asof_date)
        chains: dict[dt.date, tuple[pd.DataFrame, pd.DataFrame]] = {}
        for expiry in {e for e in (near, far, term) if e is not None}:
            leg = client.option_chain(ticker, expiry)
            if leg is not None:
                chains[expiry] = leg
        return TickerSnapshot(spot=spot, expiries=expiries, chains=chains)

    return _fetch


def enrich(
    frame: pd.DataFrame,
    *,
    asof: dt.date,
    now_utc: dt.datetime | None = None,
    previous: pd.DataFrame | None = None,
    snapshot_fn: SnapshotFn | None = None,
    grouped_root: Path | None = None,
) -> pd.DataFrame:
    """Return ``frame`` with the 16 ``options_*`` columns appended.

    Display-only telemetry — never reads or writes any selection/sort
    column. Per-ticker fail-soft: an exception degrades that ticker to
    ``chain_quality="NONE"`` and logs, never raises.
    """
    out = frame.copy()
    tickers = [str(t).upper() for t in out["ticker"]] if "ticker" in out.columns else []
    unique = list(dict.fromkeys(tickers))

    now = now_utc or dt.datetime.now(dt.UTC)
    close, next_open = stamp_window_utc(asof)
    in_window = close < now < next_open
    frozen = _previous_by_ticker(previous)

    per_ticker: dict[str, dict[str, object]] = {}
    fetch: SnapshotFn | None = None
    rv_by_ticker: dict[str, float | None] = {}

    to_fetch = [t for t in unique if t not in frozen] if in_window else []
    if to_fetch:
        fetch = snapshot_fn or _default_snapshot_fn(asof)
        root = grouped_root or _DEFAULT_GROUPED_ROOT
        try:
            closes = f.trailing_session_closes(root, to_fetch, asof, RV_SESSIONS_NEEDED)
        except Exception:  # store missing/corrupt: RV degrades to None
            logger.warning("options telemetry: grouped store read failed", exc_info=True)
            closes = {}
        rv_by_ticker = {t: f.realized_vol_20d(closes.get(t, [])) for t in to_fetch}

    for ticker in unique:
        if ticker in frozen:
            per_ticker[ticker] = frozen[ticker]
            continue
        if not in_window:
            per_ticker[ticker] = _null_values()
            continue
        try:
            assert fetch is not None
            snapshot = fetch(ticker, asof)
            per_ticker[ticker] = _compute_values(
                snapshot, asof=asof, now_utc=now, rv20=rv_by_ticker.get(ticker)
            )
        except Exception:
            logger.warning("options telemetry failed for %s", ticker, exc_info=True)
            failed = _null_values()
            failed["options_snapshot_utc"] = now.isoformat()
            failed["options_chain_quality"] = f.CHAIN_QUALITY_NONE
            per_ticker[ticker] = failed

    for col in _FLOAT_COLUMNS:
        out[col] = pd.Series(
            [cast("float | None", per_ticker[t][col]) if t else None for t in tickers],
            index=out.index,
            dtype="float64",
        )
    for col in _STR_COLUMNS:
        out[col] = pd.Series(
            [cast("str | None", per_ticker[t][col]) if t else None for t in tickers],
            index=out.index,
            dtype="object",
        )
    return out
