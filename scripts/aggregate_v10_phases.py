"""Aggregate 5 v10 phases into pooled cross-phase verdict.

Loads docs/research/v10_drawdown_overlay/holdout_p[0-4].json, concatenates
raw_returns_for_pooling (base_net + overlay_net) across phases (each phase
has a distinct asof calendar — stride=5 with phase_offset 0..4 gives
5 disjoint sub-calendars, so concat is leak-free), runs a single Ledoit-
Wolf paired circular block-bootstrap on the pooled series, and evaluates
the 6 pre-reg gates plus the cross-phase G6 dispersion gate.

Writes:
- docs/research/v10_drawdown_overlay/multi_phase_verdict.json
- docs/research/v10_drawdown_overlay/multi_phase_verdict.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alphalens.backtest.sharpe_inference import block_bootstrap_sharpe_diff

PHASE_DIR = Path("docs/research/v10_drawdown_overlay")
PHASES = list(range(5))
PERIODS_PER_YEAR = 52  # stride=5 ≈ weekly
BLOCK_SIZE = 21
N_BOOTSTRAP = 10000
SEED = 0

V9D_TARGET_ALPHA_T = 2.29
G1_T_THRESHOLD = 2.5
G1_P_THRESHOLD = 0.01
G2_SHARPE_IMPROVEMENT = 0.30
G3_MAXDD_RATIO = 0.7
G4_TOLERANCE = 0.5
G6_RANGE = 0.5


def _load_phases() -> list[dict]:
    out: list[dict] = []
    for p in PHASES:
        path = PHASE_DIR / f"holdout_p{p}.json"
        out.append(json.loads(path.read_text()))
    return out


def _pool_returns(payloads: list[dict]) -> tuple[np.ndarray, np.ndarray, list[str]]:
    base_all: list[float] = []
    overlay_all: list[float] = []
    asof_all: list[str] = []
    for pl in payloads:
        rfp = pl["raw_returns_for_pooling"]
        base_all.extend(rfp["base_net"])
        overlay_all.extend(rfp["overlay_net"])
        asof_all.extend(rfp["asof"])
    # Sort by asof (chronological pooling). Phase calendars are disjoint, so
    # this gives a strictly-increasing time series.
    order = sorted(range(len(asof_all)), key=lambda i: asof_all[i])
    base_arr = np.asarray([base_all[i] for i in order], dtype=float)
    overlay_arr = np.asarray([overlay_all[i] for i in order], dtype=float)
    asof_sorted = [asof_all[i] for i in order]
    return base_arr, overlay_arr, asof_sorted


def _per_phase_table(payloads: list[dict]) -> list[dict]:
    rows = []
    for i, pl in enumerate(payloads):
        b = pl["base_stats"]
        o = pl["overlay_stats"]
        sd = pl["sharpe_diff"]
        rows.append(
            {
                "phase": i,
                "n": b["n"],
                "base_alpha_t": b["alpha_t_4f"],
                "base_sharpe_net": b["sharpe_net"],
                "base_mdd_net": b["max_drawdown_net"],
                "overlay_alpha_t": o["alpha_t_4f"],
                "overlay_sharpe_net": o["sharpe_net"],
                "overlay_mdd_net": o["max_drawdown_net"],
                "sharpe_improvement": o["sharpe_net"] - b["sharpe_net"],
                "mdd_ratio": (
                    abs(o["max_drawdown_net"]) / abs(b["max_drawdown_net"])
                    if b["max_drawdown_net"] != 0
                    else float("nan")
                ),
                "single_phase_t": sd["t_stat"],
                "single_phase_p": sd["p_value_one_sided"],
                "weight_min": pl["weights_summary"]["min"],
                "weight_max": pl["weights_summary"]["max"],
                "weight_mean": pl["weights_summary"]["mean"],
            }
        )
    return rows


def main() -> int:
    payloads = _load_phases()
    rows = _per_phase_table(payloads)

    # Pooled bootstrap.
    base, overlay, _asofs = _pool_returns(payloads)
    pooled_diff = block_bootstrap_sharpe_diff(
        overlay,
        base,
        periods_per_year=PERIODS_PER_YEAR,
        block_size=BLOCK_SIZE,
        n_bootstrap=N_BOOTSTRAP,
        seed=SEED,
    )

    # Cross-phase aggregates.
    sharpe_imps = [r["sharpe_improvement"] for r in rows]
    base_alpha_ts = [r["base_alpha_t"] for r in rows]
    weight_mins = [r["weight_min"] for r in rows]
    weight_maxs = [r["weight_max"] for r in rows]

    mean_alpha_t = float(np.mean(base_alpha_ts))
    mean_sharpe_imp = float(np.mean(sharpe_imps))
    sharpe_imp_range = float(max(sharpe_imps) - min(sharpe_imps))
    overall_w_min = float(min(weight_mins))
    overall_w_max = float(max(weight_maxs))

    # Gate evaluations (multi-phase pooled).
    g1 = pooled_diff.t_stat >= G1_T_THRESHOLD and pooled_diff.p_value_one_sided < G1_P_THRESHOLD
    g2 = mean_sharpe_imp >= G2_SHARPE_IMPROVEMENT
    # G3: pooled MaxDD comparison — use mean of per-phase ratios as the
    # per-phase MDD reduction summary. Overall PASS = mean ratio ≤ 0.7.
    mdd_ratios = [r["mdd_ratio"] for r in rows if not np.isnan(r["mdd_ratio"])]
    mean_mdd_ratio = float(np.mean(mdd_ratios)) if mdd_ratios else float("nan")
    g3 = mean_mdd_ratio <= G3_MAXDD_RATIO
    g4 = abs(mean_alpha_t - V9D_TARGET_ALPHA_T) <= G4_TOLERANCE
    g5 = (overall_w_min >= 0.0 - 1e-12) and (overall_w_max <= 1.0 + 1e-12)
    g6 = sharpe_imp_range <= G6_RANGE

    all_pass = g1 and g2 and g3 and g4 and g5 and g6
    verdict = "PASS" if all_pass else "FAIL"

    out_payload = {
        "preregistration_id": "v10_drawdown_overlay_on_v9D_options_2026_05_04",
        "verdict": verdict,
        "n_phases": len(rows),
        "n_pooled_obs": len(base),
        "per_phase": rows,
        "cross_phase_aggregates": {
            "mean_base_alpha_t": mean_alpha_t,
            "mean_sharpe_improvement": mean_sharpe_imp,
            "sharpe_improvement_range": sharpe_imp_range,
            "mean_mdd_ratio": mean_mdd_ratio,
            "overall_weight_min": overall_w_min,
            "overall_weight_max": overall_w_max,
        },
        "pooled_bootstrap": {
            "sharpe_a_overlay": pooled_diff.sharpe_a,
            "sharpe_b_base": pooled_diff.sharpe_b,
            "sharpe_diff": pooled_diff.sharpe_diff,
            "bootstrap_se": pooled_diff.bootstrap_se,
            "t_stat": pooled_diff.t_stat,
            "p_value_one_sided": pooled_diff.p_value_one_sided,
            "ci_lower": pooled_diff.ci_lower,
            "ci_upper": pooled_diff.ci_upper,
            "n_bootstrap": pooled_diff.n_bootstrap,
            "block_size": pooled_diff.block_size,
        },
        "gates": {
            "g1_sharpe_diff_significance": {
                "rule": f"pooled bootstrap t≥{G1_T_THRESHOLD} AND p<{G1_P_THRESHOLD}",
                "t_stat": pooled_diff.t_stat,
                "p_value": pooled_diff.p_value_one_sided,
                "pass": bool(g1),
            },
            "g2_sharpe_improvement_magnitude": {
                "rule": f"mean Δ Sharpe ≥ {G2_SHARPE_IMPROVEMENT}",
                "mean_improvement": mean_sharpe_imp,
                "pass": bool(g2),
            },
            "g3_maxdd_reduction": {
                "rule": (
                    f"mean(MDD_overlay/MDD_base) ≤ {G3_MAXDD_RATIO} "
                    "(≥30% relative truncation across phases)"
                ),
                "mean_mdd_ratio": mean_mdd_ratio,
                "per_phase_ratios": mdd_ratios,
                "pass": bool(g3),
            },
            "g4_base_consistency": {
                "rule": f"|mean base αt − {V9D_TARGET_ALPHA_T}| ≤ {G4_TOLERANCE}",
                "mean_base_alpha_t": mean_alpha_t,
                "target": V9D_TARGET_ALPHA_T,
                "diff": abs(mean_alpha_t - V9D_TARGET_ALPHA_T),
                "pass": bool(g4),
            },
            "g5_weight_bound_invariant": {
                "rule": "overlay weight ∈ [0.0, 1.0] across all phases",
                "overall_weight_min": overall_w_min,
                "overall_weight_max": overall_w_max,
                "pass": bool(g5),
            },
            "g6_phase_dispersion": {
                "rule": f"max(Δ Sharpe) − min(Δ Sharpe) ≤ {G6_RANGE} across 5 phases",
                "sharpe_improvement_range": sharpe_imp_range,
                "per_phase_improvements": sharpe_imps,
                "pass": bool(g6),
            },
        },
    }

    out_json = PHASE_DIR / "multi_phase_verdict.json"
    out_json.write_text(json.dumps(out_payload, indent=2, default=str))

    out_md_lines = [
        f"# v10 drawdown-control overlay — multi-phase verdict: **{verdict}**",
        "",
        "Pre-reg id: `v10_drawdown_overlay_on_v9D_options_2026_05_04`",
        f"Phases: {len(rows)} | Pooled observations: {len(base)} | Bootstrap: "
        f"block={BLOCK_SIZE}d, n={pooled_diff.n_bootstrap}",
        "",
        "## Per-phase results",
        "",
        "| Phase | n | Base αt | Base Sh net | Base MDD | Overlay αt | Overlay Sh | "
        "Overlay MDD | MDD ratio | Δ Sharpe | bootstrap t | w mean |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        out_md_lines.append(
            f"| {r['phase']} | {r['n']} | {r['base_alpha_t']:+.2f} | "
            f"{r['base_sharpe_net']:.2f} | {r['base_mdd_net'] * 100:+.2f}% | "
            f"{r['overlay_alpha_t']:+.2f} | {r['overlay_sharpe_net']:.2f} | "
            f"{r['overlay_mdd_net'] * 100:+.2f}% | {r['mdd_ratio']:.3f} | "
            f"{r['sharpe_improvement']:+.3f} | {r['single_phase_t']:+.2f} | "
            f"{r['weight_mean']:.2f} |"
        )

    out_md_lines += [
        "",
        "## Pooled bootstrap (cross-phase Sharpe-diff)",
        "",
        f"- Sharpe(overlay) = {pooled_diff.sharpe_a:+.3f}",
        f"- Sharpe(base)    = {pooled_diff.sharpe_b:+.3f}",
        f"- Sharpe diff     = {pooled_diff.sharpe_diff:+.3f}",
        f"- bootstrap t     = {pooled_diff.t_stat:+.3f}",
        f"- p (1-sided)     = {pooled_diff.p_value_one_sided:.4f}",
        f"- 95% CI          = [{pooled_diff.ci_lower:+.3f}, {pooled_diff.ci_upper:+.3f}]",
        "",
        "## Gates",
        "",
        "| Gate | Rule | Observed | Verdict |",
        "|---|---|---|---|",
        f"| G1 | pooled t≥{G1_T_THRESHOLD} AND p<{G1_P_THRESHOLD} | "
        f"t={pooled_diff.t_stat:+.2f}, p={pooled_diff.p_value_one_sided:.3f} | "
        f"{'✅ PASS' if g1 else '❌ FAIL'} |",
        f"| G2 | mean Δ Sharpe ≥ {G2_SHARPE_IMPROVEMENT} | {mean_sharpe_imp:+.3f} | "
        f"{'✅ PASS' if g2 else '❌ FAIL'} |",
        f"| G3 | mean MDD ratio ≤ {G3_MAXDD_RATIO} | {mean_mdd_ratio:.3f} | "
        f"{'✅ PASS' if g3 else '❌ FAIL'} |",
        f"| G4 | |mean αt − {V9D_TARGET_ALPHA_T}| ≤ {G4_TOLERANCE} | "
        f"αt={mean_alpha_t:+.2f}, diff={abs(mean_alpha_t - V9D_TARGET_ALPHA_T):.3f} | "
        f"{'✅ PASS' if g4 else '❌ FAIL'} |",
        f"| G5 | weight ∈ [0,1] | min={overall_w_min:.3f}, max={overall_w_max:.3f} | "
        f"{'✅ PASS' if g5 else '❌ FAIL'} |",
        f"| G6 | Δ Sharpe range ≤ {G6_RANGE} | {sharpe_imp_range:.3f} | "
        f"{'✅ PASS' if g6 else '❌ FAIL'} |",
        "",
        f"## Verdict: **{verdict}**",
        "",
    ]
    if not all_pass:
        out_md_lines.append(
            "Failed gates: "
            + ", ".join(
                name
                for name, p in [
                    ("G1", g1),
                    ("G2", g2),
                    ("G3", g3),
                    ("G4", g4),
                    ("G5", g5),
                    ("G6", g6),
                ]
                if not p
            )
        )

    (PHASE_DIR / "multi_phase_verdict.md").write_text("\n".join(out_md_lines) + "\n")
    print(f"VERDICT: {verdict}")
    print(
        f"Failed gates: {[n for n, p in [('G1', g1), ('G2', g2), ('G3', g3), ('G4', g4), ('G5', g5), ('G6', g6)] if not p]}"
    )
    print(f"Wrote {out_json}")
    print(f"Wrote {PHASE_DIR / 'multi_phase_verdict.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
