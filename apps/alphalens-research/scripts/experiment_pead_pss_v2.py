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
import bisect
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
    bootstrap_carhart_alpha_ci,
    fit_carhart_4f_invested_only,
    run_regression,
)
from alphalens_research.backtest.metrics import sharpe
from alphalens_research.screeners.event_drift.av_earnings_ingestion import (
    AVEarningsAnnouncement,
    load_av_earnings,
)
from alphalens_research.screeners.event_drift.pead_pss_scorer import (
    build_daily_weights,
    compute_entry_day,
    portfolio_returns_from_weights,
)

# Trading days per year — annualisation constant for Sharpe / excess return.
_PERIODS_PER_YEAR = 252

# Memo §6.3 / success-criterion-6: flag any window whose invested-days fraction
# (n_invested / n_total trading days in the IS window) falls below this floor.
# A low-deployment window maximises the masking lift of the invested-days-only
# regression (a false-PASS direction per §17.2), so it must surface as a
# diagnostic, not silently pass the absolute n_invested >= 20 check.
_INVESTED_FRACTION_FLOOR = 0.40

# Memo §18.1 (all-days companion diagnostic): if the binding invested-days-only
# net αt exceeds the all-days (cash-inclusive) net αt by more than this many
# t-units, the gap signals masking lift (the false-PASS direction §17.2 warns
# about) and the window is flagged SUSPECT. Diagnostic only — the locked
# regressand stays invested-days-only, so this carries no v3 / no Bonferroni
# increment.
_ALLDAYS_GAP_SUSPECT_T = 0.2

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


def _drop_uncompletable_tail_events(
    events: list[AVEarningsAnnouncement],
    calendar: list[date],
    *,
    hold_days: int,
) -> list[AVEarningsAnnouncement]:
    """Right-censor events whose ``hold_days``-day exit falls past the calendar.

    At the factor-data tail an event entered within ``hold_days`` trading days
    of ``calendar[-1]`` cannot complete its hold — its post-announcement drift
    is simply not observable yet. The pre-reg ``compute_exit_day`` RAISES on
    such an event (``entry_day + hold_days extends past calendar``), which
    crashed the full / FL windows once the audit reached 2026 (the 2018-Q1
    smoke never touched the tail). Dropping these events is the correct
    right-censoring semantics, applied here in the driver glue so the pinned
    scorer stays untouched. Events whose ``reported_date`` has no eligible
    entry day in the calendar (``compute_entry_day`` raises) are also dropped.

    Keep iff ``entry_idx + hold_days < len(calendar)`` — the exact complement
    of ``compute_exit_day``'s ``exit_idx >= len(calendar)`` raise condition.
    """
    n = len(calendar)
    kept: list[AVEarningsAnnouncement] = []
    for event in events:
        try:
            entry = compute_entry_day(event, calendar)
        except ValueError:
            continue  # no eligible entry day at/after the calendar tail
        entry_idx = bisect.bisect_left(calendar, entry)
        if entry_idx + hold_days < n:
            kept.append(event)
    return kept


def _restrict_to_is_window(weights: pd.DataFrame, is_end: date) -> pd.DataFrame:
    """Drop post-``is_end`` rows that exist only to satisfy B2's calendar
    contract. ``weights`` is indexed by ``datetime.date`` (the trading
    calendar from ``_ensure_business_calendar``), so compare against the
    ``date`` directly — wrapping ``is_end`` in a ``pd.Timestamp`` raises
    'Cannot compare Timestamp with datetime.date' against the object-dtype
    date index."""
    return weights.loc[weights.index <= is_end]


def _invested_fraction_diag(
    n_invested: int, n_total: int, floor: float = _INVESTED_FRACTION_FLOOR
) -> dict:
    """Invested-days-fraction diagnostic (memo §6.3 / §17.2 launch gate).

    Returns the fraction, its inputs, and whether it is below ``floor``. A
    zero-trading-day window is treated as 0.0 (and therefore below floor) so a
    degenerate window is flagged rather than dividing by zero."""
    fraction = (n_invested / n_total) if n_total > 0 else 0.0
    return {
        "invested_fraction": fraction,
        "n_invested": int(n_invested),
        "n_total": int(n_total),
        "below_floor": fraction < floor,
    }


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


def _drag_per_day(weights: pd.DataFrame, cost_bps: float) -> float:
    """Scalar daily cost drag shared by ``_window_returns`` and ``assess()``.

    ``cost_bps × 2 (entry + exit) × daily turnover proxy``, where the daily
    turnover ≈ ``gross / hold_days`` for the α2 sub-leveraged weighting
    (conservative). Single source of truth so the net series and the reported
    ``cost_drag_ann`` can never disagree."""
    gross_per_day = float(weights.sum(axis=1).mean())
    daily_turnover_proxy = gross_per_day / _HOLDING_LOCK
    return 2.0 * (cost_bps / 10_000.0) * daily_turnover_proxy


def _window_returns(
    weights: pd.DataFrame,
    daily_returns_panel: pd.DataFrame,
    cost_bps: float,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Build ``(gross_daily, net_daily, invested_mask)`` for one cost arm.

    Factored out of ``assess()`` so the cost-grid regression and the §18.1 /
    §18.2 inference diagnostics consume the IDENTICAL net series — the binding
    invested-only αt and its all-days companion / bootstrap CI must never drift
    apart by recomputing the drag two different ways.

    Cost drag is a scalar daily proxy: ``cost_bps × 2 (entry + exit) × daily
    turnover proxy``, where the daily turnover ≈ ``gross / hold_days`` for the
    α2 sub-leveraged weighting (conservative). Uninvested days enter ``net``
    and ``gross`` as their real cash return (the B2 adapter yields ``0.0``, not
    NaN); the invested-only regressand masks them downstream, the all-days
    companion keeps them.
    """
    gross_daily = portfolio_returns_from_weights(weights, daily_returns_panel)
    invested_mask = (weights.abs().sum(axis=1) > 0).reindex(gross_daily.index, fill_value=False)
    net_daily = gross_daily - _drag_per_day(weights, cost_bps)
    return gross_daily, net_daily, invested_mask


def _alldays_companion_alpha_t(
    net_daily: pd.Series, factors: pd.DataFrame, *, maxlags: int
) -> float | None:
    """All-days (cash-inclusive) Carhart-4F net αt over the FULL window calendar
    (memo §18.1 companion to the binding invested-only number).

    Uninvested days enter as their real ``0.0`` cash return — NOT masked — so
    this is the deployment-diluted αt. The gap ``invested_only − all_days``
    quantifies the masking lift. Returns ``None`` when ``run_regression`` cannot
    fit (e.g. < 20 overlapping obs), so a degenerate window degrades gracefully
    rather than crashing the diagnostic."""
    try:
        res = run_regression(
            net_daily,
            factors,
            ["Mkt-RF", "SMB", "HML", "Mom"],
            periods_per_year=_PERIODS_PER_YEAR,
            hac_maxlags=maxlags,
            spec_name="Carhart-4F (all-days)",
        )
    except ValueError:
        return None
    return float(res.alpha_tstat)


def _bootstrap_net_alpha_ci(
    net_invested: pd.Series,
    factors: pd.DataFrame,
    *,
    iterations: int = 10_000,
) -> tuple[float, float] | None:
    """Moving-block bootstrap 95% CI on the net Carhart-4F α (memo §18.2 / gate
    #4). ``net_invested`` carries NaN on uninvested days; the bootstrap's
    inner-join + dropna keeps only realised-P&L observations, so the CI answers
    the same question as the invested-only HAC αt. Returns ``None`` when the
    helper cannot run (it requires ≥ 50 obs)."""
    try:
        return bootstrap_carhart_alpha_ci(net_invested, factors, iterations=iterations)
    except ValueError:
        return None


def _inference_diagnostics(
    *,
    invested_only_alpha_t_net: float,
    net_daily: pd.Series,
    invested_mask: pd.Series,
    factors: pd.DataFrame,
    maxlags: int,
    bootstrap_iterations: int = 10_000,
) -> dict:
    """Assemble the §18.1 all-days companion + §18.2 bootstrap-CI diagnostics.

    Both are REPORTED diagnostics (no v3, no Bonferroni increment). The all-days
    αt and its gap from the binding invested-only αt flag masking lift (suspect
    when the gap exceeds ``_ALLDAYS_GAP_SUSPECT_T``); the bootstrap CI on net α
    triangulates the single HAC SE (which can be downward-biased on overlapping
    20-day holds) and is required by convention to exclude 0 for any candidate
    PASS."""
    net_invested = net_daily.where(invested_mask)
    alldays_t = _alldays_companion_alpha_t(net_daily, factors, maxlags=maxlags)
    ci = _bootstrap_net_alpha_ci(net_invested, factors, iterations=bootstrap_iterations)
    out: dict = {
        "invested_only_alpha_t_net": float(invested_only_alpha_t_net),
        "alldays_alpha_t_net": alldays_t,
        "bootstrap_net_alpha_ci_95": list(ci) if ci is not None else None,
        "bootstrap_ci_excludes_zero": bool(ci is not None and (ci[0] > 0.0 or ci[1] < 0.0)),
    }
    if alldays_t is not None:
        gap = invested_only_alpha_t_net - alldays_t
        out["invested_minus_alldays_t"] = float(gap)
        out["suspect_masking_lift"] = bool(gap > _ALLDAYS_GAP_SUSPECT_T)
    else:
        out["invested_minus_alldays_t"] = None
        out["suspect_masking_lift"] = False
    return out


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
    gross_daily, net_daily, invested_mask = _window_returns(weights, daily_returns_panel, cost_bps)
    n_invested = int(invested_mask.sum())
    if n_invested < 20:
        return {"n": n_invested, "n_invested_days": n_invested}

    gross_per_day = float(weights.sum(axis=1).mean())
    drag_ann = _drag_per_day(weights, cost_bps) * 252.0

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

    # Sharpe + annualised return over the invested days. This is a long-only
    # scaffold with no benchmark, so "excess" is the portfolio's own
    # annualised invested-day return (gross / net of the scalar cost drag);
    # the benchmark-relative excess is a Phase-E orchestrator concern. These
    # feed the canonical ``Sh gross=.. net=.. | excess gross=..% net=..%``
    # result line that ``audit_multi_phase.run_audit`` parses (see
    # ``_format_result_line``).
    gross_invested_vals = gross_invested.dropna()
    net_invested_vals = net_invested.dropna()
    return {
        "n": n_invested,
        "n_invested_days": n_invested,
        "gross_per_day_mean": gross_per_day,
        "sharpe_gross": sharpe(gross_invested_vals.tolist(), periods_per_year=_PERIODS_PER_YEAR),
        "sharpe_net": sharpe(net_invested_vals.tolist(), periods_per_year=_PERIODS_PER_YEAR),
        "excess_gross_ann": float(gross_invested_vals.mean() * _PERIODS_PER_YEAR),
        "excess_net_ann": float(net_invested_vals.mean() * _PERIODS_PER_YEAR),
        "alpha_gross_4f": float(res_gross.alpha_annualized),
        "t_4f": float(res_gross.alpha_tstat),
        "beta_smb": float(res_gross.betas.get("SMB", 0.0)),
        "beta_hml": float(res_gross.betas.get("HML", 0.0)),
        "beta_mom": float(res_gross.betas.get("Mom", 0.0)),
        "cost_drag_ann": drag_ann,
        "alpha_net_4f": float(res_net.alpha_annualized),
        "t_net_4f": float(res_net.alpha_tstat),
    }


def _format_result_line(stats: dict, cost_bps: float) -> str:
    """Canonical per-cost result line consumed by
    ``phase_robust_backtesting.audit_multi_phase._RESULT_LINE``.

    The orchestrator parses each phase's stderr for this exact shape; without
    the ``Sh gross=.. net=.. | excess gross=..% net=..%`` prefix the regex
    misses the line and ``run_audit`` aggregates zero rows (empty verdict).
    The ``cost=..bps | n=..`` prefix is the per-cost config key (split on
    `` | n=`` by ``_config_key_from_line``). ``cost_bps`` is passed explicitly
    (not read from ``stats``) so the function is self-contained — ``assess()``
    does not stamp it; the caller does, after the fact."""
    return (
        f"cost={cost_bps:.0f}bps | n={stats['n']:d} | "
        f"Sh gross={stats['sharpe_gross']:.2f} net={stats['sharpe_net']:.2f} | "
        f"excess gross={stats['excess_gross_ann'] * 100:.1f}% "
        f"net={stats['excess_net_ann'] * 100:.1f}% | "
        f"α 4F={stats['alpha_gross_4f'] * 100:.1f}% t={stats['t_4f']:.2f} | "
        f"α-net 4F={stats['alpha_net_4f'] * 100:.1f}% t-net={stats['t_net_4f']:.2f}"
    )


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

    # Right-censor events whose 20-day hold would extend past the factor-data
    # tail (``compute_exit_day`` raises on them — crashed full/FL once the audit
    # reached 2026). Their drift is not yet observable, so drop them.
    n_before = len(qualifying)
    qualifying = _drop_uncompletable_tail_events(qualifying, calendar, hold_days=_HOLDING_LOCK)
    n_censored = n_before - len(qualifying)
    if n_censored:
        logger.info(
            "Right-censored %d/%d events whose %d-day hold extends past the "
            "calendar tail (last=%s)",
            n_censored,
            n_before,
            _HOLDING_LOCK,
            calendar[-1] if calendar else "n/a",
        )
    if not qualifying:
        sys.stderr.write(
            "ERROR: all qualifying events right-censored (hold extends past "
            "the available factor calendar). Widen the factor data or shorten "
            "the window.\n"
        )
        return 7

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
            logger.info("%s", _format_result_line(stats, cost_bps))

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

    # Invested-days-fraction guard (memo §6.3 / §17.2). n_total = trading days
    # in the canonical IS window (weights are already restricted to is_end);
    # n_invested is cost-independent so any row carries it.
    n_total = len(weights.index)
    n_invested = int(all_rows[0].get("n_invested_days", 0)) if all_rows else 0
    inv_frac = _invested_fraction_diag(n_invested, n_total)
    window_diagnostics["invested_fraction"] = inv_frac
    if inv_frac["below_floor"]:
        logger.warning(
            "invested-fraction %.2f below floor %.2f (n_invested=%d / n_total=%d) "
            "— flag for spec review per memo §6.3; low deployment maximises the "
            "invested-days-only masking lift (false-PASS direction)",
            inv_frac["invested_fraction"],
            _INVESTED_FRACTION_FLOOR,
            n_invested,
            n_total,
        )

    # §18.1 all-days companion αt + §18.2 / gate #4 bootstrap-CI on net α.
    # Computed once at the 5bps baseline arm (the canonical reporting cost) from
    # the SAME net series the cost grid used. Reported diagnostics only — no v3,
    # no Bonferroni increment. Skipped when the baseline window has too few
    # invested days for a Carhart fit.
    if baseline_5bps and baseline_5bps.get("n", 0) > 0:
        _, base_net_daily, base_mask = _window_returns(weights, daily_panel, 5.0)
        window_diagnostics["inference"] = _inference_diagnostics(
            invested_only_alpha_t_net=float(baseline_5bps["t_net_4f"]),
            net_daily=base_net_daily,
            invested_mask=base_mask,
            factors=factors,
            maxlags=_HAC_MAXLAGS_LOCK,
        )
        if window_diagnostics["inference"]["suspect_masking_lift"]:
            logger.warning(
                "invested-only net αt %.2f exceeds all-days net αt %.2f by > %.1ft "
                "— window flagged SUSPECT for masking lift (memo §18.1, false-PASS "
                "direction)",
                window_diagnostics["inference"]["invested_only_alpha_t_net"],
                window_diagnostics["inference"]["alldays_alpha_t_net"],
                _ALLDAYS_GAP_SUSPECT_T,
            )

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
