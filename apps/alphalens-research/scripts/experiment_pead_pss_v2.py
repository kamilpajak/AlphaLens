"""pead_pss_v2_2026_05_13 — Paradigm-14 PEAD event-driven daily-rebalance backtest.

Pre-reg context:
- Ledger: pead_v5_pss_2026_05_13 under class event_drift_search_2026_05_03
  (n=3 strict, |t|>=2.39 class-internal; project doctrine 3.5 binds).
- Design memo: docs/research/paradigm14_pead_v2_design_2026_05_13.md
- Cost-model audit: docs/research/paradigm14_pead_cost_model_audit_2026_05_14.md
  (α2 sub-leveraged weighting; N_FIXED=150 from Little's-Law peak + 50% safety).
- Data: Alpha Vantage EARNINGS endpoint, post-2017-06-01 + |estimatedEPS|>=$0.10.
  Cache populated by VPS systemd timer; expected ~21 days for S&P 500 union.
- Universe: S&P 500 PIT union (~503 tickers across snapshot vintages).
- Pipeline: B1 ``pss_rank`` → top-quintile selection → B2 ``build_daily_weights``
  with α2=1/150 per active → daily portfolio returns → C ``fit_carhart_4f_invested_only``
  with NW HAC maxlags=20 over invested-days only.

Scaffold scope (Phase D): single-window pipeline alive — runs IS phase only and
emits one cost-grid result block. Full multi-phase orchestration (IS/OOS/FL ×
5-cost-arm grid) is invoked by phase_robust_backtesting.audit_multi_phase.run_audit
at Phase E launch.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd
from alphalens_pipeline.data.alt_data.yfinance_cache import load_cached_histories
from alphalens_pipeline.data.factors import load_carhart_daily
from alphalens_pipeline.data.universes.sp1500_pit import load_sp500_pit_union
from alphalens_research.attribution.factor_analysis import (
    fit_carhart_4f_invested_only,
)
from alphalens_research.screeners.event_drift.av_earnings_ingestion import (
    AVEarningsAnnouncement,
    load_av_earnings,
)
from alphalens_research.screeners.event_drift.pead_pss_scorer import (
    build_daily_weights,
    portfolio_returns_from_weights,
)

logger = logging.getLogger(__name__)

# Pre-reg locks per memo + ledger pead_v5_pss_2026_05_13.
_HOLDING_LOCK = 20  # trading days
_N_FIXED_LOCK = 150  # B0 Little's-Law peak + 50% safety margin
_COHORT_WINDOW_DAYS_LOCK = 45  # calendar days, B1
_TOP_QUINTILE_THRESHOLD = 80.0  # percentile_rank >= 80 → top 20%
_HAC_MAXLAGS_LOCK = 20  # half the hold per Phase C
_BONFERRONI_CRITICAL_T_CLASS_INTERNAL = 2.39  # strict n=3 at α=0.05
_BONFERRONI_PROJECT_THRESHOLD = 3.5  # project doctrine

_AV_CACHE_DIR = Path.home() / ".alphalens" / "av_cache"
_PRICES_DIR = Path.home() / ".alphalens" / "prices"


def _close_lookup_factory(histories: dict[str, pd.DataFrame]):
    """close-on-or-before lookup honouring weekends/holidays. PEAD uses
    ``close(reported_date - 1 calendar day)``; the literal calendar-day may
    be a weekend, so we step back to the last trading day on or before."""

    def _lookup(ticker: str, d: date) -> float | None:
        df = histories.get(ticker)
        if df is None or df.empty:
            return None
        ts = pd.Timestamp(d)
        prior = df.index[df.index <= ts]
        if len(prior) == 0:
            return None
        return float(df.at[prior[-1], "close"])

    return _lookup


def _load_universe(cap: int | None) -> list[str]:
    universe = load_sp500_pit_union()
    if cap is not None:
        universe = universe[:cap]
    return universe


def _gather_qualifying_events(
    universe: list[str],
    *,
    is_start: date,
    is_end: date,
    cache_dir: Path,
    close_lookup,
) -> list[AVEarningsAnnouncement]:
    """Load every event whose reported_date is in the entry window and
    whose cohort-relative PSS rank ≥ 80th percentile.

    Strategy: load all events once per ticker (single file read), then
    cross-sectionally rank per reported_date within trailing-cohort window.
    Avoids the per-asof file-reads pattern of B1's pss_rank that would
    re-open each ticker JSON N times.
    """
    # 1) Bulk-load all events; compute each event's PSS up front.
    all_events: list[AVEarningsAnnouncement] = []
    for ticker in universe:
        try:
            all_events.extend(load_av_earnings(ticker, cache_dir=cache_dir))
        except FileNotFoundError:
            continue
    if not all_events:
        return []

    # PSS per event = (rEPS − eEPS) / close(reported_date − 1 calendar day).
    enriched: list[dict] = []
    for event in all_events:
        prior_close = close_lookup(event.ticker, event.reported_date - pd.Timedelta(days=1))
        if prior_close is None or prior_close < 5.0:
            continue  # eligibility: close >= $5
        pss = (event.reported_eps - event.estimated_eps) / prior_close
        if abs(pss) >= 0.20:
            continue  # eligibility: |pss| < 0.20
        enriched.append({"event": event, "pss": float(pss)})
    if not enriched:
        return []

    # 2) For each unique reported_date in the IS entry window, compute
    # cross-sectional percentile rank within the 45d trailing cohort
    # ending on that date.
    #
    # Cohort boundary matches B1 ``score_pead_pss.pss_rank`` contract:
    # half-open-left, closed-right ``(asof - cohort_window_days, asof]``.
    # The strict-left boundary keeps the same-asof event at the upper edge
    # included while excluding the boundary-day from cohort_window_days
    # back, matching B1's ``_latest_event_in_cohort`` filter.
    entry_window = (pd.Timestamp(is_start), pd.Timestamp(is_end))
    df = pd.DataFrame(enriched)
    df["reported_date"] = df["event"].apply(lambda e: pd.Timestamp(e.reported_date))

    qualifying: list[AVEarningsAnnouncement] = []
    for asof, _ in df.groupby("reported_date"):
        if asof < entry_window[0] or asof > entry_window[1]:
            continue
        cohort_lower_exclusive = asof - pd.Timedelta(days=_COHORT_WINDOW_DAYS_LOCK)
        cohort = df[(df["reported_date"] > cohort_lower_exclusive) & (df["reported_date"] <= asof)]
        if len(cohort) < 2:
            continue  # singleton cohort → percentile rank ill-defined
        # Per-ticker keep latest reported_date inside cohort (B1 contract:
        # multiple-events-per-ticker picks most-recent).
        cohort = (
            cohort.sort_values("reported_date")
            .groupby(cohort["event"].apply(lambda e: e.ticker))
            .tail(1)
        )
        # Idiomatic match to B1: ``rank(pct=True) * 100``. Equivalent to
        # ``rank() / len(cohort) * 100`` when no NaN in pss, but explicit.
        cohort = cohort.assign(
            percentile_rank=cohort["pss"].rank(pct=True, method="average") * 100.0
        )
        # Top-quintile WHOSE reported_date == asof (newly-announced today).
        top_today = cohort[
            (cohort["percentile_rank"] >= _TOP_QUINTILE_THRESHOLD)
            & (cohort["reported_date"] == asof)
        ]
        for _, row in top_today.iterrows():
            qualifying.append(row["event"])

    return qualifying


def _ensure_business_calendar(
    factors: pd.DataFrame,
    *,
    start: date,
    end: date,
) -> list[date]:
    """Trading calendar = Carhart factor index over the IS window. FF
    factors are published on US trading days, so the index is canonical."""
    mask = (factors.index >= pd.Timestamp(start)) & (factors.index <= pd.Timestamp(end))
    return [d.date() for d in factors.index[mask]]


def _restrict_to_is_window(weights: pd.DataFrame, is_end: date) -> pd.DataFrame:
    """Drop post-``is_end`` rows that exist only to satisfy B2's calendar
    contract. ``weights`` is indexed by ``datetime.date`` (the trading
    calendar from ``_ensure_business_calendar``), so compare against the
    ``date`` directly — wrapping ``is_end`` in a ``pd.Timestamp`` raises
    'Cannot compare Timestamp with datetime.date' against the object-dtype
    date index."""
    return weights.loc[weights.index <= is_end]


def _factor_window_end(is_end: date, hold_days: int) -> date:
    """Calendar end for the factor / trading-calendar window.

    Extends past ``is_end`` by ~1.5x ``hold_days`` so ``build_daily_weights``
    can complete the hold window for events whose ``reported_date`` falls near
    ``is_end``. Returns a ``datetime.date`` because the callees
    (``load_carhart_daily`` / ``_ensure_business_calendar``) are typed on
    ``date`` — ``date + timedelta`` stays a ``date`` (a prior version added a
    pandas offset and called ``.date()`` on the result, which is already a
    ``date`` and has no such attribute, crashing the smoke)."""
    return is_end + timedelta(days=int(hold_days * 1.5))


def assess(
    *,
    weights: pd.DataFrame,
    daily_returns_panel: pd.DataFrame,
    factors: pd.DataFrame,
    cost_bps: float,
) -> dict:
    """Compute portfolio P&L, apply scalar cost drag, run invested-days Carhart.

    Cost drag is a scalar daily proxy applied to gross daily portfolio return.
    The full multi-phase orchestrator at Phase E applies per-rebalance turnover
    drag with the regime-conditional cost-stress matrix; this scaffold uses
    the simpler scalar form for the single-window smoke.
    """
    gross_daily = portfolio_returns_from_weights(weights, daily_returns_panel)
    invested_mask = (weights.abs().sum(axis=1) > 0).reindex(gross_daily.index, fill_value=False)
    n_invested = int(invested_mask.sum())
    if n_invested < 20:
        return {"n": n_invested, "n_invested_days": n_invested}

    # Scalar cost: cost_bps × 2 (entry + exit) × daily turnover proxy.
    # Daily turnover = 1/n_fixed per new position; for α2 sub-leveraged
    # weighting the mean daily turnover ≈ gross/hold_days. Conservative.
    gross_per_day = float(weights.sum(axis=1).mean())
    daily_turnover_proxy = gross_per_day / _HOLDING_LOCK
    drag_per_day = 2.0 * (cost_bps / 10_000.0) * daily_turnover_proxy
    drag_ann = drag_per_day * 252.0
    net_daily = gross_daily - drag_per_day

    # Mask uninvested days as NaN for Phase-C invested-only regression.
    gross_invested = gross_daily.where(invested_mask)
    net_invested = net_daily.where(invested_mask)

    res_gross = fit_carhart_4f_invested_only(
        gross_invested,
        factors,
        invested_mask=invested_mask,
        maxlags=_HAC_MAXLAGS_LOCK,
    )
    res_net = fit_carhart_4f_invested_only(
        net_invested,
        factors,
        invested_mask=invested_mask,
        maxlags=_HAC_MAXLAGS_LOCK,
    )
    return {
        "n": n_invested,
        "n_invested_days": n_invested,
        "gross_per_day_mean": gross_per_day,
        "alpha_gross_4f": float(res_gross.alpha_annualized),
        "t_4f": float(res_gross.alpha_tstat),
        "beta_smb": float(res_gross.betas.get("SMB", 0.0)),
        "beta_hml": float(res_gross.betas.get("HML", 0.0)),
        "beta_mom": float(res_gross.betas.get("Mom", 0.0)),
        "cost_drag_ann": drag_ann,
        "alpha_net_4f": float(res_net.alpha_annualized),
        "t_net_4f": float(res_net.alpha_tstat),
    }


def _daily_returns_panel(
    tickers: Iterable[str],
    histories: dict[str, pd.DataFrame],
    calendar: list[date],
) -> pd.DataFrame:
    """close-to-close % return per ticker, indexed by trading calendar."""
    out: dict[str, pd.Series] = {}
    cal_index = pd.DatetimeIndex(pd.to_datetime(calendar))
    for ticker in tickers:
        df = histories.get(ticker)
        if df is None or df.empty:
            continue
        rets = df["close"].pct_change()
        out[ticker] = rets.reindex(cal_index).fillna(0.0)
    if not out:
        return pd.DataFrame(index=cal_index)
    return pd.DataFrame(out, index=cal_index)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--is-start", type=date.fromisoformat, required=True)
    ap.add_argument("--is-end", type=date.fromisoformat, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--phase-offset", type=int, default=0, help="No-op; accepted for orchestrator compat."
    )
    ap.add_argument(
        "--rebalance-stride",
        type=int,
        default=1,
        help="No-op — PEAD is daily-rebalance by construction. Accepted for orchestrator compat.",
    )
    ap.add_argument(
        "--universe-size-cap",
        type=int,
        default=None,
        help="Optional ticker cap for smoke runs. Smoke profile uses 200.",
    )
    ap.add_argument(
        "--skip-precheck",
        action="store_true",
        help="No-op; accepted for smoke-harness compatibility.",
    )
    ap.add_argument(
        "--cost-half-spreads",
        nargs="+",
        type=float,
        default=[0.0, 5.0, 10.0, 15.0, 25.0],
        help="Cost stress grid (half-spread bps). G4 gate evaluates 15bps net αt.",
    )
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    logger.info(
        "experiment pead_pss_v2 | %s..%s | phase_offset=%d",
        args.is_start,
        args.is_end,
        args.phase_offset,
    )

    universe = _load_universe(args.universe_size_cap)
    if not universe:
        sys.stderr.write("ERROR: empty universe (sp500_pit_union returned 0 tickers).\n")
        return 3
    logger.info("Universe: %d tickers", len(universe))

    histories = load_cached_histories([*universe, "SPY"], _PRICES_DIR)
    if not histories:
        sys.stderr.write(f"ERROR: no cached price histories under {_PRICES_DIR}.\n")
        return 4
    close_lookup = _close_lookup_factory(histories)

    # Entry window: extend by hold_days on each side to capture events whose
    # entry falls inside [is_start, is_end].
    entry_window_start = args.is_start - pd.Timedelta(days=5).to_pytimedelta()
    entry_window_end = args.is_end

    qualifying = _gather_qualifying_events(
        universe,
        is_start=entry_window_start,
        is_end=entry_window_end,
        cache_dir=_AV_CACHE_DIR,
        close_lookup=close_lookup,
    )
    logger.info("Qualifying events (top-quintile PSS): %d", len(qualifying))
    if not qualifying:
        sys.stderr.write(
            "ERROR: zero qualifying events in window. Likely cause: AV cache empty "
            f"under {_AV_CACHE_DIR}, or no top-quintile PSS events in the window.\n"
        )
        return 5

    # Extend factor window past ``is_end`` by ~1.5× hold_days (calendar) so
    # B2.build_daily_weights can complete the hold window for events whose
    # ``reported_date`` falls near ``is_end``. Without this buffer the
    # ``compute_exit_day`` call inside build_daily_weights raises
    # ``ValueError("entry_day + hold_days extends past calendar")`` and the
    # scaffold crashes. The post-``is_end`` returns are computed but will
    # not enter the regression because we restrict the invested mask to
    # ``[is_start, is_end]`` at the assess() boundary.
    calendar_end = _factor_window_end(args.is_end, _HOLDING_LOCK)
    factors = load_carhart_daily(start=args.is_start, end=calendar_end)
    if factors.empty:
        sys.stderr.write("ERROR: empty Carhart factor frame for the window.\n")
        return 6
    calendar = _ensure_business_calendar(factors, start=args.is_start, end=calendar_end)

    weights = build_daily_weights(
        events=qualifying,
        calendar=calendar,
        n_fixed=_N_FIXED_LOCK,
        hold_days=_HOLDING_LOCK,
    )
    # Restrict regression input to the canonical IS window — post-is_end
    # rows in ``weights`` exist only to satisfy B2's calendar contract.
    weights = _restrict_to_is_window(weights, args.is_end)
    daily_panel = _daily_returns_panel(weights.columns, histories, calendar)

    all_rows: list[dict] = []
    for cost_bps in args.cost_half_spreads:
        stats = assess(
            weights=weights,
            daily_returns_panel=daily_panel,
            factors=factors,
            cost_bps=cost_bps,
        )
        stats["cost_bps"] = cost_bps
        all_rows.append(stats)
        if stats.get("n", 0) > 0:
            logger.info(
                "cost=%.0fbps | n=%d | α 4F=%.1f%% t=%.2f | α-net 4F=%.1f%% t-net=%.2f",
                cost_bps,
                stats["n"],
                stats["alpha_gross_4f"] * 100,
                stats["t_4f"],
                stats["alpha_net_4f"] * 100,
                stats["t_net_4f"],
            )

    # Per-window diagnostics — informational only. Pre-reg gates (1)-(3)
    # (full-sample net αt ≥ 3.5, phase-mean net αt ≥ 2.5, per-phase positive)
    # are MULTI-PHASE and computed by audit_multi_phase.run_audit across
    # IS/OOS/FL invocations of this script — they cannot be answered from a
    # single-window run. Gate (4) cost-stress IS single-window applicable
    # because it evaluates this phase's net αt at the 15bps stress arm.
    # Gate (5) AV PIT validation is logged in the ledger pre-audit.
    baseline_5bps = next(
        (r for r in all_rows if abs(r["cost_bps"] - 5.0) < 1e-5),
        all_rows[0] if all_rows else None,
    )
    stress_15bps = next((r for r in all_rows if abs(r["cost_bps"] - 15.0) < 1e-5), None)
    window_diagnostics: dict = {}
    if baseline_5bps and baseline_5bps.get("n", 0) > 0:
        window_diagnostics["window_alpha_t_net_5bps"] = float(baseline_5bps["t_net_4f"])
        window_diagnostics["window_alpha_t_gross"] = float(baseline_5bps["t_4f"])
    if stress_15bps and stress_15bps.get("n", 0) > 0:
        # Memo §8 gate (4): net αt at 15bps half-spread MUST remain ≥ 2.0
        # in EACH of IS and OOS phases (knockout). Single-window scaffold
        # emits this value; orchestrator enforces the knockout across
        # phases.
        window_diagnostics["window_g4_alpha_t_net_15bps"] = {
            "value": float(stress_15bps["t_net_4f"]),
            "memo_gate_4_threshold": 2.0,
            "this_window_meets_threshold": stress_15bps["t_net_4f"] >= 2.0,
        }

    payload = {
        "strategy": "pead_pss_v2_2026_05_13",
        "ledger_id": "pead_v5_pss_2026_05_13",
        "signal_class": "event_drift_search_2026_05_03",
        "design_memo": "docs/research/paradigm14_pead_v2_design_2026_05_13.md",
        "cost_model_audit": "docs/research/paradigm14_pead_cost_model_audit_2026_05_14.md",
        "is_start": args.is_start.isoformat(),
        "is_end": args.is_end.isoformat(),
        "phase_offset": args.phase_offset,
        "universe_size": len(universe),
        "qualifying_events": len(qualifying),
        "hold_days": _HOLDING_LOCK,
        "n_fixed": _N_FIXED_LOCK,
        "cohort_window_days": _COHORT_WINDOW_DAYS_LOCK,
        "cost_grid_results": all_rows,
        "window_diagnostics": window_diagnostics,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    logger.info("Wrote audit output to %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
