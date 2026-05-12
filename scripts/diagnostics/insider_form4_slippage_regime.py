"""Slippage stress diagnostic for insider_form4_opportunistic — single-purpose orchestration.

Pre-reg memo: ``docs/research/insider_form4_opportunistic_slippage_stress_design_2026_05_12.md``.

Reads per-phase gross returns + per-phase turnover artifacts produced by
``scripts/experiment_insider_form4_opportunistic.py`` under
``~/.alphalens/audit/insider_form4_opportunistic_*/phase_{0-4}_{returns,turnover}.parquet``,
loads IWM benchmark daily series + Carhart factors, then exercises the
``alphalens.diagnostics.slippage_regime`` grid over both OOS and final-lock
windows.

Outputs:
- ``~/.alphalens/diagnostics/insider_form4_slippage_results_2026_05_12.parquet``
  long-format results table indexed by (window, half_spread_bps, beta,
  phase_offset, subsample).
- ``~/.alphalens/diagnostics/insider_form4_slippage_cyclicality_2026_05_12.parquet``
  post-drag cyclicality reversal table (pre-reg memo §9).

Usage::

    .venv/bin/python scripts/diagnostics/insider_form4_slippage_regime.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from alphalens.attribution.signal_vol_regime import (  # noqa: E402
    aggregate_returns_by_regime,
    assign_vol_regime_quintiles,
    classify_cyclicality_excess,
)
from alphalens.data.alt_data.yfinance_cache import load_cached_histories  # noqa: E402
from alphalens.data.factors import load_carhart_daily  # noqa: E402
from alphalens.data.store.history import HistoryStore  # noqa: E402

_PRICES_DIR = Path.home() / ".alphalens" / "prices"
from alphalens.diagnostics.slippage_regime import (  # noqa: E402
    broadcast_turnover_to_daily,
    run_one_slippage_combo,
)

logger = logging.getLogger(__name__)

# Pre-reg cost grid (memo §4)
HALF_SPREAD_GRID_BPS = [5.0, 25.0, 50.0, 75.0, 100.0, 150.0, 200.0, 500.0]
BETA_GRID = [0.0, 1.0, 2.0, 3.0]
HOLDING_DAYS = 21
ADVERSE_SELECTION_BPS = 5.0
HAC_MAXLAGS = 126
PERIODS_PER_YEAR = 252
SIGMA_ROLLING_WINDOW = 60  # IWM 60d realized vol per memo §5

DEFAULT_OOS_ROOT = Path.home() / ".alphalens/audit/insider_form4_opportunistic_phase_b"
DEFAULT_FINALLOCK_ROOT = Path.home() / ".alphalens/audit/insider_form4_opportunistic_final_lock"
DEFAULT_OUT_ROOT = Path.home() / ".alphalens/diagnostics"

WINDOWS = (
    ("OOS_2018_2023", DEFAULT_OOS_ROOT),
    ("FINALLOCK_2024_2026", DEFAULT_FINALLOCK_ROOT),
)
N_PHASES = 5


def _load_phase_artifacts(
    root: Path, n_phases: int = N_PHASES
) -> list[tuple[pd.Series, pd.DataFrame]]:
    """Load (gross_daily, turnover_df) for each phase under ``root``."""
    out: list[tuple[pd.Series, pd.DataFrame]] = []
    for p in range(n_phases):
        rets_path = root / f"phase_{p}_returns.parquet"
        turn_path = root / f"phase_{p}_turnover.parquet"
        if not rets_path.exists():
            raise FileNotFoundError(f"missing returns parquet: {rets_path}")
        if not turn_path.exists():
            raise FileNotFoundError(
                f"missing turnover parquet: {turn_path}. Re-run the audit with the "
                "patched experiment script that auto-dumps per-phase turnover."
            )
        gross_df = pd.read_parquet(rets_path)
        gross = gross_df.iloc[:, 0]
        gross.index = pd.DatetimeIndex(gross.index)
        turnover_df = pd.read_parquet(turn_path)
        turnover_df.index = pd.DatetimeIndex(turnover_df.index)
        out.append((gross, turnover_df))
    return out


def _load_iwm_daily(
    history_store: HistoryStore, start: pd.Timestamp, end: pd.Timestamp
) -> pd.Series:
    df = history_store.full("IWM")
    df = df.loc[(df.index >= start) & (df.index <= end)]
    returns = df["close"].pct_change().dropna()
    returns.name = "IWM"
    return returns


def _compute_iwm_60d_vol(iwm_daily: pd.Series, window: int = SIGMA_ROLLING_WINDOW) -> pd.Series:
    return iwm_daily.rolling(window=window).std() * np.sqrt(PERIODS_PER_YEAR)


def _run_grid_for_window(
    *,
    label: str,
    artifacts: list[tuple[pd.Series, pd.DataFrame]],
    iwm_daily: pd.Series,
    vol_series: pd.Series,
    sigma_median: float,
    factors: pd.DataFrame,
    subsample: str,
) -> list[dict]:
    rows: list[dict] = []
    for phase_idx, (gross, turnover_df) in enumerate(artifacts):
        daily_idx = gross.index
        turnover_daily = broadcast_turnover_to_daily(
            turnover_df, daily_idx, holding_days=HOLDING_DAYS, mode="concentrate"
        )
        for hs in HALF_SPREAD_GRID_BPS:
            for beta in BETA_GRID:
                row = run_one_slippage_combo(
                    gross_daily=gross,
                    turnover_daily=turnover_daily,
                    vol_series=vol_series,
                    factors=factors,
                    half_spread_bps=hs,
                    beta=beta,
                    sigma_median=sigma_median,
                    adverse_selection_bps=ADVERSE_SELECTION_BPS,
                    hac_maxlags=HAC_MAXLAGS,
                    periods_per_year=PERIODS_PER_YEAR,
                )
                row["window"] = label
                row["phase_offset"] = phase_idx
                row["subsample"] = subsample
                row["sigma_median_used"] = sigma_median
                rows.append(row)
    return rows


def _post_drag_cyclicality(
    *,
    label: str,
    artifacts: list[tuple[pd.Series, pd.DataFrame]],
    iwm_daily: pd.Series,
    vol_series: pd.Series,
    sigma_median: float,
    subsample: str,
) -> list[dict]:
    """Per-phase post-drag cyclicality verdict at β=2 (pre-reg memo §9)."""
    rows: list[dict] = []
    from alphalens.diagnostics.slippage_regime import (
        apply_regime_drag,
        compute_effective_half_spread,
    )

    base_bps = 50.0
    beta = 2.0
    for phase_idx, (gross, turnover_df) in enumerate(artifacts):
        daily_idx = gross.index
        turnover_daily = broadcast_turnover_to_daily(
            turnover_df, daily_idx, holding_days=HOLDING_DAYS, mode="concentrate"
        )
        vol_aligned = vol_series.reindex(daily_idx)
        eff_hs = compute_effective_half_spread(
            vol_aligned, base_bps=base_bps, beta=beta, sigma_median=sigma_median
        )
        drag = apply_regime_drag(
            gross, eff_hs, turnover_daily, adverse_selection_bps=ADVERSE_SELECTION_BPS
        )
        rets_net = drag["rets_net"]

        # Pre-drag cyclicality
        quintiles = assign_vol_regime_quintiles(vol_series.reindex(gross.index))
        try:
            strat_pre = aggregate_returns_by_regime(gross, quintiles)
            bench_pre = aggregate_returns_by_regime(iwm_daily.reindex(gross.index), quintiles)
            pre_verdict = classify_cyclicality_excess(strat_pre, bench_pre)
            pre_excess = pre_verdict.excess_R_mean
        except (ValueError, RuntimeError) as exc:
            logger.warning("pre-drag cyclicality failed phase %d %s: %s", phase_idx, label, exc)
            pre_excess = float("nan")

        # Post-drag cyclicality
        post_idx = rets_net.index
        quintiles_post = assign_vol_regime_quintiles(vol_series.reindex(post_idx))
        try:
            strat_post = aggregate_returns_by_regime(rets_net, quintiles_post)
            bench_post = aggregate_returns_by_regime(iwm_daily.reindex(post_idx), quintiles_post)
            post_verdict = classify_cyclicality_excess(strat_post, bench_post)
            post_excess = post_verdict.excess_R_mean
            post_classification = post_verdict.classification
        except (ValueError, RuntimeError) as exc:
            logger.warning("post-drag cyclicality failed phase %d %s: %s", phase_idx, label, exc)
            post_excess = float("nan")
            post_classification = "ERROR"

        rows.append(
            {
                "window": label,
                "phase_offset": phase_idx,
                "subsample": subsample,
                "base_bps": base_bps,
                "beta": beta,
                "R_excess_pre_drag": pre_excess,
                "R_excess_post_drag": post_excess,
                "delta": (post_excess - pre_excess)
                if (np.isfinite(pre_excess) and np.isfinite(post_excess))
                else float("nan"),
                "post_classification": post_classification,
            }
        )
    return rows


def _filter_subsample(
    artifacts: list[tuple[pd.Series, pd.DataFrame]], min_date: pd.Timestamp
) -> list[tuple[pd.Series, pd.DataFrame]]:
    out = []
    for gross, turnover_df in artifacts:
        gross_filt = gross.loc[gross.index >= min_date]
        turn_filt = turnover_df.loc[turnover_df.index >= min_date]
        out.append((gross_filt, turn_filt))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--oos-root",
        type=Path,
        default=DEFAULT_OOS_ROOT,
        help="Directory with OOS phase_N_{returns,turnover}.parquet artifacts.",
    )
    parser.add_argument(
        "--finallock-root",
        type=Path,
        default=DEFAULT_FINALLOCK_ROOT,
        help="Directory with final-lock phase_N_{returns,turnover}.parquet artifacts.",
    )
    parser.add_argument(
        "--out-root",
        type=Path,
        default=DEFAULT_OUT_ROOT,
        help="Where to write the results parquets.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args.out_root.mkdir(parents=True, exist_ok=True)

    logger.info("loading audit artifacts")
    artifacts = {
        "OOS_2018_2023": _load_phase_artifacts(args.oos_root),
        "FINALLOCK_2024_2026": _load_phase_artifacts(args.finallock_root),
    }

    # Joint date span — for σ_median computation (full-sample per memo §5).
    all_starts = [a[0].index.min() for window in artifacts.values() for a in window]
    all_ends = [a[0].index.max() for window in artifacts.values() for a in window]
    joint_start = min(all_starts) - pd.Timedelta(days=SIGMA_ROLLING_WINDOW + 5)
    joint_end = max(all_ends)

    histories = load_cached_histories(["IWM"], _PRICES_DIR)
    history_store = HistoryStore(histories)
    iwm_daily = _load_iwm_daily(history_store, joint_start, joint_end)
    vol_series = _compute_iwm_60d_vol(iwm_daily).dropna()
    sigma_median = float(vol_series.median())
    logger.info(
        "σ_median over joint window = %.4f (%.2f%% annualized)", sigma_median, sigma_median * 100
    )

    factors = load_carhart_daily(start=joint_start.date(), end=joint_end.date())

    all_results: list[dict] = []
    all_cyclicality: list[dict] = []

    for subsample_label, min_date in (
        ("full_sample", pd.Timestamp("1900-01-01")),
        ("post_2010", pd.Timestamp("2010-01-01")),
    ):
        for window_label, window_artifacts in artifacts.items():
            filtered = _filter_subsample(window_artifacts, min_date)
            if any(a[0].empty for a in filtered):
                logger.info(
                    "subsample %s of %s has empty phases — skipping", subsample_label, window_label
                )
                continue
            logger.info("running grid: window=%s subsample=%s", window_label, subsample_label)
            rows = _run_grid_for_window(
                label=window_label,
                artifacts=filtered,
                iwm_daily=iwm_daily,
                vol_series=vol_series,
                sigma_median=sigma_median,
                factors=factors,
                subsample=subsample_label,
            )
            all_results.extend(rows)
            logger.info(
                "post-drag cyclicality: window=%s subsample=%s", window_label, subsample_label
            )
            cyc_rows = _post_drag_cyclicality(
                label=window_label,
                artifacts=filtered,
                iwm_daily=iwm_daily,
                vol_series=vol_series,
                sigma_median=sigma_median,
                subsample=subsample_label,
            )
            all_cyclicality.extend(cyc_rows)

    results_df = pd.DataFrame(all_results)
    cyclicality_df = pd.DataFrame(all_cyclicality)

    results_path = args.out_root / "insider_form4_slippage_results_2026_05_12.parquet"
    cyclicality_path = args.out_root / "insider_form4_slippage_cyclicality_2026_05_12.parquet"
    results_df.to_parquet(results_path)
    cyclicality_df.to_parquet(cyclicality_path)
    logger.info("wrote %s (%d rows)", results_path, len(results_df))
    logger.info("wrote %s (%d rows)", cyclicality_path, len(cyclicality_df))

    # Quick verdict preview at the pre-reg primary gates.
    print("\n=== Quick gate preview (full_sample, pooled across phases) ===")
    full = results_df[results_df["subsample"] == "full_sample"]
    if not full.empty:
        pooled = full.groupby(["window", "half_spread_bps", "beta"], as_index=False).agg(
            alpha_t_net=("alpha_t_net", "mean"), alpha_ann_net=("alpha_annualized_net", "mean")
        )
        for window in pooled["window"].unique():
            sub = pooled[pooled["window"] == window]
            g1 = sub[(sub["half_spread_bps"] == 50.0) & (sub["beta"] == 0.0)]["alpha_t_net"]
            g2 = sub[(sub["half_spread_bps"] == 100.0) & (sub["beta"] == 0.0)]["alpha_t_net"]
            g3 = sub[(sub["half_spread_bps"] == 50.0) & (sub["beta"] == 2.0)]["alpha_t_net"]
            print(
                f"{window}: G1(50/β0) αt={float(g1.iloc[0]) if len(g1) else float('nan'):+.2f} ≥ 2.0? "
                f"| G2(100/β0) αt={float(g2.iloc[0]) if len(g2) else float('nan'):+.2f} ≥ 1.5? "
                f"| G3(50/β2) αt={float(g3.iloc[0]) if len(g3) else float('nan'):+.2f} ≥ 1.5?"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
