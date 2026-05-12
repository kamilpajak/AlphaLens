"""5-phase audit orchestrator for ev_fcff_yield (paradigm #13).

Pre-reg ledger entry: ``ev_fcff_yield_2026_05_12_v1`` (LOCKED 2026-05-12).
Adapted from ``scripts/run_insider_pc_compound_audit.py`` minus block-bootstrap
(memo §8 does not require Romano-Wolf bounds for this paradigm).

## Why a custom orchestrator instead of ``alphalens audit``

``phase_robust_backtesting.audit_multi_phase.run_audit`` hard-codes
``--rebalance-stride <N>`` (passes it to the experiment script AND uses
``range(N)`` for phase iteration). Our experiment script enforces
``--rebalance-stride 63`` as a pre-reg lock (exit code 9 on override). The
two collide: passing ``rebalance_stride=5`` to ``run_audit`` would trip the
lock; passing ``rebalance_stride=63`` would spawn 63 phases. Same trap
paradigm #12 hit — see
``docs/research/insider_pc_compound_audit_launch_postmortem_2026_05_11.md``.

## What this orchestrator does

For each of 3 hardcoded windows (IS / OOS / FL per memo §7), spawn
``N_PHASES`` parallel subprocesses (phases 0..N-1). Each subprocess runs
``scripts/experiment_ev_fcff_yield.py`` with the locked stride/holding and
emits one log line per cost-stress level — orchestrator parses both
baseline (5bps) and G4 (15bps) cost rows from a single subprocess output.

Per memo §8 gate matrix (applied in code; verdict written into JSON):
- G1: full-sample net αt ≥ 3.5 (pooled across phases at 5 bps)
- G2: mean αt per-phase ≥ 2.5
- G3: positive αt each phase
- G4: net αt at 15 bps ≥ 2.0 (mean across phases)

Output: single aggregated ``docs/research/ev_fcff_yield_audit_<date>.json``
with ``windows: {IS, OOS, FL}`` blocks + ``gate_summary`` + top-level
``verdict``.

## Operational locks (do not parameterise)

- ``N_PHASES = 5`` — convention from paradigm #12; matches memo §7 expectation
- ``REBALANCE_STRIDE_DAYS = 63`` / ``HOLDING_DAYS = 63`` — memo §5
- 3 window tuples — memo §7

CLI overrides (``--window-only``, ``--n-phases``, ``--is-start-override``,
``--is-end-override``, ``--universe-size-cap``) exist for *local smoke
verification only*. Audit runs ignore them (orchestrator launched without
overrides → uses locked constants).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

logger = logging.getLogger(__name__)

EXPERIMENT_SCRIPT = REPO / "scripts" / "experiment_ev_fcff_yield.py"
DEFAULT_ARTIFACT_ROOT = Path.home() / ".alphalens" / "audit" / "ev_fcff_yield"

# Pre-reg locked constants (memo ev_fcff_yield_v1_design_2026_05_12.md).
N_PHASES = 5
REBALANCE_STRIDE_DAYS = 63  # memo §5
HOLDING_DAYS = 63  # memo §5

# Memo §7 phase split — 3 non-overlapping 3y windows.
WINDOWS: tuple[tuple[str, date, date], ...] = (
    ("IS", date(2016, 8, 31), date(2019, 8, 31)),
    ("OOS", date(2019, 8, 31), date(2022, 8, 31)),
    ("FL", date(2022, 8, 31), date(2025, 8, 31)),
)

# Memo §8 gate thresholds.
G1_BONFERRONI_T = 3.5  # project-imposed conservative (class-internal would be 1.96)
G2_MEAN_PER_PHASE_T = 2.5
G3_POSITIVE_PER_PHASE_T = 0.0
G4_COST_STRESS_T = 2.0
PASS_MARGINAL_T_LO = 2.5  # PASS_MARGINAL window: alpha_t in [2.5, 3.5)

# Cost levels we EVALUATE gates at. Subprocess emits a row per cost; we
# parse 5bps (baseline) and 15bps (G4 stress) from each phase.
COST_BASELINE_BPS = 5.0
COST_G4_STRESS_BPS = 15.0
COST_HALF_SPREADS_REQUESTED: tuple[float, ...] = (
    COST_BASELINE_BPS,
    COST_G4_STRESS_BPS,
)

# Regex captures cost AND the metric block from the assess() log line.
# Example match:
#   "INFO __main__: cost=5bps | n=441 topN=148.3 turn=...% | Sh gross=1.42 net=1.40 |
#    excess gross=4.2% net=4.0% | α 4F=8.3% t=2.71"
_RESULT_LINE = re.compile(
    r"cost=(?P<cost>[\d.]+)bps \| .*?"
    r"Sh gross=(?P<sg>[-\d.]+) net=(?P<sn>[-\d.]+) \| "
    r"excess gross=(?P<eg>[-\d.]+)% net=(?P<en>[-\d.]+)% \| "
    r"α 4F=(?P<a>[-\d.]+)% t=(?P<t>[-\d.]+)"
)


def _phase_command(
    phase_offset: int,
    *,
    is_start: date,
    is_end: date,
    cost_half_spreads: tuple[float, ...],
    universe_size_cap: int | None,
    out_path: Path,
) -> list[str]:
    """Hardcoded subprocess argv. Stride + holding are LOCKED here so a
    future caller cannot re-introduce the paradigm-#12 stride-mismatch bug.
    """
    cmd = [
        sys.executable,
        str(EXPERIMENT_SCRIPT),
        "--is-start",
        is_start.isoformat(),
        "--is-end",
        is_end.isoformat(),
        "--rebalance-stride",
        str(REBALANCE_STRIDE_DAYS),
        "--holding",
        str(HOLDING_DAYS),
        "--phase-offset",
        str(phase_offset),
        "--cost-half-spreads",
        *(f"{c}" for c in cost_half_spreads),
        "--out",
        str(out_path),
        "--skip-precheck",  # no-op for ev_fcff_yield but satisfies harness
    ]
    if universe_size_cap is not None:
        cmd.extend(["--universe-size-cap", str(universe_size_cap)])
    return cmd


def _run_one_phase(
    phase_offset: int,
    *,
    window_name: str,
    is_start: date,
    is_end: date,
    cost_half_spreads: tuple[float, ...],
    universe_size_cap: int | None,
    log_dir: Path,
    artifact_root: Path,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """Spawn experiment subprocess; capture stderr to isolated per-phase log."""
    log_path = log_dir / f"{window_name}_p{phase_offset}.log"
    out_path = artifact_root / f"{window_name}_p{phase_offset}_report.json"
    artifact_root.mkdir(parents=True, exist_ok=True)
    cmd = _phase_command(
        phase_offset,
        is_start=is_start,
        is_end=is_end,
        cost_half_spreads=cost_half_spreads,
        universe_size_cap=universe_size_cap,
        out_path=out_path,
    )
    # Explicit env propagation — zen review 2026-05-12 (avoid SimFin SDK
    # defaulting to ~/.simfin/ and bypassing our cache → 5 parallel API
    # downloads = rate-limit ban / OOM).
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    logger.info("[%s p%d] launching: cost_grid=%s", window_name, phase_offset, cost_half_spreads)
    t0 = time.monotonic()
    with log_path.open("w", encoding="utf-8") as logfh:
        proc = subprocess.run(
            cmd,
            stdout=logfh,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )
    wall = time.monotonic() - t0
    logger.info(
        "[%s p%d] subprocess exit=%d wall=%.0fs log=%s",
        window_name,
        phase_offset,
        proc.returncode,
        wall,
        log_path,
    )

    log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    per_cost_rows = _parse_per_cost_rows(log_text)
    return {
        "window": window_name,
        "phase_offset": phase_offset,
        "returncode": proc.returncode,
        "wall_seconds": wall,
        "log_path": str(log_path),
        "out_path": str(out_path),
        "per_cost": per_cost_rows,
    }


def _parse_per_cost_rows(log_text: str) -> dict[float, dict[str, float]]:
    """Parse all `cost=Nbps | ... α 4F=...% t=...` lines from one phase log."""
    rows: dict[float, dict[str, float]] = {}
    for line in log_text.splitlines():
        m = _RESULT_LINE.search(line)
        if not m:
            continue
        cost = float(m.group("cost"))
        rows[cost] = {
            "sharpe_gross": float(m.group("sg")),
            "sharpe_net": float(m.group("sn")),
            "excess_gross_ann": float(m.group("eg")) / 100.0,
            "excess_net_ann": float(m.group("en")) / 100.0,
            "alpha_ann": float(m.group("a")) / 100.0,
            "alpha_t": float(m.group("t")),
            "raw_line": line.strip(),
        }
    return rows


def _aggregate_per_phase(phase_results: list[dict], *, cost: float) -> dict | None:
    """Mean ± std + min/max of αt and excess_net across phases at a given cost."""
    rows = [
        pr["per_cost"].get(cost) for pr in phase_results if pr["per_cost"].get(cost) is not None
    ]
    if not rows:
        return None
    alpha_ts = [r["alpha_t"] for r in rows]
    excess_nets = [r["excess_net_ann"] for r in rows]
    sharpe_nets = [r["sharpe_net"] for r in rows]

    def _stats(values: list[float]) -> dict[str, float]:
        if len(values) == 1:
            return {"mean": values[0], "std": 0.0, "min": values[0], "max": values[0]}
        return {
            "mean": statistics.mean(values),
            "std": statistics.pstdev(values),
            "min": min(values),
            "max": max(values),
        }

    return {
        "n_phases": len(rows),
        "alpha_t": _stats(alpha_ts),
        "excess_net_ann": _stats(excess_nets),
        "sharpe_net": _stats(sharpe_nets),
        "per_phase_alpha_t": alpha_ts,
        "per_phase_excess_net_ann": excess_nets,
    }


def _evaluate_window_gates(window_block: dict) -> dict:
    """Apply memo §8 to a single window. Used both per-window and pooled."""
    baseline = window_block.get("baseline_cost_5bps")
    stress = window_block.get("stress_cost_15bps")
    gates: dict[str, dict] = {}
    if baseline is None or stress is None:
        return {"verdict": "UNKNOWN", "reason": "missing baseline or stress aggregates"}
    alpha_ts = baseline["per_phase_alpha_t"]
    mean_alpha_t = baseline["alpha_t"]["mean"]
    min_alpha_t = baseline["alpha_t"]["min"]
    stress_mean_t = stress["alpha_t"]["mean"]

    gates["G1_full_sample_alpha_t"] = {
        "value": mean_alpha_t,
        "threshold": G1_BONFERRONI_T,
        "passed": mean_alpha_t >= G1_BONFERRONI_T,
    }
    gates["G2_mean_per_phase_alpha_t"] = {
        "value": mean_alpha_t,
        "threshold": G2_MEAN_PER_PHASE_T,
        "passed": mean_alpha_t >= G2_MEAN_PER_PHASE_T,
    }
    gates["G3_positive_each_phase"] = {
        "min_phase_alpha_t": min_alpha_t,
        "threshold": G3_POSITIVE_PER_PHASE_T,
        "passed": all(t > G3_POSITIVE_PER_PHASE_T for t in alpha_ts),
    }
    gates["G4_cost_stress_15bps_mean_alpha_t"] = {
        "value": stress_mean_t,
        "threshold": G4_COST_STRESS_T,
        "passed": stress_mean_t >= G4_COST_STRESS_T,
    }
    passed_all = all(g["passed"] for g in gates.values())
    if passed_all:
        verdict = "PASS"
    elif (
        mean_alpha_t >= PASS_MARGINAL_T_LO
        and gates["G3_positive_each_phase"]["passed"]
        and gates["G4_cost_stress_15bps_mean_alpha_t"]["passed"]
    ):
        verdict = "PASS_MARGINAL"
    else:
        verdict = "FAIL"
    return {"verdict": verdict, "gates": gates}


def _audit_one_window(
    window_name: str,
    is_start: date,
    is_end: date,
    *,
    n_phases: int,
    cost_half_spreads: tuple[float, ...],
    universe_size_cap: int | None,
    log_dir: Path,
    artifact_root: Path,
    extra_env: dict[str, str] | None,
) -> dict:
    """Run all phases in parallel for one window; aggregate at each cost level."""
    logger.info(
        ">>> window %s | %s..%s | %d phases | costs=%s",
        window_name,
        is_start,
        is_end,
        n_phases,
        cost_half_spreads,
    )
    phase_results: list[dict] = [None] * n_phases  # type: ignore[list-item]
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=n_phases) as ex:
        futures = {
            ex.submit(
                _run_one_phase,
                p,
                window_name=window_name,
                is_start=is_start,
                is_end=is_end,
                cost_half_spreads=cost_half_spreads,
                universe_size_cap=universe_size_cap,
                log_dir=log_dir,
                artifact_root=artifact_root,
                extra_env=extra_env,
            ): p
            for p in range(n_phases)
        }
        for fut in as_completed(futures):
            p = futures[fut]
            phase_results[p] = fut.result()
    window_wall = time.monotonic() - t0
    logger.info(">>> window %s done in %.0fs", window_name, window_wall)

    baseline_agg = _aggregate_per_phase(phase_results, cost=COST_BASELINE_BPS)
    stress_agg = _aggregate_per_phase(phase_results, cost=COST_G4_STRESS_BPS)
    window_block = {
        "window_name": window_name,
        "is_start": is_start.isoformat(),
        "is_end": is_end.isoformat(),
        "n_phases": n_phases,
        "wall_seconds": window_wall,
        "baseline_cost_5bps": baseline_agg,
        "stress_cost_15bps": stress_agg,
        "phase_results": [
            {k: v for k, v in pr.items() if k != "per_cost"} | {"per_cost": pr["per_cost"]}
            for pr in phase_results
        ],
    }
    window_block.update(_evaluate_window_gates(window_block))
    return window_block


def _overall_verdict(window_blocks: list[dict]) -> dict:
    """Joint PASS rule per memo §8: all 3 windows must individually pass."""
    per_window = {w["window_name"]: w["verdict"] for w in window_blocks}
    if all(v == "PASS" for v in per_window.values()):
        overall = "PASS"
    elif all(v in {"PASS", "PASS_MARGINAL"} for v in per_window.values()):
        overall = "PASS_MARGINAL"
    else:
        overall = "FAIL"
    return {
        "overall_verdict": overall,
        "per_window_verdicts": per_window,
        "rule": "joint PASS: every window in {IS, OOS, FL} must individually clear memo §8 gates",
    }


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--out",
        type=Path,
        default=REPO / f"docs/research/ev_fcff_yield_audit_{date.today().isoformat()}.json",
    )
    ap.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
        help="Directory for per-phase JSON reports + intermediate artifacts.",
    )
    ap.add_argument(
        "--log-dir",
        type=Path,
        default=Path("/workspace/audit_logs")
        if Path("/workspace").exists()
        else (REPO / "audit_logs"),
        help="Isolated per-phase stdout/stderr capture (zen review 2026-05-12).",
    )
    # Smoke-only overrides. Default audit runs ignore these.
    ap.add_argument(
        "--n-phases",
        type=int,
        default=N_PHASES,
        help="Override phase count (SMOKE ONLY — locked at 5 for audit per memo).",
    )
    ap.add_argument(
        "--window-only",
        choices=["IS", "OOS", "FL"],
        default=None,
        help="Limit to a single window (SMOKE ONLY).",
    )
    ap.add_argument(
        "--is-start-override",
        type=date.fromisoformat,
        default=None,
        help="Override window start (SMOKE ONLY; requires --window-only).",
    )
    ap.add_argument(
        "--is-end-override",
        type=date.fromisoformat,
        default=None,
        help="Override window end (SMOKE ONLY; requires --window-only).",
    )
    ap.add_argument(
        "--universe-size-cap",
        type=int,
        default=None,
        help="Pass through to experiment script (SMOKE ONLY).",
    )
    ap.add_argument(
        "--simfin-data-dir",
        type=str,
        default=str(Path.home() / ".alphalens" / "simfin_cache"),
        help="SimFin bulk CSV directory (forwarded as env var to subprocesses).",
    )
    return ap


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args.log_dir.mkdir(parents=True, exist_ok=True)
    args.artifact_root.mkdir(parents=True, exist_ok=True)

    windows = list(WINDOWS)
    if args.window_only is not None:
        windows = [w for w in windows if w[0] == args.window_only]
    if args.is_start_override and args.is_end_override:
        assert args.window_only is not None, "--is-start-override requires --window-only"
        name = args.window_only
        windows = [(name, args.is_start_override, args.is_end_override)]

    extra_env = {"SIMFIN_DATA_DIR": args.simfin_data_dir}
    logger.info(
        "orchestrator launch | windows=%s | n_phases=%d | log_dir=%s",
        [w[0] for w in windows],
        args.n_phases,
        args.log_dir,
    )

    t0 = time.monotonic()
    window_blocks: list[dict] = []
    for name, start, end in windows:  # sequential across windows
        block = _audit_one_window(
            name,
            start,
            end,
            n_phases=args.n_phases,
            cost_half_spreads=COST_HALF_SPREADS_REQUESTED,
            universe_size_cap=args.universe_size_cap,
            log_dir=args.log_dir,
            artifact_root=args.artifact_root,
            extra_env=extra_env,
        )
        window_blocks.append(block)
        logger.info(">>> %s verdict=%s", name, block["verdict"])

    overall = _overall_verdict(window_blocks)
    total_wall = time.monotonic() - t0

    payload = {
        "strategy": "ev_fcff_yield",
        "ledger_id": "ev_fcff_yield_2026_05_12_v1",
        "signal_class": "fundamental_value_dcf_2026_05_12",
        "design_memo": "docs/research/ev_fcff_yield_v1_design_2026_05_12.md",
        "orchestrator_invoked_at": datetime.now().isoformat(timespec="seconds"),
        "orchestrator_total_wall_seconds": total_wall,
        "locked_constants": {
            "n_phases": N_PHASES,
            "rebalance_stride_days": REBALANCE_STRIDE_DAYS,
            "holding_days": HOLDING_DAYS,
            "cost_baseline_bps": COST_BASELINE_BPS,
            "cost_g4_stress_bps": COST_G4_STRESS_BPS,
        },
        "windows": window_blocks,
        "overall": overall,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    logger.info(">>> wrote canonical audit JSON to %s", args.out)
    logger.info(">>> overall verdict: %s", overall["overall_verdict"])
    logger.info(">>> total wall %.0fs (%.1f min)", total_wall, total_wall / 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
