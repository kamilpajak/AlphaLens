"""Aggregate verdict for P/C abnormal volume retrospective pre-2018 battery.

Pre-reg locked in
``docs/research/preregistration/params_pc_abnormal_volume_retrospective_pre_2018_2026_05_05.json``
(post-amendment sha256 ``1debf1cc0ae8644d53955e7406007248e0052ab12559cff3f55fde688dbc8922``).

Layout: 2 universes × 3 sub-periods × 5 phase offsets = 30 cells.
Primary verdict on U2 (cache-only, includes pre-2018 delisted backfill).
Sensitivity check on U1 (legacy yaml universe).

Pre-reg decision tree (Bonferroni n=26 program-level, |t|≥2.85 binding):
- αt ≥ 3.5 + bounds excl 0 + stability gates → PASS_ROBUST
- αt ∈ [2.85, 3.5) + G2 PASS → PASS_MARGINAL
- αt ∈ [1.0, 2.85) → INCONCLUSIVE
- αt < 1.0 → FAIL_ROBUST_TENTATIVE (U2 only — requires U3 follow-up)
- |U1 − U2| > 3.0 → RECONSTRUCTION_DOMINANT
- G3/G4 dispersion fail → REGIME_DEPENDENT
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.backtest.bounds_inference import andrews_manski_bounds

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CELLS_DIR = REPO_ROOT / "docs" / "research" / "pc_abnormal_retrospective_pre_2018"
PRE_REG_PATH = (
    REPO_ROOT
    / "docs"
    / "research"
    / "preregistration"
    / "params_pc_abnormal_volume_retrospective_pre_2018_2026_05_05.json"
)

UNIVERSES = ("U1", "U2")
SUB_PERIODS = ("GFC_recovery", "mid_cycle_eu_debt", "late_cycle_china_shock")
PHASES = (0, 1, 2, 3, 4)

# Pre-reg locked thresholds (program-level Bonferroni n=26 binding).
BIAS_RANGE_PCT = (1.0, 2.0)
ALPHA_T_RANGE_STABLE = 1.5
ALPHA_T_RANGE_REGIME_THRESHOLD = 2.5
PASS_ROBUST_ALPHA_T = 3.5
PASS_MARGINAL_ALPHA_T = 2.85
INCONCLUSIVE_LOWER_ALPHA_T = 1.0
FAIL_ROBUST_ALPHA_T = 1.0
RECONSTRUCTION_DOMINANT_GAP = 3.0


def load_cells(cells_dir: Path) -> dict[tuple[str, str, int], dict]:
    cells: dict[tuple[str, str, int], dict] = {}
    for path in sorted(cells_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skip unreadable %s: %s", path, exc)
            continue
        cell = payload.get("cell", {})
        u, s, p = cell.get("universe"), cell.get("sub_period"), cell.get("phase_offset")
        if u and s and p is not None:
            cells[(u, s, int(p))] = payload
    return cells


def per_us_summary(cells: dict[tuple[str, str, int], dict]) -> pd.DataFrame:
    rows = []
    for u, s in product(UNIVERSES, SUB_PERIODS):
        ats, sps, pcts, ses = [], [], [], []
        n_obs = 0
        for p in PHASES:
            cell = cells.get((u, s, p))
            if cell is None:
                continue
            stats = cell.get("stats", {})
            ats.append(float(stats.get("alpha_t_4f", float("nan"))))
            sps.append(float(stats.get("sharpe_net", float("nan"))))
            pcts.append(float(stats.get("alpha_gross_4f", float("nan"))))
            ses.append(float(stats.get("alpha_se_4f", float("nan"))))
            n_obs += int(stats.get("n", 0))
        ats_arr = np.array(ats, dtype=float)
        rows.append(
            {
                "universe": u,
                "sub_period": s,
                "n_phases": int(np.isfinite(ats_arr).sum()),
                "alpha_t_mean": float(np.nanmean(ats_arr)) if ats_arr.size else float("nan"),
                "alpha_t_min": float(np.nanmin(ats_arr)) if ats_arr.size else float("nan"),
                "alpha_t_max": float(np.nanmax(ats_arr)) if ats_arr.size else float("nan"),
                "alpha_t_range": (
                    float(np.nanmax(ats_arr) - np.nanmin(ats_arr))
                    if np.isfinite(ats_arr).any()
                    else float("nan")
                ),
                "sharpe_net_mean": float(np.nanmean(sps)) if sps else float("nan"),
                "alpha_pct_mean": float(np.nanmean(pcts)) if pcts else float("nan"),
                "alpha_pct_se_mean": float(np.nanmean(ses)) if ses else float("nan"),
                "n_obs_total": n_obs,
            }
        )
    return pd.DataFrame(rows)


def primary_pooled(summary: pd.DataFrame, universe: str) -> dict:
    rows = summary.loc[summary["universe"] == universe]
    if rows.empty:
        return {"universe": universe, "n_subperiods": 0}
    return {
        "universe": universe,
        "n_subperiods": len(rows),
        "alpha_t_mean": float(rows["alpha_t_mean"].mean()),
        "alpha_pct_mean": float(rows["alpha_pct_mean"].mean()),
        "alpha_pct_se_mean": float(rows["alpha_pct_se_mean"].mean()),
        "sharpe_net_mean": float(rows["sharpe_net_mean"].mean()),
        "alpha_t_within_subperiod_range_max": float(rows["alpha_t_range"].max()),
        "alpha_t_subperiod_min": float(rows["alpha_t_mean"].min()),
        "alpha_t_subperiod_max": float(rows["alpha_t_mean"].max()),
        "alpha_t_cross_subperiod_range": (
            float(rows["alpha_t_mean"].max() - rows["alpha_t_mean"].min())
        ),
    }


def classify(
    primary: dict, u1: dict, summary: pd.DataFrame, bounds_lower_t: float
) -> tuple[str, str]:
    alpha_t = primary.get("alpha_t_mean", float("nan"))
    if not np.isfinite(alpha_t):
        return "INCONCLUSIVE", "primary alpha_t non-finite"

    cross_gap = (
        abs(u1.get("alpha_t_mean", float("nan")) - alpha_t)
        if np.isfinite(u1.get("alpha_t_mean", float("nan")))
        else float("nan")
    )
    sub_range = primary["alpha_t_cross_subperiod_range"]
    within_max = primary["alpha_t_within_subperiod_range_max"]

    if np.isfinite(cross_gap) and cross_gap > RECONSTRUCTION_DOMINANT_GAP:
        return (
            "RECONSTRUCTION_DOMINANT",
            f"|U1 αt − U2 αt|={cross_gap:.2f} > {RECONSTRUCTION_DOMINANT_GAP}",
        )

    if alpha_t >= PASS_ROBUST_ALPHA_T and bounds_lower_t > 0:
        if within_max <= ALPHA_T_RANGE_STABLE and sub_range <= ALPHA_T_RANGE_REGIME_THRESHOLD:
            return "PASS_ROBUST", (
                f"αt={alpha_t:.2f} ≥ {PASS_ROBUST_ALPHA_T}, bounds-lower-t={bounds_lower_t:.2f} > 0, "
                f"within-sub max={within_max:.2f}, cross-sub={sub_range:.2f}"
            )
        return "REGIME_DEPENDENT", (
            f"αt={alpha_t:.2f} ≥ {PASS_ROBUST_ALPHA_T} but stability gates fail: "
            f"within={within_max:.2f} (≤{ALPHA_T_RANGE_STABLE}?), cross={sub_range:.2f} (≤{ALPHA_T_RANGE_REGIME_THRESHOLD}?)"
        )

    if alpha_t >= PASS_MARGINAL_ALPHA_T and bounds_lower_t > 0:
        return "PASS_MARGINAL", (
            f"αt={alpha_t:.2f} ∈ [{PASS_MARGINAL_ALPHA_T}, {PASS_ROBUST_ALPHA_T}), bounds-lower-t={bounds_lower_t:.2f} > 0"
        )

    if alpha_t < FAIL_ROBUST_ALPHA_T:
        return "FAIL_ROBUST_TENTATIVE", (
            f"αt={alpha_t:.2f} < {FAIL_ROBUST_ALPHA_T} on U2 — pre-reg requires U3 follow-up before class closure"
        )

    return "INCONCLUSIVE", (
        f"αt={alpha_t:.2f} ∈ [{INCONCLUSIVE_LOWER_ALPHA_T}, {PASS_MARGINAL_ALPHA_T}), "
        f"bounds-lower-t={bounds_lower_t:.2f}"
    )


def collect_pooled_returns_per_subperiod(
    cells: dict[tuple[str, str, int], dict], universe: str
) -> dict[str, pd.DataFrame]:
    """Per-sub-period DataFrames of phase-strategy returns.

    Mirrors aggregate_v9d_retrospective_verdict.collect_pooled_returns_per_subperiod
    for stratified Romano-Wolf bootstrap (Bug 2 fix per zen review 2026-05-05)."""
    out: dict[str, pd.DataFrame] = {}
    for s in SUB_PERIODS:
        panels = []
        for p in PHASES:
            cell = cells.get((universe, s, p))
            if cell is None:
                continue
            rfp = cell.get("raw_returns_for_pooling", {})
            asof = rfp.get("asof", [])
            long_net = rfp.get("long_net", [])
            if not asof or not long_net or len(asof) != len(long_net):
                continue
            panels.append(pd.DataFrame({f"p{p}": long_net}, index=pd.to_datetime(asof)))
        if not panels:
            continue
        sub = pd.concat(panels, axis=1).sort_index().dropna(axis=0, how="any")
        if not sub.empty:
            out[s] = sub
    return out


def romano_wolf_critical_stratified(
    cells: dict[tuple[str, str, int], dict],
    universe: str = "U2",
    *,
    n_bootstrap: int = 10000,
    mean_block_length: float = 4.0,
    seed: int = 42,
) -> dict:
    """Skipped-by-design: stratified RW does not apply to stride-disjoint phases.

    Each (sub-period, phase) cell has its own asof calendar (5d stride-shifted
    so phases within a sub-period sample disjoint trading days). Concat-by-index
    + dropna-any collapses the panel to 0 rows. Concat-by-index without dropna
    yields a 95%-NaN panel that breaks the bootstrap.

    The proper approach is per-strategy independent block bootstrap (each
    strategy's own time-series gets its own block resample, joint t-stat
    distribution aggregated across strategies). Implementation is deferred —
    pre-reg explicitly permits naive Bonferroni fallback (|t|≥2.85 program-level)
    which is the binding gate for this experiment regardless of RW critical.
    """
    return {
        "universe": universe,
        "n_strategies": 0,
        "note": (
            "RW skipped: stride-disjoint phase strategies not amenable to "
            "stratified-by-sub-period bootstrap; pre-reg fallback to naive "
            "Bonferroni |t|≥2.85 binds the verdict"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cells-dir", type=Path, default=CELLS_DIR)
    ap.add_argument(
        "--out-md",
        type=Path,
        default=REPO_ROOT / "docs" / "research" / "pc_abnormal_retrospective_pre_2018_verdict.md",
    )
    ap.add_argument(
        "--out-json",
        type=Path,
        default=REPO_ROOT / "docs" / "research" / "pc_abnormal_retrospective_pre_2018_verdict.json",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    cells = load_cells(args.cells_dir)
    expected = [(u, s, p) for u, s, p in product(UNIVERSES, SUB_PERIODS, PHASES)]
    missing = [c for c in expected if c not in cells]
    logger.info("Loaded %d/30 cells; missing=%d", len(cells), len(missing))
    if missing:
        for c in missing[:10]:
            logger.warning("MISSING cell: %s", c)

    summary = per_us_summary(cells)
    primary = primary_pooled(summary, "U2")
    u1 = primary_pooled(summary, "U1")

    # Andrews-Manski bounds CI on primary U2 αt
    bounds_lower_t = float("nan")
    bounds_upper_t = float("nan")
    if np.isfinite(primary.get("alpha_pct_mean", float("nan"))) and np.isfinite(
        primary.get("alpha_pct_se_mean", float("nan"))
    ):
        try:
            bounds = andrews_manski_bounds(
                alpha_t=primary["alpha_t_mean"],
                alpha_pct=primary["alpha_pct_mean"],
                alpha_pct_se=primary["alpha_pct_se_mean"],
                bias_lower_pct=BIAS_RANGE_PCT[0],
                bias_upper_pct=BIAS_RANGE_PCT[1],
            )
            bounds_lower_t = float(bounds.alpha_t_lower)
            bounds_upper_t = float(bounds.alpha_t_upper)
        except Exception as exc:
            logger.warning("Bounds inference failed: %s", exc)

    verdict, reason = classify(primary, u1, summary, bounds_lower_t)

    # Stratified Romano-Wolf (per-sub-period block bootstrap)
    rw_result = romano_wolf_critical_stratified(cells, universe="U2")
    logger.info(
        "Stratified RW (U2): n_strategies=%d max_observed_t=%.2f max_adjusted_critical=%.2f n_rejected=%d",
        rw_result.get("n_strategies", 0),
        rw_result.get("max_observed_t", float("nan")),
        rw_result.get("max_adjusted_critical", float("nan")),
        rw_result.get("n_rejected", 0),
    )

    # Build markdown postmortem
    lines = []
    lines.append(f"# P/C abnormal volume retrospective pre-2018 — {verdict}")
    lines.append("")
    lines.append(f"**Verdict:** {verdict}")
    lines.append("")
    lines.append(f"**Reason:** {reason}")
    lines.append("")
    lines.append("## Headline (U2 primary universe)")
    lines.append("")
    if primary.get("n_subperiods"):
        lines.append(f"- αt mean (5 phases × 3 sub-periods): **{primary['alpha_t_mean']:+.2f}**")
        lines.append(f"- αpct mean: {primary['alpha_pct_mean'] * 100:+.2f}%/y")
        lines.append(f"- Sharpe net mean: {primary['sharpe_net_mean']:.2f}")
        lines.append(
            f"- Within-sub-period αt range max: {primary['alpha_t_within_subperiod_range_max']:.2f} (gate ≤{ALPHA_T_RANGE_STABLE})"
        )
        lines.append(
            f"- Cross-sub-period αt range: {primary['alpha_t_cross_subperiod_range']:.2f} (gate ≤{ALPHA_T_RANGE_REGIME_THRESHOLD})"
        )
    lines.append("")
    lines.append(
        f"**Andrews-Manski bounds CI** (bias range {BIAS_RANGE_PCT[0]:.1f}-{BIAS_RANGE_PCT[1]:.1f}%/y, mapped to t-stat):"
    )
    lines.append(f"- Lower-bound t: {bounds_lower_t:+.2f}")
    lines.append(f"- Upper-bound t: {bounds_upper_t:+.2f}")
    lines.append(f"- Excludes 0: {'YES' if bounds_lower_t > 0 else 'NO'}")
    lines.append("")
    lines.append("## Per-universe × sub-period αt")
    lines.append("")
    lines.append("| | GFC_recovery | mid_cycle_eu_debt | late_cycle_china_shock |")
    lines.append("|---|---|---|---|")
    for u in UNIVERSES:
        cells_for_u = []
        for s in SUB_PERIODS:
            r = summary.loc[(summary["universe"] == u) & (summary["sub_period"] == s)]
            if r.empty:
                cells_for_u.append("missing")
            else:
                cells_for_u.append(f"{float(r['alpha_t_mean'].iloc[0]):+.2f}")
        lines.append(f"| **{u}** | {cells_for_u[0]} | {cells_for_u[1]} | {cells_for_u[2]} |")
    lines.append("")
    lines.append("## Bonferroni accounting")
    lines.append("")
    lines.append("- Program-level n=26, naive Bonferroni |t|≥2.85 (binding per pre-reg)")
    lines.append(
        "- Romano-Wolf adjusted critical not computed (panel collapses on disjoint sub-period dates per v9d_retrospective experience)"
    )
    lines.append("")
    lines.append("## Coverage")
    lines.append("")
    lines.append(f"- Cells loaded: {len(cells)}/30")
    if missing:
        lines.append(
            f"- Missing cells: {len(missing)} — {missing[:5]}{'...' if len(missing) > 5 else ''}"
        )
    lines.append("")
    lines.append("## Pre-reg sha256")
    lines.append("")
    lines.append(
        "- Original lock: `03ddf4b7906ed07049bbb74dcdd599afa29abda1e8c4f6551a1876c78e45e689`"
    )
    lines.append(
        "- Post-amendment lock (log_marketCap dropped pre-first-run): `1debf1cc0ae8644d53955e7406007248e0052ab12559cff3f55fde688dbc8922`"
    )
    lines.append("")

    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text("\n".join(lines) + "\n")
    logger.info("Wrote %s", args.out_md)

    payload = {
        "verdict": verdict,
        "reason": reason,
        "primary_U2": primary,
        "sensitivity_U1": u1,
        "romano_wolf_stratified_U2": rw_result,
        "summary": summary.to_dict(orient="records"),
        "bounds_lower_t": bounds_lower_t,
        "bounds_upper_t": bounds_upper_t,
        "bias_range_pct": list(BIAS_RANGE_PCT),
        "thresholds": {
            "PASS_ROBUST_alpha_t": PASS_ROBUST_ALPHA_T,
            "PASS_MARGINAL_alpha_t": PASS_MARGINAL_ALPHA_T,
            "INCONCLUSIVE_lower": INCONCLUSIVE_LOWER_ALPHA_T,
            "alpha_t_range_stable": ALPHA_T_RANGE_STABLE,
            "alpha_t_range_regime": ALPHA_T_RANGE_REGIME_THRESHOLD,
            "reconstruction_dominant_gap": RECONSTRUCTION_DOMINANT_GAP,
        },
        "n_cells_loaded": len(cells),
        "n_cells_missing": len(missing),
        "missing_cells": [list(c) for c in missing],
    }
    args.out_json.write_text(json.dumps(payload, indent=2, default=str))
    logger.info("Wrote %s", args.out_json)
    print(f"\n=== {verdict} ===\n{reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
