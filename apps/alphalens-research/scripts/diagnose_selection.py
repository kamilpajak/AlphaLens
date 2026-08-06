#!/usr/bin/env python
"""Fixed-horizon CAR (selection) + Kaplan-Meier survival-fill (entry) diagnostic.

Read-only, research-side. Reads the same three ~/.alphalens parquet stores as
diagnose_nofill.py. Selection = market-adjusted BHAR over fixed k-session
windows from the event (complete-window-only); entry = time-to-touch-E1
survival with right-censoring at the entry TTL. Telemetry-only.

Every event is scored TWICE and both columns are written: ``car_<k>`` subtracts
the market return one-for-one (beta = 1, the historical form) and
``car_mm_<k>`` subtracts the event's own estimated beta times the market
return. The per-event ``beta`` / ``beta_source`` / ``beta_n_obs`` columns say
where that beta came from. Re-running this script over the stored parquets is
the retrospective pass -- it re-scores history, it does not restart it.

Confidence intervals resample WHOLE BRIEF DATES, because the events on one
brief date share a market move and are not independent draws.

    .venv/bin/python apps/alphalens-research/scripts/diagnose_selection.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
from pathlib import Path
from typing import NamedTuple

import pandas as pd
from alphalens_pipeline.data import rs_history
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    previous_trading_day,
    session_on_or_after,
)
from alphalens_research.diagnostics import anchor as anchor_mod
from alphalens_research.diagnostics import edge_stores, fill_survival, fixed_horizon, nofill
from broker_contract.constants import DEFAULT_ORDER_TTL_DAYS

_SPY = "SPY"
_FILLED = {"OPEN", "PARTIAL_TP_OPEN", "TP_FULL", "SL_HIT"}
_SESSION_CACHE_ENTRIES = 4096  # one per distinct (anchor, window, exchange); ~1 per brief date


def _close(snapshot: dict | None, ticker: str) -> float | None:
    if not snapshot:
        return None
    bar = snapshot.get(ticker.upper())
    if not bar:
        return None
    try:
        c = float(bar["c"])
    except (KeyError, TypeError, ValueError):
        return None
    return c if c > 0.0 else None


def _low(snapshot: dict | None, ticker: str) -> float | None:
    if not snapshot:
        return None
    bar = snapshot.get(ticker.upper())
    if not bar:
        return None
    try:
        return float(bar["l"])
    except (KeyError, TypeError, ValueError):
        return None


def _open(snapshot: dict | None, ticker: str) -> float | None:
    if not snapshot:
        return None
    bar = snapshot.get(ticker.upper())
    if not bar:
        return None
    try:
        o = float(bar["o"])
    except (KeyError, TypeError, ValueError):
        return None
    return o if o > 0.0 else None


@functools.lru_cache(maxsize=_SESSION_CACHE_ENTRIES)
def _pre_event_sessions(anchor: dt.date, window: int, exchange: str) -> tuple[dt.date, ...]:
    """The ``window`` sessions ending at (and including) ``anchor``, oldest first.

    Memoized because every event on a given brief date shares one anchor, and
    walking the exchange calendar back 60 sessions per event otherwise dominates
    the run. One entry per distinct brief date, so the bound is generous.

    The window ends at the anchor, which is the session BEFORE arrival, so it
    never contains the arrival move. It can still contain earlier drift on the
    same catalyst: briefs are dated T-1 and a news item may be older than the
    brief that carries it. Event studies usually gap the estimation window for
    this reason. No gap is applied here because its length would be an invented
    constant -- ``beta_source`` and the CAR columns this script now stores are
    what a later pass needs to choose one from data.
    """
    sessions = [anchor]
    for _ in range(window - 1):
        sessions.append(previous_trading_day(sessions[-1], exchange))
    return tuple(reversed(sessions))


def _pre_event_closes(
    grouped,
    anchor: dt.date,
    ticker: str,
    *,
    window: int,
    exchange: str,
) -> tuple[list[float | None], list[float | None]]:
    """Aligned ``(stock, market)`` close series over the pre-event window.

    A session the store cannot price yields ``None`` in place rather than a
    shorter list, so the two series stay index-aligned and the beta estimator
    can drop exactly the returns that span the hole.
    """
    sessions = _pre_event_sessions(anchor, window, exchange)
    stock: list[float | None] = []
    market: list[float | None] = []
    for s in sessions:
        snapshot = grouped.get(s)
        stock.append(_close(snapshot, ticker))
        market.append(_close(snapshot, _SPY))
    return stock, market


_BETA_FALLBACK_SOURCES = (
    fixed_horizon.BETA_FALLBACK_THIN,
    fixed_horizon.BETA_FALLBACK_DEGENERATE,
)


class _BetaCounts(NamedTuple):
    """How the per-event beta column came out, in buckets that partition it."""

    estimated: int
    fell_back: int
    not_attempted: int
    unexpected: int


def _beta_counts(sources: pd.Series) -> _BetaCounts:
    """Tally a ``beta_source`` column.

    A null means the estimate was never attempted -- the event had no elapsed
    CAR window, or no priceable anchor -- and its ``beta`` is ``None``, not 1.0.
    That is a different statement from an attempt that fell back, so the two are
    counted apart.

    Fallbacks are counted by naming the tags rather than by subtraction, so a
    ``beta_source`` value added later cannot quietly inflate them. Anything this
    helper does not recognise lands in ``unexpected``, which the caller prints
    when it is non-zero -- visible, without aborting a long diagnostic run at
    its last line.
    """
    estimated = int((sources == fixed_horizon.BETA_ESTIMATED).sum())
    fell_back = int(sources.isin(_BETA_FALLBACK_SOURCES).sum())
    not_attempted = int(sources.isna().sum())
    return _BetaCounts(
        estimated=estimated,
        fell_back=fell_back,
        not_attempted=not_attempted,
        unexpected=len(sources) - estimated - fell_back - not_attempted,
    )


def _e1(setup: dict | None) -> float | None:
    if not setup or setup.get("status") != "OK":
        return None
    tiers = setup.get("entry_tiers") or []
    if not tiers:
        return None
    try:
        return float(tiers[0]["limit"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ladders-dir", type=Path, default=edge_stores.HOME / "population_ladders")
    ap.add_argument("--briefs-dir", type=Path, default=edge_stores.HOME / "thematic_briefs")
    ap.add_argument("--grouped-root", type=Path, default=rs_history.DEFAULT_RS_HISTORY_ROOT)
    ap.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    ap.add_argument("--ttl", type=int, default=DEFAULT_ORDER_TTL_DAYS)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--anchor",
        choices=anchor_mod.ANCHOR_MODES,
        default=anchor_mod.ANCHOR_PRIOR_CLOSE,
        help="CAR anchor: prior_close (legacy) or arrival_vwap (price an arrival entry pays)",
    )
    ap.add_argument(
        "--beta-window",
        type=int,
        default=fixed_horizon.DEFAULT_BETA_WINDOW,
        help="pre-event sessions used to estimate each event's beta vs SPY",
    )
    ap.add_argument(
        "--out", type=Path, default=edge_stores.HOME / "diagnostics" / "selection.parquet"
    )
    args = ap.parse_args()

    outcomes = edge_stores.load_store(args.ladders_dir)
    if outcomes.empty or "plannable" not in outcomes.columns:
        print("no plannable population-ladder outcomes at", args.ladders_dir)
        return
    setups = edge_stores.setup_index(args.briefs_dir)
    grouped = edge_stores.GroupedDailyCache(args.grouped_root)
    newest = edge_stores.newest_session(args.grouped_root)
    if newest is None:
        print("empty grouped-daily store at", args.grouped_root)
        return

    plannable = outcomes[outcomes["plannable"] == True].copy()  # noqa: E712

    # Per-event CAR at each k (complete-window-only) + fill duration/censoring.
    records: list[dict] = []
    for _, row in plannable.iterrows():
        brief_date = row["brief_date"]
        ticker = str(row["ticker"]).upper()
        classification = str(row.get("ladder_classification") or "")
        arrival = session_on_or_after(brief_date, args.exchange)
        anchor_session = previous_trading_day(arrival, args.exchange)
        prior_close_stock = _close(grouped.get(anchor_session), ticker)
        prior_close_spy = _close(grouped.get(anchor_session), _SPY)
        # reference_close is the arrival 30-min VWAP (the price an arrival entry pays).
        # Coerce missing / NaN to None so car_for_event treats it as an incomputable window.
        ref_close = row.get("reference_close")
        arrival_vwap_stock = (
            float(ref_close) if ref_close is not None and not pd.isna(ref_close) else None
        )
        if arrival_vwap_stock is not None and pd.isna(arrival_vwap_stock):
            arrival_vwap_stock = None
        # SPY arrival OPEN is the closest single-price market proxy at the start of the entry
        # window; first-session drift vs the stock's 30-min VWAP is negligible over the CAR.
        arrival_open_spy = _open(grouped.get(arrival), _SPY)
        a_stock, a_spy = anchor_mod.event_anchor(
            args.anchor,
            prior_close_stock=prior_close_stock,
            prior_close_spy=prior_close_spy,
            arrival_vwap_stock=arrival_vwap_stock,
            arrival_open_spy=arrival_open_spy,
        )

        horizons = {
            k: advance_trading_sessions(arrival, k - 1, args.exchange)
            for k in fixed_horizon.K_WINDOWS
        }
        # Estimating beta walks the grouped store back a whole window, so skip it entirely
        # when no CAR is computable anyway (nothing elapsed, or the anchor cannot be priced).
        elapsed = any(h <= newest for h in horizons.values())
        beta_est = (
            fixed_horizon.estimate_beta(
                *_pre_event_closes(
                    grouped,
                    anchor_session,
                    ticker,
                    window=args.beta_window,
                    exchange=args.exchange,
                )
            )
            if elapsed and a_stock is not None and a_spy is not None
            else None
        )

        rec: dict = {
            "brief_date": brief_date,
            "ticker": ticker,
            "classification": classification,
            "beta": beta_est.beta if beta_est else None,
            "beta_source": beta_est.source if beta_est else None,
            "beta_n_obs": beta_est.n_observations if beta_est else None,
            "beta_n_zero_returns": beta_est.n_zero_returns if beta_est else None,
        }
        for k, horizon in horizons.items():
            if horizon > newest or beta_est is None:
                rec[f"car_{k}"] = None  # window not elapsed, or nothing to price it against
                rec[f"car_mm_{k}"] = None
                continue
            window_kwargs = {
                "stock_anchor": a_stock,
                "stock_horizon": _close(grouped.get(horizon), ticker),
                "spy_anchor": a_spy,
                "spy_horizon": _close(grouped.get(horizon), _SPY),
            }
            rec[f"car_{k}"] = fixed_horizon.car_for_event(**window_kwargs)
            rec[f"car_mm_{k}"] = fixed_horizon.car_for_event_market_model(
                beta=beta_est.beta, **window_kwargs
            )

        # Survival: first session in [arrival, arrival+ttl) whose low touches E1.
        e1 = _e1(setups.get((brief_date, ticker)))
        duration: int | None = None
        event = 0
        if e1 is not None and e1 > 0.0:
            incomplete = False
            for i in range(args.ttl):
                s = advance_trading_sessions(arrival, i, args.exchange)
                if s > newest:
                    incomplete = True
                    break
                low = _low(grouped.get(s), ticker)
                if low is None:
                    incomplete = True
                    break
                if low <= e1 * (1.0 + nofill.TOUCH_EPS):
                    duration, event = i + 1, 1
                    break
            if duration is None and not incomplete:
                duration, event = args.ttl, 0  # right-censored at TTL
        rec["fill_duration"] = duration
        rec["fill_event"] = event
        records.append(rec)

    table = pd.DataFrame.from_records(records)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(args.out, index=False)
    print(f"plannable: {len(plannable)}; wrote {args.out} rows: {len(table)}")

    if table.empty:
        print("no rows scored; nothing to report for selection or survival")
        return

    # ---- Selection: per-k CAR with day-blocked bootstrap CI (all / filled / unfilled) ----
    counts = _beta_counts(table["beta_source"])
    stale_sessions = int(table["beta_n_zero_returns"].fillna(0).sum())
    print(
        f"\nbeta vs SPY over {args.beta_window} pre-event sessions, {len(table)} events: "
        f"{counts.estimated} estimated, "
        f"{counts.fell_back} fell back to {fixed_horizon.BETA_FALLBACK_VALUE} "
        f"(min {fixed_horizon.MIN_BETA_OBSERVATIONS} usable returns), "
        f"{counts.not_attempted} not attempted (no elapsed window or no priceable anchor); "
        f"{stale_sessions} flat stock sessions inside the estimated windows"
        + (f"; {counts.unexpected} with an unrecognised beta_source" if counts.unexpected else "")
    )
    print(
        f"\nfixed-horizon CAR (BHAR vs SPY, anchor={args.anchor}), "
        "day-blocked bootstrap 90% CI (events on one brief date resample together):"
    )
    for k in fixed_horizon.K_WINDOWS:
        print(f"  k={k}:")
        # car_mm_<k> is None exactly when car_<k> is, so one completeness mask serves both.
        col = table.get(f"car_{k}", None)
        if col is None:
            continue
        complete = table[col.notna()]
        groups = {
            "all": complete,
            "filled": complete[complete["classification"].isin(_FILLED)],
            "unfilled": complete[complete["classification"] == "NO_FILL"],
        }
        for label, prefix in (("beta=1", "car"), ("market-model", "car_mm")):
            for name, sub in groups.items():
                by_day: dict[object, list[float | None]] = {
                    day: rows[f"{prefix}_{k}"].tolist() for day, rows in sub.groupby("brief_date")
                }
                lo, mean, hi = fixed_horizon.day_block_bootstrap_ci(by_day, seed=args.seed)
                warn = "  [low-N]" if len(sub) < fixed_horizon.LOW_N_WARN else ""
                ms = f"{mean:+.4f}" if mean is not None else "n/a"
                cis = f"[{lo:+.4f}, {hi:+.4f}]" if lo is not None else ""
                print(
                    f"    {label:12} {name:9} n={len(sub):3} days={len(by_day):3} "
                    f"mean={ms} {cis}{warn}"
                )

    # ---- Entry: fill-rate + Kaplan-Meier survival ----
    fillable = table[table["fill_duration"].notna()]
    n_total = len(fillable)
    n_touched = int((fillable["fill_event"] == 1).sum())
    lo, rate, hi = fill_survival.fill_rate_ci(n_touched, n_total, seed=args.seed)
    if rate is not None:
        warn = "  [low-N]" if n_total < fixed_horizon.LOW_N_WARN else ""
        print(
            f"\nfill-rate (touch E1 within TTL={args.ttl}): {n_touched}/{n_total} "
            f"= {rate:.3f}  90% CI [{lo:.3f}, {hi:.3f}]{warn}"
        )
        durations = [int(d) for d in fillable["fill_duration"].tolist()]
        events = [int(e) for e in fillable["fill_event"].tolist()]
        print("Kaplan-Meier S(t) = P(not yet filled by session t):")
        for t, s in fill_survival.kaplan_meier(durations, events):
            print(f"  t={t:2}  S={s:.3f}")
    else:
        print("\nno fillable rows with a complete entry window yet")


if __name__ == "__main__":
    main()
