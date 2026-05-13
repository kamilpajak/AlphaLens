"""Phase 4 — synthesize verdict for v9D retrospective pre-2018 battery.

Reads 45 per-cell JSONs from
``docs/research/v9d_retrospective_pre_2018/``, applies pre-registered
decision tree + Andrews-Manski bounds + Romano-Wolf adjusted critical
value, writes a postmortem markdown + consolidated verdict JSON.

Pre-reg locked in
``docs/research/preregistration/params_v9d_retrospective_pre_2018_2026_05_05.json``
(sha256 ``f43ac35785391fd3010efbc128d875fb0c982923df31a5ad7eed4d35f7548b58``).
This driver is the only place the verdict is decided; do not invoke
``romano_wolf_step_down`` etc. ad-hoc on partial data."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.backtest.bounds_inference import andrews_manski_bounds
from alphalens.backtest.romano_wolf import (
    romano_wolf_step_down_per_strategy,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CELLS_DIR = REPO_ROOT / "docs" / "research" / "v9d_retrospective_pre_2018"
PRE_REG_PATH = (
    REPO_ROOT
    / "docs"
    / "research"
    / "preregistration"
    / "params_v9d_retrospective_pre_2018_2026_05_05.json"
)

UNIVERSES = ("U1", "U2", "U3")
SUB_PERIODS = (
    "GFC_recovery",
    "mid_cycle_eu_debt",
    "late_cycle_china_shock",
)
PHASES = (0, 1, 2, 3, 4)

# Pre-reg locked thresholds (re-stated here so a postmortem reader can
# follow without cross-referencing the JSON).
BIAS_RANGE_PCT = (1.0, 2.0)  # /y, [B_lower, B_upper]
ALPHA_T_RANGE_STABLE = 1.5  # per-phase dispersion within sub-period
ALPHA_T_RANGE_REGIME_THRESHOLD = 2.5  # cross-sub-period
PASS_ROBUST_ALPHA_T = 3.5
PASS_MARGINAL_ALPHA_T = 2.5
INCONCLUSIVE_LOWER_ALPHA_T = 1.0
FAIL_ROBUST_ALPHA_T = 1.0
RECONSTRUCTION_DOMINANT_GAP = 3.0
G6_TERMINAL_RETURN_TOL = 0.5  # not currently exercised; documented for future


def load_cells(cells_dir: Path) -> dict[tuple[str, str, int], dict]:
    cells: dict[tuple[str, str, int], dict] = {}
    for path in sorted(cells_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping unreadable %s: %s", path, exc)
            continue
        cell = payload.get("cell", {})
        u = cell.get("universe")
        s = cell.get("sub_period")
        p = cell.get("phase_offset")
        if u is None or s is None or p is None:
            logger.warning("Skipping payload without (universe,sub,phase): %s", path)
            continue
        cells[(u, s, int(p))] = payload
    return cells


def expected_cells() -> list[tuple[str, str, int]]:
    return [(u, s, p) for u, s, p in product(UNIVERSES, SUB_PERIODS, PHASES)]


def coverage_check(
    cells: dict[tuple[str, str, int], dict],
) -> tuple[list[tuple], list[tuple]]:
    expected = set(expected_cells())
    found = set(cells.keys())
    missing = sorted(expected - found)
    extra = sorted(found - expected)
    return missing, extra


def per_universe_per_subperiod_summary(
    cells: dict[tuple[str, str, int], dict],
) -> pd.DataFrame:
    rows = []
    for u, s in product(UNIVERSES, SUB_PERIODS):
        phase_alpha_ts: list[float] = []
        phase_sharpes: list[float] = []
        phase_alpha_pcts: list[float] = []
        phase_alpha_pct_ses: list[float] = []
        n_obs_total = 0
        for p in PHASES:
            cell = cells.get((u, s, p))
            if cell is None:
                continue
            stats = cell.get("stats", {})
            phase_alpha_ts.append(float(stats.get("alpha_t_4f", float("nan"))))
            phase_sharpes.append(float(stats.get("sharpe_net", float("nan"))))
            phase_alpha_pcts.append(float(stats.get("alpha_gross_4f", float("nan"))))
            phase_alpha_pct_ses.append(float(stats.get("alpha_se_4f", float("nan"))))
            n_obs_total += int(stats.get("n", 0))
        ats = np.array(phase_alpha_ts, dtype=float)
        sps = np.array(phase_sharpes, dtype=float)
        pcts = np.array(phase_alpha_pcts, dtype=float)
        ses = np.array(phase_alpha_pct_ses, dtype=float)
        rows.append(
            {
                "universe": u,
                "sub_period": s,
                "n_phases_present": int(np.isfinite(ats).sum()),
                "alpha_t_mean": float(np.nanmean(ats)) if ats.size else float("nan"),
                "alpha_t_min": float(np.nanmin(ats)) if ats.size else float("nan"),
                "alpha_t_max": float(np.nanmax(ats)) if ats.size else float("nan"),
                "alpha_t_range": (
                    float(np.nanmax(ats) - np.nanmin(ats))
                    if np.isfinite(ats).any()
                    else float("nan")
                ),
                "sharpe_net_mean": float(np.nanmean(sps)) if sps.size else float("nan"),
                "alpha_pct_mean": float(np.nanmean(pcts)) if pcts.size else float("nan"),
                "alpha_pct_se_mean": (float(np.nanmean(ses)) if ses.size else float("nan")),
                "n_obs_total": n_obs_total,
            }
        )
    return pd.DataFrame(rows)


def collect_pooled_returns(cells: dict[tuple[str, str, int], dict], universe: str) -> pd.DataFrame:
    """Legacy outer-joined panel (preserved for backwards-compat with tests).

    Per-strategy RW (issue #66) consumes raw ``long_net`` arrays directly
    via ``romano_wolf_critical``; no panel construction needed.
    """
    panels = []
    for s, p in product(SUB_PERIODS, PHASES):
        cell = cells.get((universe, s, p))
        if cell is None:
            continue
        rfp = cell.get("raw_returns_for_pooling", {})
        asof = rfp.get("asof", [])
        long_net = rfp.get("long_net", [])
        if not asof or not long_net or len(asof) != len(long_net):
            continue
        col_name = f"{universe}_{s}_p{p}"
        panels.append(pd.DataFrame({col_name: long_net}, index=pd.to_datetime(asof)))
    if not panels:
        return pd.DataFrame()
    return pd.concat(panels, axis=1).sort_index()


def primary_pooled_alpha_t(
    cells: dict[tuple[str, str, int], dict],
    universe: str = "U3",
) -> dict:
    """Mean αt and αpct across all 5 phases × 3 sub-periods for the primary
    universe (U3 by default per pre-reg). Reported alongside the
    per-cell scatter for the verdict."""
    summary = per_universe_per_subperiod_summary(cells)
    rows = summary.loc[summary["universe"] == universe]
    if rows.empty:
        return {"universe": universe, "n_cells": 0}
    alpha_t = float(rows["alpha_t_mean"].mean())
    alpha_pct = float(rows["alpha_pct_mean"].mean())
    alpha_pct_se = float(rows["alpha_pct_se_mean"].mean())
    sharpe_net = float(rows["sharpe_net_mean"].mean())
    return {
        "universe": universe,
        "n_subperiods": len(rows),
        "alpha_t_mean": alpha_t,
        "alpha_pct_mean": alpha_pct,
        "alpha_pct_se_mean": alpha_pct_se,
        "sharpe_net_mean": sharpe_net,
        "alpha_t_range_within_subperiod_max": float(rows["alpha_t_range"].max()),
        "alpha_t_subperiod_min": float(rows["alpha_t_mean"].min()),
        "alpha_t_subperiod_max": float(rows["alpha_t_mean"].max()),
        "alpha_t_range_across_subperiods": (
            float(rows["alpha_t_mean"].max() - rows["alpha_t_mean"].min())
        ),
    }


def romano_wolf_critical(
    cells: dict[tuple[str, str, int], dict],
    universe: str = "U3",
    *,
    n_bootstrap: int = 10000,
    mean_block_length: float = 4.0,
    seed: int = 42,
) -> dict:
    """Per-strategy block-bootstrap RW critical across all (sub-period × phase)
    cells for ``universe``.

    Each cell's ``long_net`` return series is one strategy; per-strategy
    independent stationary block bootstrap (issue #66) handles the
    stride-shifted disjoint asof calendars that ruled out the unstratified
    and stratified-by-sub-period variants.
    """
    returns_per_strategy: list[np.ndarray] = []
    for s, p in product(SUB_PERIODS, PHASES):
        cell = cells.get((universe, s, p))
        if cell is None:
            continue
        rfp = cell.get("raw_returns_for_pooling", {})
        long_net = rfp.get("long_net", [])
        if not long_net:
            continue
        returns_per_strategy.append(np.asarray(long_net, dtype=np.float64))

    if not returns_per_strategy:
        return {"universe": universe, "n_strategies": 0, "note": "no cells available"}

    rng = np.random.default_rng(seed)
    result = romano_wolf_step_down_per_strategy(
        returns_per_strategy,
        alpha=0.05,
        mean_block_length=mean_block_length,
        n_bootstrap=n_bootstrap,
        rng=rng,
    )
    return {
        "universe": universe,
        "n_strategies": result.n_strategies,
        "n_obs": result.n_obs,
        "n_bootstrap": result.n_bootstrap,
        "mean_block_length": result.mean_block_length,
        "max_observed_t": float(np.abs(result.observed_tstats).max()),
        "max_adjusted_critical": float(
            result.adjusted_critical[~np.isinf(result.adjusted_critical)].max()
            if (~np.isinf(result.adjusted_critical)).any()
            else float("inf")
        ),
        "n_rejected": int(result.rejected.sum()),
        "note": "per-strategy block bootstrap (issue #66)",
    }


def classify_verdict(
    primary: dict,
    bounds_result_for_pass_robust,
    bounds_result_for_pass_marginal,
    cross_universe_gap: float,
    sub_period_range: float,
    within_subperiod_range_max: float,
) -> tuple[str, str]:
    """Apply pre-reg decision tree. Returns ``(verdict, reason)``."""
    alpha_t = primary["alpha_t_mean"]
    if not np.isfinite(alpha_t):
        return "INCONCLUSIVE", "primary alpha_t non-finite"

    if cross_universe_gap > RECONSTRUCTION_DOMINANT_GAP:
        return (
            "RECONSTRUCTION_DOMINANT",
            f"|U1 αt − U3 αt| = {cross_universe_gap:.2f} > {RECONSTRUCTION_DOMINANT_GAP}",
        )
    if alpha_t >= PASS_ROBUST_ALPHA_T and bounds_result_for_pass_robust.lower_bound_excludes_zero:
        if (
            within_subperiod_range_max <= ALPHA_T_RANGE_STABLE
            and sub_period_range <= ALPHA_T_RANGE_REGIME_THRESHOLD
        ):
            return (
                "PASS_ROBUST",
                f"αt={alpha_t:.2f} ≥ {PASS_ROBUST_ALPHA_T}, lower-bound excludes 0, "
                f"within-sub-period range max={within_subperiod_range_max:.2f}, "
                f"cross-sub-period range={sub_period_range:.2f}",
            )
        return (
            "REGIME_DEPENDENT",
            f"αt={alpha_t:.2f} ≥ {PASS_ROBUST_ALPHA_T} but stability gates fail: "
            f"within={within_subperiod_range_max:.2f} (≤{ALPHA_T_RANGE_STABLE}?), "
            f"cross={sub_period_range:.2f} (≤{ALPHA_T_RANGE_REGIME_THRESHOLD}?)",
        )
    if (
        alpha_t >= PASS_MARGINAL_ALPHA_T
        and bounds_result_for_pass_marginal.lower_bound_excludes_zero
    ):
        return (
            "PASS_MARGINAL",
            f"αt={alpha_t:.2f} in [{PASS_MARGINAL_ALPHA_T}, {PASS_ROBUST_ALPHA_T}), "
            "lower-bound excludes 0",
        )
    if alpha_t < FAIL_ROBUST_ALPHA_T:
        return (
            "FAIL_ROBUST",
            f"αt={alpha_t:.2f} < {FAIL_ROBUST_ALPHA_T}, signal does not generalize "
            "to pre-2018 fresh OOS",
        )
    return (
        "INCONCLUSIVE",
        f"αt={alpha_t:.2f} in [{INCONCLUSIVE_LOWER_ALPHA_T}, {PASS_MARGINAL_ALPHA_T}), "
        "bounds CI may straddle zero",
    )


def build_verdict_payload(
    cells: dict[tuple[str, str, int], dict],
    *,
    n_bootstrap: int = 10000,
    seed: int = 42,
) -> dict:
    summary = per_universe_per_subperiod_summary(cells)
    primary = primary_pooled_alpha_t(cells, "U3")
    u1 = primary_pooled_alpha_t(cells, "U1")
    u2 = primary_pooled_alpha_t(cells, "U2")

    cross_universe_gap = (
        abs(u1.get("alpha_t_mean", float("nan")) - primary.get("alpha_t_mean", float("nan")))
        if np.isfinite(u1.get("alpha_t_mean", float("nan")))
        and np.isfinite(primary.get("alpha_t_mean", float("nan")))
        else float("nan")
    )

    primary_finite = (
        np.isfinite(primary.get("alpha_t_mean", float("nan")))
        and np.isfinite(primary.get("alpha_pct_mean", float("nan")))
        and np.isfinite(primary.get("alpha_pct_se_mean", float("nan")))
        and abs(primary.get("alpha_pct_se_mean", 0.0)) > 1e-9
    )
    if primary_finite:
        bounds_robust = andrews_manski_bounds(
            alpha_t=primary["alpha_t_mean"],
            alpha_pct=primary["alpha_pct_mean"],
            alpha_pct_se=abs(primary["alpha_pct_se_mean"]),
            bias_lower_pct=BIAS_RANGE_PCT[0],
            bias_upper_pct=BIAS_RANGE_PCT[1],
        )
        bounds_marginal = bounds_robust
        rw_result = romano_wolf_critical(cells, "U3", n_bootstrap=n_bootstrap, seed=seed)
    else:
        bounds_robust = None
        bounds_marginal = None
        rw_result = {
            "universe": "U3",
            "n_strategies": 0,
            "note": "primary U3 αt non-finite — bootstrap skipped",
        }

    sub_period_range = primary.get("alpha_t_range_across_subperiods", float("nan"))
    within_max = primary.get("alpha_t_range_within_subperiod_max", float("nan"))
    cross_universe_gap_safe = float(cross_universe_gap) if np.isfinite(cross_universe_gap) else 0.0
    sub_period_range_safe = float(sub_period_range) if np.isfinite(sub_period_range) else 0.0
    within_max_safe = float(within_max) if np.isfinite(within_max) else 0.0

    if not primary_finite or bounds_robust is None:
        verdict = "INCOMPLETE"
        reason = (
            "Primary U3 missing or insufficient cells; cannot compute "
            "Andrews-Manski bounds or Romano-Wolf critical value."
        )
    else:
        verdict, reason = classify_verdict(
            primary,
            bounds_robust,
            bounds_marginal,
            cross_universe_gap_safe,
            sub_period_range_safe,
            within_max_safe,
        )

    missing, extra = coverage_check(cells)

    return {
        "verdict": verdict,
        "verdict_reason": reason,
        "coverage": {
            "n_cells_present": len(cells),
            "n_cells_expected": len(expected_cells()),
            "missing": [list(m) for m in missing],
            "extra": [list(e) for e in extra],
        },
        "primary_U3": primary,
        "U2": u2,
        "U1": u1,
        "bounds_andrews_manski": (asdict(bounds_robust) if bounds_robust is not None else None),
        "romano_wolf": rw_result,
        "stability_gates": {
            "alpha_t_range_within_subperiod_max": within_max_safe,
            "alpha_t_range_within_subperiod_threshold": ALPHA_T_RANGE_STABLE,
            "alpha_t_range_across_subperiods": sub_period_range_safe,
            "alpha_t_range_across_subperiods_threshold": ALPHA_T_RANGE_REGIME_THRESHOLD,
            "cross_universe_gap_U1_U3": cross_universe_gap_safe,
            "cross_universe_gap_threshold": RECONSTRUCTION_DOMINANT_GAP,
        },
        "decision_thresholds": {
            "pass_robust_alpha_t": PASS_ROBUST_ALPHA_T,
            "pass_marginal_alpha_t": PASS_MARGINAL_ALPHA_T,
            "inconclusive_lower_alpha_t": INCONCLUSIVE_LOWER_ALPHA_T,
            "fail_robust_alpha_t": FAIL_ROBUST_ALPHA_T,
            "bias_range_pct": list(BIAS_RANGE_PCT),
        },
        "per_universe_per_subperiod": summary.to_dict(orient="records"),
    }


def render_postmortem_md(payload: dict) -> str:
    lines = []
    lines.append("# v9D Retrospective Pre-2018 — Verdict")
    lines.append("")
    lines.append(f"**Verdict: {payload['verdict']}**")
    lines.append("")
    lines.append(f"_Reason: {payload['verdict_reason']}_")
    lines.append("")
    lines.append("## Coverage")
    cov = payload["coverage"]
    lines.append(f"- {cov['n_cells_present']} / {cov['n_cells_expected']} cells present")
    n_missing = len(cov["missing"])
    if n_missing:
        if n_missing <= 5:
            lines.append(f"- Missing cells: {cov['missing']}")
        else:
            lines.append(f"- Missing cells: {n_missing} (head: {cov['missing'][:3]}...)")
    lines.append("")
    lines.append("## Primary U3 (cap-band NBER rebuild)")
    pri = payload["primary_U3"]
    lines.append(
        f"- Mean αt across 5 × 3 = 15 cells: **{pri.get('alpha_t_mean', float('nan')):.2f}**"
    )
    lines.append(f"- Mean αpct (annualized %): {pri.get('alpha_pct_mean', float('nan')):.2f}")
    lines.append(f"- Mean Sharpe net: {pri.get('sharpe_net_mean', float('nan')):.2f}")
    lines.append(
        f"- Within-sub-period αt range max: "
        f"{pri.get('alpha_t_range_within_subperiod_max', float('nan')):.2f}"
    )
    lines.append(
        f"- Across-sub-period αt range: "
        f"{pri.get('alpha_t_range_across_subperiods', float('nan')):.2f} "
        f"(min={pri.get('alpha_t_subperiod_min', float('nan')):.2f}, "
        f"max={pri.get('alpha_t_subperiod_max', float('nan')):.2f})"
    )
    lines.append("")
    lines.append("## Andrews-Manski bounds")
    bnd = payload["bounds_andrews_manski"]
    if bnd is None:
        lines.append("- _Skipped: primary U3 αt non-finite (incomplete coverage)._")
    else:
        lines.append(
            f"- Bias range pre-locked: [{bnd['bias_lower_pct']:.1f}, "
            f"{bnd['bias_upper_pct']:.1f}] %/y"
        )
        lines.append(f"- Unbiased αt CI: [{bnd['alpha_t_lower']:.2f}, {bnd['alpha_t_upper']:.2f}]")
        lines.append(
            f"- Unbiased αpct CI: [{bnd['alpha_pct_lower']:.2f}, {bnd['alpha_pct_upper']:.2f}] %/y"
        )
        lines.append(f"- Lower-bound excludes 0: {'YES' if bnd['alpha_t_lower'] > 0 else 'NO'}")
    lines.append("")
    lines.append("## Romano-Wolf step-down (n=15 family, U3 cells)")
    rw = payload["romano_wolf"]
    if "max_adjusted_critical" in rw:
        lines.append(f"- Max observed |t|: {rw['max_observed_t']:.2f}")
        lines.append(f"- Adjusted critical |t|: {rw['max_adjusted_critical']:.2f}")
        lines.append(f"- Strategies rejected: {rw['n_rejected']} / {rw['n_strategies']}")
        lines.append(f"- Bootstrap B={rw['n_bootstrap']}, mean_block={rw['mean_block_length']}")
        lines.append(
            "- Note: per-strategy independent block bootstrap (issue #66) operates on "
            "raw `long_net` returns, not Carhart-4F residuals. Per-strategy "
            "independence destroys cross-strategy correlation that would tighten "
            "the family-max critical, so this critical is closer to Bonferroni than "
            "the pre-reg's `~2.13` aspirational estimate. The αt-vs-PASS_MARGINAL "
            "gate remains the binding pre-reg criterion."
        )
    else:
        lines.append(f"- {rw.get('note', 'no result')}")
    lines.append("")
    lines.append("## Cross-universe sanity")
    sg = payload["stability_gates"]
    lines.append(
        f"- |U1 αt − U3 αt|: {sg['cross_universe_gap_U1_U3']:.2f} "
        f"(reconstruction-dominant threshold: {sg['cross_universe_gap_threshold']:.1f})"
    )
    lines.append("")
    lines.append("## Per-universe × per-sub-period αt")
    lines.append("")
    lines.append("| Universe | Sub-period | n_phases | αt mean | αt range | Sharpe net | n_obs |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in payload["per_universe_per_subperiod"]:
        lines.append(
            f"| {r['universe']} | {r['sub_period']} | {r['n_phases_present']} | "
            f"{r['alpha_t_mean']:.2f} | {r['alpha_t_range']:.2f} | "
            f"{r['sharpe_net_mean']:.2f} | {r['n_obs_total']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cells-dir", type=Path, default=CELLS_DIR)
    ap.add_argument(
        "--out-md",
        type=Path,
        default=REPO_ROOT / "docs" / "research" / "v9d_retrospective_pre_2018_verdict.md",
    )
    ap.add_argument(
        "--out-json",
        type=Path,
        default=REPO_ROOT / "docs" / "research" / "v9d_retrospective_pre_2018_verdict.json",
    )
    ap.add_argument("--bootstrap-n", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--log-level", default="INFO")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cells = load_cells(args.cells_dir)
    if not cells:
        logger.error("No cells found in %s — abort", args.cells_dir)
        return 2

    payload = build_verdict_payload(cells, n_bootstrap=args.bootstrap_n, seed=args.seed)
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(payload, indent=2, default=str))
    args.out_md.write_text(render_postmortem_md(payload))
    logger.info("Wrote %s", args.out_json)
    logger.info("Wrote %s", args.out_md)
    print(f"\nVERDICT: {payload['verdict']}")
    print(f"Reason: {payload['verdict_reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
