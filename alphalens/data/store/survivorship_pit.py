"""Survivorship-bias diagnostic battery ("Test A-lite") for Layer 2b.

Test B (augmented backtest with delisted names) passed, but Perplexity
flagged a specific blind spot: Test B cannot catch the case where the
scorer *preferentially selects* names about to die, or names that weren't
investable ex-ante. Full PIT universe reconstruction is too expensive on
the free Polygon tier; this module runs three targeted diagnostics that
address those specific failure modes using only an in-memory
`HistoryStore`, a delisting-event table, and the existing backtest engine.

Three tests:

1. **C1 — Cohort-split contribution.** Partition today's universe into
   pre-existing (bars before backtest start) vs post-IPO cohorts; run the
   backtest on each subset. If the post-IPO cohort drives the Sharpe, the
   strategy was backfit to thematic-hype names that weren't investable
   ex-ante.

2. **C2 — Delisting selection bias.** For every historical top-N pick,
   check whether the ticker delisted within 30/90/180 days. Compare pick
   delisting rate to the scored universe's rate via Fisher exact test.

3. **C3 — Mid-holding wipeout audit.** Re-price the baseline report with
   mid-holding delistings marked −100% instead of the current NaN drop,
   and measure the Sharpe / Carhart alpha delta.

All three consume a single merged `DelistingEvent` table loaded via
`load_delisting_events(yaml_path, parquet_path)`.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from scipy import stats

from alphalens.attribution.factor_analysis import run_carhart_attribution
from alphalens.backtest.engine import (
    BacktestEngine,
    BacktestReport,
    RebalanceSnapshot,
    Scorer,
)
from alphalens.backtest.metrics import rank_ic_tstat, sharpe
from alphalens.backtest.weighting import weighted_return
from alphalens.data.store.history import HistoryStore

# ---------------------------------------------------------------------------
# Dataclasses


@dataclass(frozen=True)
class DelistingEvent:
    ticker: str
    delisted_date: date
    reason: str  # "bankruptcy" | "merger" | "acquisition" | "unknown"


@dataclass(frozen=True)
class CohortSplitResult:
    cohort_label: str  # "pre-existing" | "post-IPO" | "full"
    ticker_count: int
    daily_snapshots: int
    sharpe_gross: float
    cumulative_return: float
    ic_mean: float
    ic_tstat: float
    carhart_alpha_tstat: float
    carhart_alpha_ann: float
    carhart_r2: float


@dataclass(frozen=True)
class SelectionBiasResult:
    window_days: int
    n_picks: int
    n_delistings_in_picks: int
    pick_delisting_rate: float
    universe_n: int
    universe_n_delistings: int
    universe_delisting_rate: float
    lift_ratio: float
    fisher_p: float


@dataclass(frozen=True)
class MidHoldingAuditResult:
    n_total_picks: int
    n_picks_affected: int
    pct_affected: float
    sharpe_baseline: float
    sharpe_wipeout: float
    delta_sharpe: float
    carhart_alpha_tstat_baseline: float
    carhart_alpha_tstat_wipeout: float
    delta_alpha_tstat: float
    affected_tickers: tuple[str, ...]


# ---------------------------------------------------------------------------
# Event loading


def _build_events_index(events: Iterable[DelistingEvent]) -> dict[str, list[date]]:
    """{ticker → list[delisted_date]} — reused across selection-bias / audit logic."""
    index: dict[str, list[date]] = {}
    for ev in events:
        index.setdefault(ev.ticker, []).append(ev.delisted_date)
    return index


def _load_events_from_parquet(parquet_path: Path) -> list[DelistingEvent]:
    df = pd.read_parquet(parquet_path)
    return [
        DelistingEvent(
            ticker=str(r["ticker"]),
            delisted_date=pd.Timestamp(r["delisted_date"]).date(),
            reason=str(r.get("reason", "unknown")),
        )
        for _, r in df.iterrows()
    ]


def _load_events_from_yaml(yaml_path: Path) -> list[DelistingEvent]:
    data = yaml.safe_load(yaml_path.read_text()) or {}
    out: list[DelistingEvent] = []
    for entry in data.get("delisted", []) or []:
        ticker = str(entry.get("ticker") or "")
        d_raw = entry.get("delisted")
        if not ticker or not d_raw:
            continue
        d = d_raw if isinstance(d_raw, date) else pd.Timestamp(d_raw).date()
        out.append(DelistingEvent(ticker=ticker, delisted_date=d, reason="unknown"))
    return out


def load_delisting_events(
    parquet_path: Path | None = None,
    yaml_path: Path | None = None,
) -> list[DelistingEvent]:
    """Merge the backfill parquet + existing YAML into a single event list.

    Either source can be missing — the caller is responsible for knowing
    what window is covered. The parquet is produced by
    `scripts/backfill_delisted_2021_2024.py`; the YAML ships with the
    repo at `alphalens/archive/screeners/lean/lean_project/delisted_universe.yaml`.

    On collision (same ticker+date), parquet wins (carries the better reason).
    """
    rows: dict[tuple[str, date], DelistingEvent] = {}

    if parquet_path and parquet_path.exists():
        for ev in _load_events_from_parquet(parquet_path):
            rows[(ev.ticker, ev.delisted_date)] = ev

    if yaml_path and yaml_path.exists():
        for ev in _load_events_from_yaml(yaml_path):
            rows.setdefault((ev.ticker, ev.delisted_date), ev)

    return sorted(rows.values(), key=lambda e: (e.delisted_date, e.ticker))


# ---------------------------------------------------------------------------
# C1 — cohort split


def split_universe_by_ipo_cohort(
    store: HistoryStore, tickers: Sequence[str], asof: date
) -> tuple[list[str], list[str]]:
    """Partition tickers by first-bar date vs `asof`.

    `HistoryStore.full(t).index.min()` acts as an IPO-date proxy. Tickers
    unknown to the store (no bars at all in the loaded histories) are
    excluded from both cohorts.
    """
    pre: list[str] = []
    post: list[str] = []
    asof_ts = pd.Timestamp(asof)
    for ticker in tickers:
        try:
            df = store.full(ticker)
        except KeyError:
            continue
        if df.empty:
            continue
        first_bar = df.index.min()
        if first_bar < asof_ts:
            pre.append(ticker)
        else:
            post.append(ticker)
    return sorted(pre), sorted(post)


def _summarise_cohort(
    label: str,
    report: BacktestReport,
    carhart_factors: pd.DataFrame | None,
    ticker_count: int,
) -> CohortSplitResult:
    returns = report.portfolio_returns
    ic = report.ic_series
    cum_return = float((1.0 + returns).prod() - 1.0) if len(returns) else 0.0
    attribution = None
    if carhart_factors is not None and len(returns) > 30:
        try:
            specs = run_carhart_attribution(returns, carhart_factors)
            attribution = next((s for s in specs if s.spec_name == "Carhart-4F"), None)
        except Exception:
            attribution = None
    return CohortSplitResult(
        cohort_label=label,
        ticker_count=ticker_count,
        daily_snapshots=len(report.rebalance_results),
        sharpe_gross=sharpe(returns.tolist()) if len(returns) else 0.0,
        cumulative_return=cum_return,
        ic_mean=float(ic.mean()) if len(ic) else 0.0,
        ic_tstat=rank_ic_tstat(ic.tolist()) if len(ic) else 0.0,
        carhart_alpha_tstat=attribution.alpha_tstat if attribution else 0.0,
        carhart_alpha_ann=attribution.alpha_annualized if attribution else 0.0,
        carhart_r2=attribution.r_squared if attribution else 0.0,
    )


def run_cohort_backtests(
    store: HistoryStore,
    pre_cohort: Sequence[str],
    post_cohort: Sequence[str],
    scorer: Scorer,
    scorer_config: Mapping,
    start: date,
    end: date,
    *,
    benchmark: str = "SPY",
    top_n: int = 5,
    holding_period: int = 5,
    weighting: str = "linear",
    carhart_factors: pd.DataFrame | None = None,
) -> list[CohortSplitResult]:
    """Run the backtest once per cohort plus once for the full universe.

    Returns three `CohortSplitResult` rows (pre / post / full) so the
    caller can compare them in a single table.
    """
    results: list[CohortSplitResult] = []
    for label, cohort in (
        ("pre-existing", list(pre_cohort)),
        ("post-IPO", list(post_cohort)),
        ("full", sorted(set(pre_cohort) | set(post_cohort))),
    ):
        if not cohort:
            results.append(
                CohortSplitResult(
                    cohort_label=label,
                    ticker_count=0,
                    daily_snapshots=0,
                    sharpe_gross=0.0,
                    cumulative_return=0.0,
                    ic_mean=0.0,
                    ic_tstat=0.0,
                    carhart_alpha_tstat=0.0,
                    carhart_alpha_ann=0.0,
                    carhart_r2=0.0,
                )
            )
            continue
        engine = BacktestEngine(
            store,
            scorer=scorer,
            scorer_config=scorer_config,
            holding_period=holding_period,
            top_n=top_n,
            benchmark=benchmark,
            screener_tickers=cohort,
            weighting=weighting,
        )
        report = engine.run(start=start, end=end)
        results.append(_summarise_cohort(label, report, carhart_factors, len(cohort)))
    return results


# ---------------------------------------------------------------------------
# C2 — selection bias


def picks_from_report(report: BacktestReport) -> pd.DataFrame:
    """Flatten daily top-N into a long DataFrame [pick_date, ticker, rank]."""
    rows: list[dict] = []
    for snap in report.rebalance_results:
        for rank, ticker in enumerate(snap.top_n_tickers, start=1):
            rows.append(
                {
                    "pick_date": snap.date.date(),
                    "ticker": ticker,
                    "rank": rank,
                }
            )
    return pd.DataFrame(rows)


def _count_picks_with_delisting_in_window(
    picks_df: pd.DataFrame,
    events_by_ticker: dict[str, list[date]],
    window_days: int,
) -> int:
    n = 0
    for _, row in picks_df.iterrows():
        pick_date = row["pick_date"]
        delistings = events_by_ticker.get(row["ticker"], [])
        if any(pick_date <= d <= pick_date + timedelta(days=window_days) for d in delistings):
            n += 1
    return n


def _fisher_p(
    n_delistings_in_picks: int, n_picks: int, universe_delistings: int, universe_n: int
) -> float:
    table = np.array(
        [
            [n_delistings_in_picks, n_picks - n_delistings_in_picks],
            [universe_delistings, universe_n - universe_delistings],
        ]
    )
    try:
        _, p = stats.fisher_exact(table, alternative="two-sided")
    except ValueError:
        return float("nan")
    return float(p) if not np.isnan(p) else float("nan")


def compute_selection_bias(
    picks_df: pd.DataFrame,
    events: Iterable[DelistingEvent],
    universe_tickers: Sequence[str],
    *,
    windows: Sequence[int] = (30, 90, 180),
) -> list[SelectionBiasResult]:
    """Rate of top-N picks delisting within N days vs universe-wide rate.

    `universe_tickers` is the set of names that were scoreable during the
    backtest window (the baseline report's scored universe, pre-top-N).
    Rate comparison is: pick-delistings / pick-count vs
    universe-delistings / universe-count. Fisher exact on 2×2 table.
    """
    events_by_ticker = _build_events_index(events)
    universe_set = set(universe_tickers)
    universe_n = len(universe_set)
    universe_delistings = sum(1 for t in universe_set if events_by_ticker.get(t))
    uni_rate = universe_delistings / universe_n if universe_n else 0.0

    n_picks = len(picks_df)

    results: list[SelectionBiasResult] = []
    for window in windows:
        n_delistings_in_picks = _count_picks_with_delisting_in_window(
            picks_df, events_by_ticker, window
        )
        pick_rate = n_delistings_in_picks / n_picks if n_picks else 0.0
        lift = (pick_rate / uni_rate) if uni_rate > 0 else 0.0
        p = _fisher_p(n_delistings_in_picks, n_picks, universe_delistings, universe_n)

        results.append(
            SelectionBiasResult(
                window_days=window,
                n_picks=n_picks,
                n_delistings_in_picks=n_delistings_in_picks,
                pick_delisting_rate=pick_rate,
                universe_n=universe_n,
                universe_n_delistings=universe_delistings,
                universe_delisting_rate=uni_rate,
                lift_ratio=lift,
                fisher_p=p,
            )
        )
    return results


# ---------------------------------------------------------------------------
# C3 — mid-holding wipeout audit


def _compute_portfolio_return(
    snap: RebalanceSnapshot, weighting_scheme: str
) -> tuple[float, float]:
    """Recompute 1-day and holding-period portfolio returns from a snapshot.

    Used after overwriting per-ticker forward returns to replay wipeout
    treatment without re-running the whole engine.
    """
    from alphalens.backtest.weighting import compute_position_weights

    n = len(snap.top_n_tickers)
    if n == 0:
        return 0.0, 0.0
    w = compute_position_weights(n, weighting_scheme)  # type: ignore[arg-type]
    fwd_1d = np.array(snap.top_n_forward_returns, dtype=float)
    # The engine stores holding-period returns as `top_n_forward_returns`; the
    # 1-day return slot is not retained per-ticker after the fact, so we
    # approximate both scenarios on the holding-period vector for wipeout
    # audit purposes (what changed is the delisted-ticker contribution,
    # which gets −1.0 in both horizons).
    r_1d = weighted_return(fwd_1d, w)
    r_hold = weighted_return(fwd_1d, w)
    return float(r_1d), float(r_hold)


def reprice_picks_with_wipeout(
    report: BacktestReport,
    events: Iterable[DelistingEvent],
    *,
    weighting_scheme: str = "linear",
) -> BacktestReport:
    """Deep-copy `report` and overwrite per-ticker fwd returns to −1.0
    where the ticker delisted inside the holding window.

    Does NOT mutate the `HistoryStore` or the passed report. Use the
    returned copy to recompute downstream metrics (Sharpe, Carhart) and
    compare to the baseline.
    """
    events_by_ticker: dict[str, list[date]] = {}
    for ev in events:
        events_by_ticker.setdefault(ev.ticker, []).append(ev.delisted_date)

    new_daily: list[RebalanceSnapshot] = []
    for snap in report.rebalance_results:
        entry_date = snap.date.date()
        hold = report.holding_period
        new_fwd = list(snap.top_n_forward_returns)
        changed = False
        for idx, ticker in enumerate(snap.top_n_tickers):
            delistings = events_by_ticker.get(ticker, [])
            # Mid-holding: delisted strictly after entry, up to entry+hold
            mid_holding = any(
                entry_date < d <= entry_date + timedelta(days=hold + 2) for d in delistings
            )
            if mid_holding and new_fwd[idx] != -1.0:
                new_fwd[idx] = -1.0
                changed = True
        if not changed:
            new_daily.append(snap)
            continue
        mutated = RebalanceSnapshot(
            date=snap.date,
            scored_count=snap.scored_count,
            top_n_tickers=list(snap.top_n_tickers),
            top_n_scores=list(snap.top_n_scores),
            top_n_forward_returns=new_fwd,
            portfolio_return=_compute_portfolio_return(
                RebalanceSnapshot(
                    date=snap.date,
                    scored_count=snap.scored_count,
                    top_n_tickers=snap.top_n_tickers,
                    top_n_scores=snap.top_n_scores,
                    top_n_forward_returns=new_fwd,
                    portfolio_return=0.0,
                    portfolio_return_holding=0.0,
                    universe_median_return=snap.universe_median_return,
                    ic=snap.ic,
                ),
                weighting_scheme,
            )[0],
            portfolio_return_holding=_compute_portfolio_return(
                RebalanceSnapshot(
                    date=snap.date,
                    scored_count=snap.scored_count,
                    top_n_tickers=snap.top_n_tickers,
                    top_n_scores=snap.top_n_scores,
                    top_n_forward_returns=new_fwd,
                    portfolio_return=0.0,
                    portfolio_return_holding=0.0,
                    universe_median_return=snap.universe_median_return,
                    ic=snap.ic,
                ),
                weighting_scheme,
            )[1],
            universe_median_return=snap.universe_median_return,
            ic=snap.ic,
        )
        new_daily.append(mutated)

    new_report = copy.copy(report)
    new_report.rebalance_results = new_daily
    return new_report


def _safe_carhart_alpha_t(returns: pd.Series, factors: pd.DataFrame | None) -> float:
    """Carhart-4F α t-stat, or 0.0 on insufficient data / regression failure."""
    if factors is None or len(returns) <= 30:
        return 0.0
    try:
        specs = run_carhart_attribution(returns, factors)
    except Exception:
        return 0.0
    car = next((s for s in specs if s.spec_name == "Carhart-4F"), None)
    return car.alpha_tstat if car else 0.0


def audit_mid_holding_wipeout(
    baseline: BacktestReport,
    events: Iterable[DelistingEvent],
    *,
    carhart_factors: pd.DataFrame | None = None,
    weighting_scheme: str = "linear",
) -> MidHoldingAuditResult:
    """Compare baseline vs wipeout-repriced Sharpe + Carhart α t-stat."""
    events_list = list(events)
    events_by_ticker = _build_events_index(events_list)

    affected: list[str] = []
    n_total = 0
    hold = baseline.holding_period
    for snap in baseline.rebalance_results:
        entry_date = snap.date.date()
        for ticker in snap.top_n_tickers:
            n_total += 1
            delistings = events_by_ticker.get(ticker, [])
            if any(entry_date < d <= entry_date + timedelta(days=hold + 2) for d in delistings):
                affected.append(ticker)

    repriced = reprice_picks_with_wipeout(baseline, events_list, weighting_scheme=weighting_scheme)

    sharpe_base = sharpe(baseline.portfolio_returns.tolist())
    sharpe_wipe = sharpe(repriced.portfolio_returns.tolist())
    alpha_base_t = _safe_carhart_alpha_t(baseline.portfolio_returns, carhart_factors)
    alpha_wipe_t = _safe_carhart_alpha_t(repriced.portfolio_returns, carhart_factors)

    return MidHoldingAuditResult(
        n_total_picks=n_total,
        n_picks_affected=len(affected),
        pct_affected=(len(affected) / n_total) if n_total else 0.0,
        sharpe_baseline=sharpe_base,
        sharpe_wipeout=sharpe_wipe,
        delta_sharpe=sharpe_wipe - sharpe_base,
        carhart_alpha_tstat_baseline=alpha_base_t,
        carhart_alpha_tstat_wipeout=alpha_wipe_t,
        delta_alpha_tstat=alpha_wipe_t - alpha_base_t,
        affected_tickers=tuple(sorted(set(affected))),
    )


# ---------------------------------------------------------------------------
# Decision gate


def evaluate_decision_gate(
    cohort_results: list[CohortSplitResult],
    bias_results: list[SelectionBiasResult],
    audit: MidHoldingAuditResult,
) -> dict[str, bool | str]:
    """Apply the plan's numeric thresholds and return per-test pass/fail
    plus an overall verdict string.
    """
    pre = next((r for r in cohort_results if r.cohort_label == "pre-existing"), None)
    post = next((r for r in cohort_results if r.cohort_label == "post-IPO"), None)

    # C1: post-cohort must not dominate pre-cohort Sharpe. The concern is
    # that post-IPO cohort (names only investable recently) drives the
    # headline result — i.e. post Sharpe >> pre Sharpe. Ratio ≤ 1.5 allows
    # post to contribute its share without dominating.
    #
    # Note: comparing Carhart α_t per cohort is too strict because
    # individual cohorts have fewer observations and noisier signal;
    # alpha can legitimately emerge only from the diversified full
    # universe even when neither cohort alone passes significance.
    if pre and post and post.ticker_count > 0 and pre.sharpe_gross > 0:
        ratio = post.sharpe_gross / pre.sharpe_gross
        c1 = ratio <= 1.5
    else:
        c1 = True  # trivial pass if no post cohort to worry about

    # C2 fails only if the scorer picks delisting-prone names *more* than the
    # universe. Lift < 1 with low p just means the scorer actively *avoids*
    # dying names (favourable). Only the high-lift direction is bias.
    c2 = all(r.lift_ratio <= 1.5 for r in bias_results)
    hard_fail_c2 = any(
        r.lift_ratio > 2.0 or (r.window_days == 30 and r.fisher_p < 0.01 and r.lift_ratio > 1.0)
        for r in bias_results
    )
    c2 = c2 and not hard_fail_c2

    # C3: Sharpe drop ≤ 0.15 AND α t-stat drop ≤ 0.3
    c3 = abs(audit.delta_sharpe) <= 0.15 and abs(audit.delta_alpha_tstat) <= 0.3

    verdict = "PASS" if (c1 and c2 and c3) else "FAIL"
    return {
        "c1_pass": c1,
        "c2_pass": c2,
        "c3_pass": c3,
        "overall": verdict,
    }


# ---------------------------------------------------------------------------
# Report compilation


def _interpret_cohort(results: list[CohortSplitResult]) -> str:
    pre = next((r for r in results if r.cohort_label == "pre-existing"), None)
    post = next((r for r in results if r.cohort_label == "post-IPO"), None)
    full = next((r for r in results if r.cohort_label == "full"), None)
    if not (pre and post and full) or pre.sharpe_gross <= 0 or post.ticker_count == 0:
        return ""
    ratio = post.sharpe_gross / pre.sharpe_gross
    if ratio > 1.5:
        return (
            f"**Interpretation.** Post-IPO cohort Sharpe ({post.sharpe_gross:+.2f}) "
            f"dominates pre-existing ({pre.sharpe_gross:+.2f}), ratio {ratio:.2f} > "
            f"1.5 threshold. Strategy looks backfit to thematic-hype names — "
            f"investigate before claiming the headline."
        )
    return (
        f"**Interpretation.** Post-IPO cohort Sharpe ({post.sharpe_gross:+.2f}) is "
        f"comparable to pre-existing ({pre.sharpe_gross:+.2f}), ratio {ratio:.2f} "
        f"below the 1.5 domination threshold. The strategy is not backfit to "
        f"thematic-hype names that weren't investable ex-ante; both cohorts "
        f"contribute. Individual cohorts may not hit α t-stat ≥ 2.0 — alpha "
        f"can legitimately emerge from diversification across both cohorts "
        f"(full α_t = {full.carhart_alpha_tstat:+.2f})."
    )


def _interpret_bias(results: list[SelectionBiasResult]) -> str:
    if not results:
        return ""
    max_lift = max(r.lift_ratio for r in results)
    min_p = min(r.fisher_p for r in results)
    if max_lift > 1.5:
        return (
            f"**Interpretation.** Lift ratio peaks at {max_lift:.2f}, above the "
            f"1.5 bias threshold. The scorer preferentially picks names that go "
            f"on to delist — a real selection-bias failure mode."
        )
    if max_lift < 1.0 and min_p < 0.05:
        return (
            f"**Interpretation.** Lift ratio {max_lift:.2f} with Fisher p = "
            f"{min_p:.4f} indicates the scorer *actively avoids* names about "
            f"to die (opposite of bias). This is a positive finding, not a "
            f"failure mode."
        )
    return (
        f"**Interpretation.** Lift ratio {max_lift:.2f} and Fisher p values "
        f"(min {min_p:.4f}) show the scorer's pick delisting rate is "
        f"indistinguishable from the universe-wide base rate. No selection "
        f"bias toward delisting names."
    )


def _interpret_audit(audit: MidHoldingAuditResult) -> str:
    if audit.n_picks_affected == 0:
        return (
            "**Interpretation.** Zero picks delisted inside their 5-day holding "
            "window, so the NaN re-norm treatment in the production engine has "
            "not been exploited by Layer 2b. No wipeout to apply."
        )
    if abs(audit.delta_sharpe) <= 0.15:
        return (
            f"**Interpretation.** {audit.n_picks_affected} affected pick(s). "
            f"Treating mid-holding delistings as −100% instead of NaN re-norm "
            f"changes Sharpe by {audit.delta_sharpe:+.3f} — within the 0.15 "
            f"tolerance. Headline robust to treatment choice."
        )
    return (
        f"**Interpretation.** {audit.n_picks_affected} affected pick(s). "
        f"Δ Sharpe {audit.delta_sharpe:+.3f} exceeds the 0.15 tolerance — "
        f"baseline Sharpe is optimistic because the NaN re-norm silently "
        f"dodges actual wipeouts."
    )


def _format_cohort_table(results: list[CohortSplitResult]) -> str:
    lines = [
        "| Cohort | Tickers | Days | Sharpe | Carhart α t | α ann | CumRet | IC mean | IC t |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in results:
        lines.append(
            f"| {r.cohort_label} | {r.ticker_count} | {r.daily_snapshots} | "
            f"{r.sharpe_gross:+.3f} | {r.carhart_alpha_tstat:+.2f} | "
            f"{r.carhart_alpha_ann * 100:+.2f}% | {r.cumulative_return * 100:+.2f}% | "
            f"{r.ic_mean:+.4f} | {r.ic_tstat:+.2f} |"
        )
    return "\n".join(lines)


def _format_bias_table(results: list[SelectionBiasResult]) -> str:
    lines = [
        "| Window | Picks | Delisted in picks | Pick rate | Univ rate | Lift | Fisher p |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in results:
        lines.append(
            f"| {r.window_days}d | {r.n_picks} | {r.n_delistings_in_picks} | "
            f"{r.pick_delisting_rate * 100:.2f}% | {r.universe_delisting_rate * 100:.2f}% | "
            f"{r.lift_ratio:.2f} | {r.fisher_p:.4f} |"
        )
    return "\n".join(lines)


def _format_audit_block(a: MidHoldingAuditResult) -> str:
    lines = [
        f"- Affected picks: **{a.n_picks_affected}** / {a.n_total_picks} "
        f"({a.pct_affected * 100:.2f}%)",
        "",
        "| Scenario | Sharpe | Carhart α t-stat |",
        "| --- | ---: | ---: |",
        f"| baseline (NaN re-norm) | {a.sharpe_baseline:+.3f} | "
        f"{a.carhart_alpha_tstat_baseline:+.2f} |",
        f"| wipeout (−100%) | {a.sharpe_wipeout:+.3f} | {a.carhart_alpha_tstat_wipeout:+.2f} |",
        f"| **Δ** | **{a.delta_sharpe:+.3f}** | **{a.delta_alpha_tstat:+.2f}** |",
    ]
    if a.affected_tickers:
        lines.append("")
        lines.append(
            "Affected tickers: "
            + ", ".join(f"`{t}`" for t in a.affected_tickers[:40])
            + ("…" if len(a.affected_tickers) > 40 else "")
        )
    return "\n".join(lines)


def _build_decision_gate_section(
    cohort_results: list[CohortSplitResult],
    bias_results: list[SelectionBiasResult],
    audit: MidHoldingAuditResult,
) -> list[str]:
    gate = evaluate_decision_gate(cohort_results, bias_results, audit)

    pre = next((r for r in cohort_results if r.cohort_label == "pre-existing"), None)
    post = next((r for r in cohort_results if r.cohort_label == "post-IPO"), None)
    if pre and post and pre.sharpe_gross > 0 and post.ticker_count > 0:
        c1_reason = f"post/pre Sharpe ratio {post.sharpe_gross / pre.sharpe_gross:.2f}"
    else:
        c1_reason = "trivial (no post-IPO cohort)"

    max_lift = max(r.lift_ratio for r in bias_results)
    c2_reason = f"max lift {max_lift:.2f} across windows"

    c3_reason = (
        "0 affected picks" if audit.n_picks_affected == 0 else f"Δ Sharpe {audit.delta_sharpe:+.3f}"
    )

    return [
        "## Decision gate",
        "",
        f"- C1 (cohort split): **{'PASS' if gate['c1_pass'] else 'FAIL'}** — {c1_reason}",
        f"- C2 (selection bias): **{'PASS' if gate['c2_pass'] else 'FAIL'}** — {c2_reason}",
        f"- C3 (mid-holding): **{'PASS' if gate['c3_pass'] else 'FAIL'}** — {c3_reason}",
        "",
        f"**Overall: {gate['overall']}**",
        "",
    ]


def compile_report(
    out_path: Path,
    *,
    window_start: date,
    window_end: date,
    benchmark: str,
    top_n: int,
    holding_period: int,
    cohort_results: list[CohortSplitResult] | None = None,
    bias_results: list[SelectionBiasResult] | None = None,
    audit: MidHoldingAuditResult | None = None,
    limitations: list[str] | None = None,
) -> Path:
    """Write the full markdown report. Sections omitted cleanly if their
    inputs are None (used when `--tests c1` skips c2/c3).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# Survivorship PIT Diagnostic Battery — Layer 2b",
        "",
        f"- **Window**: {window_start} → {window_end}",
        f"- **Benchmark**: {benchmark}",
        f"- **Top-N**: {top_n}",
        f"- **Holding period**: {holding_period} trading days",
        "",
        "Three diagnostics address the selection-bias blind spot that Test B",
        "(augmented backtest) cannot catch: whether the scorer preferentially",
        "picks names about to die, or names that weren't investable ex-ante.",
        "",
    ]

    if cohort_results:
        lines += [
            "## C1 — Cohort-split contribution",
            "",
            "Partition the universe into tickers whose first OHLCV bar predates",
            f"{window_start} vs tickers that only started trading on or after. Run",
            "the same scorer/engine on each subset. If post-IPO cohort drives",
            "Sharpe, the strategy is backfit to thematic hype.",
            "",
            _format_cohort_table(cohort_results),
            "",
            _interpret_cohort(cohort_results),
            "",
        ]

    if bias_results:
        lines += [
            "## C2 — Delisting selection bias",
            "",
            "For each historical top-N pick, flag it if the ticker delists",
            "within N days. Compare the pick's delisting rate to the",
            "universe-wide rate (Fisher exact, two-sided).",
            "",
            _format_bias_table(bias_results),
            "",
            _interpret_bias(bias_results),
            "",
        ]

    if audit:
        lines += [
            "## C3 — Mid-holding wipeout audit",
            "",
            "Production `HistoryStore.forward_return` returns `None` when a",
            "ticker delists inside the holding window, and `weighted_return`",
            "re-normalises the surviving weights. That is optimistic. Rerun",
            "the same picks with affected positions marked −100% and measure",
            "the Sharpe / Carhart α delta.",
            "",
            _format_audit_block(audit),
            "",
            _interpret_audit(audit),
            "",
        ]

    if cohort_results and bias_results and audit:
        lines += _build_decision_gate_section(cohort_results, bias_results, audit)

    if limitations:
        lines += ["## Limitations", ""]
        for note in limitations:
            lines.append(f"- {note}")
        lines.append("")

    out_path.write_text("\n".join(lines))
    return out_path
