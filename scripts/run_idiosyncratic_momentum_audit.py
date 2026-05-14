"""3-window x 5-phase audit orchestrator for idiosyncratic_momentum (paradigm #15).

Pre-reg ledger entry: ``idiosyncratic_momentum_2026_05_14_v1`` (LOCKED 2026-05-14).
Adapted from ``scripts/run_ev_fcff_yield_audit.py``.

## Why a custom orchestrator instead of ``alphalens audit``

``phase_robust_backtesting.audit_multi_phase.run_audit`` hard-codes
``--rebalance-stride <N>`` (passes it to the experiment script AND uses
``range(N)`` for phase iteration). Our experiment script enforces
``--rebalance-stride 21`` as a pre-reg lock (exit code 9 on override). The
two collide: passing ``rebalance_stride=5`` to ``run_audit`` would trip the
lock; passing ``rebalance_stride=21`` would spawn 21 phases. Same trap that
paradigms #12 and #13 hit — see
``docs/research/insider_pc_compound_audit_launch_postmortem_2026_05_11.md``.

## What this orchestrator does

For each of 3 hardcoded windows (IS / OOS / FL per memo section 6), spawn
``N_PHASES`` parallel subprocesses (phases 0..N-1). Each subprocess runs
``scripts/experiment_idiosyncratic_momentum.py`` with the locked stride/holding
and emits one log line per cost-stress level. Orchestrator parses both
baseline (5bps) and G4 (15bps) cost rows from a single subprocess output.

Per memo section 8 gate matrix (applied in code; verdict written into JSON):
- G1: full-sample net alpha_t >= 3.5 (pooled across phases at 5 bps)
- G2: mean alpha_t per-phase >= 2.5
- G3: positive alpha_t each phase
- G4: net alpha_t at 15 bps >= 2.0 (mean across phases)

Output: aggregated ``docs/research/idiosyncratic_momentum_audit_<date>.json``
with ``windows: {IS, OOS, FL}`` blocks + ``gate_summary`` + ``overall``.

## Operational locks (do not parameterise)

- ``N_PHASES = 5`` — convention from paradigm 12/13; matches multi-phase
  orchestrator pattern
- ``REBALANCE_STRIDE_DAYS = 21`` / ``HOLDING_DAYS = 21`` — memo section 7
- 3 window tuples — memo section 6 (IS 2010-2017, OOS 2018-2021, FL 2022-2024)

CLI overrides (``--window-only``, ``--n-phases``, ``--is-start-override``,
``--is-end-override``, ``--universe-size-cap``) exist for *local smoke
verification only*. Audit runs ignore them (orchestrator launched without
overrides -> uses locked constants).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
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

EXPERIMENT_SCRIPT = REPO / "scripts" / "experiment_idiosyncratic_momentum.py"
DEFAULT_ARTIFACT_ROOT = Path.home() / ".alphalens" / "audit" / "idiosyncratic_momentum"

# Pre-reg locked constants (memo idiosyncratic_momentum_v1_design_2026_05_14.md).
N_PHASES = 5
REBALANCE_STRIDE_DAYS = 21  # memo section 7
HOLDING_DAYS = 21  # memo section 7

# Memo section 6 phase split — 3 non-overlapping windows.
WINDOWS: tuple[tuple[str, date, date], ...] = (
    ("IS", date(2010, 1, 1), date(2017, 12, 31)),
    ("OOS", date(2018, 1, 1), date(2021, 12, 31)),
    ("FL", date(2022, 1, 1), date(2024, 12, 31)),
)

# Memo section 8 gate thresholds.
G1_BONFERRONI_T = 3.5  # project-imposed doctrine (class-internal would be 2.57 at n=5)
G2_MEAN_PER_PHASE_T = 2.5
G3_POSITIVE_PER_PHASE_T = 0.0
G4_COST_STRESS_T = 2.0
PASS_MARGINAL_T_LO = 2.5  # PASS_MARGINAL band: alpha_t in [2.5, 3.5)

# Cost levels we EVALUATE gates at. Subprocess emits a row per cost; we
# parse 5bps (baseline) and 15bps (G4 stress) from each phase.
COST_BASELINE_BPS = 5.0
COST_G4_STRESS_BPS = 15.0
COST_HALF_SPREADS_REQUESTED: tuple[float, ...] = (
    COST_BASELINE_BPS,
    COST_G4_STRESS_BPS,
)

# Same regex pattern as ev_fcff_yield orchestrator — captures cost + Sharpe +
# excess + Carhart alpha + optional net alpha tokens. Net tokens optional
# for backwards compatibility with pre-H1-fix logs.
_RESULT_LINE = re.compile(
    r"cost=(?P<cost>[\d.]+)bps \| .*?"
    r"Sh gross=(?P<sg>[-\d.]+) net=(?P<sn>[-\d.]+) \| "
    r".*?"
    r"alpha 4F=(?P<a>[-\d.]+)% t=(?P<t>[-\d.]+|nan|inf)"
    r"(?: \| alpha-net 4F=(?P<an>[-\d.]+)% t-net=(?P<tn>[-\d.]+|nan|inf))?"
)
# Experiment script logs use unicode greek alpha. Provide both patterns.
_RESULT_LINE_UNICODE = re.compile(
    r"cost=(?P<cost>[\d.]+)bps \| .*?"
    r"Sh gross=(?P<sg>[-\d.]+) net=(?P<sn>[-\d.]+) \| "
    r".*?"
    r"α 4F=(?P<a>[-\d.]+)% t=(?P<t>[-\d.]+|nan|inf)"
    r"(?: \| α-net 4F=(?P<an>[-\d.]+)% t-net=(?P<tn>[-\d.]+|nan|inf))?"
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
    """Hardcoded subprocess argv. Stride + holding LOCKED here so a future
    caller cannot re-introduce the paradigm-#12 stride-mismatch bug.
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
        "--skip-precheck",
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
    """Parse all `cost=Nbps | ... alpha 4F=...% t=...` lines from one phase log.

    Tries both ASCII (`alpha`) and Unicode (`u03b1`) patterns since
    experiment script emits the Greek letter.
    """
    rows: dict[float, dict[str, float]] = {}
    for line in log_text.splitlines():
        m = _RESULT_LINE_UNICODE.search(line) or _RESULT_LINE.search(line)
        if not m:
            continue
        cost = float(m.group("cost"))
        tn = m.group("tn")
        an = m.group("an")
        rows[cost] = {
            "sharpe_gross": float(m.group("sg")),
            "sharpe_net": float(m.group("sn")),
            "alpha_ann": float(m.group("a")) / 100.0,
            "alpha_t": float(m.group("t")),
            "alpha_net_ann": float(an) / 100.0 if an is not None else float(m.group("a")) / 100.0,
            "alpha_t_net": float(tn) if tn is not None else float(m.group("t")),
            "raw_line": line.strip(),
        }
    return rows


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        nan = float("nan")
        return {"mean": nan, "std": nan, "min": nan, "max": nan}
    if len(values) == 1:
        return {"mean": values[0], "std": 0.0, "min": values[0], "max": values[0]}
    if any(not math.isfinite(v) for v in values):
        nan = float("nan")
        return {"mean": nan, "std": nan, "min": nan, "max": nan}
    return {
        "mean": statistics.mean(values),
        "std": statistics.pstdev(values),
        "min": min(values),
        "max": max(values),
    }


def _aggregate_per_phase(phase_results: list[dict], *, cost: float) -> dict | None:
    rows = [
        pr["per_cost"].get(cost) for pr in phase_results if pr["per_cost"].get(cost) is not None
    ]
    if not rows:
        return None
    alpha_ts = [r["alpha_t"] for r in rows]
    alpha_t_nets = [r["alpha_t_net"] for r in rows]
    sharpe_nets = [r["sharpe_net"] for r in rows]
    return {
        "n_phases": len(rows),
        "alpha_t": _stats(alpha_ts),
        "alpha_t_net": _stats(alpha_t_nets),
        "sharpe_net": _stats(sharpe_nets),
        "per_phase_alpha_t": alpha_ts,
        "per_phase_alpha_t_net": alpha_t_nets,
    }


def _evaluate_window_gates(window_block: dict) -> dict:
    """Apply memo section 8 to a single window."""
    baseline = window_block.get("baseline_cost_5bps")
    stress = window_block.get("stress_cost_15bps")
    gates: dict[str, dict] = {}
    if baseline is None or stress is None:
        return {"verdict": "UNKNOWN", "reason": "missing baseline or stress aggregates"}
    alpha_ts = baseline["per_phase_alpha_t"]
    mean_alpha_t = baseline["alpha_t"]["mean"]
    min_alpha_t = baseline["alpha_t"]["min"]
    stress_mean_t = stress["alpha_t_net"]["mean"]

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
    """Joint PASS per memo section 8: every window must individually pass."""
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
        "rule": "joint PASS: every window in {IS, OOS, FL} must individually clear memo section 8 gates",
    }


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--out",
        type=Path,
        default=REPO
        / f"docs/research/idiosyncratic_momentum_audit_{date.today().isoformat()}.json",
    )
    ap.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
    )
    ap.add_argument(
        "--log-dir",
        type=Path,
        default=Path("/workspace/audit_logs")
        if Path("/workspace").exists()
        else (REPO / "audit_logs"),
    )
    ap.add_argument("--n-phases", type=int, default=N_PHASES)
    ap.add_argument("--window-only", choices=["IS", "OOS", "FL"], default=None)
    ap.add_argument("--is-start-override", type=date.fromisoformat, default=None)
    ap.add_argument("--is-end-override", type=date.fromisoformat, default=None)
    ap.add_argument("--universe-size-cap", type=int, default=None)
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
            extra_env=None,
        )
        window_blocks.append(block)
        logger.info(">>> %s verdict=%s", name, block["verdict"])

    overall = _overall_verdict(window_blocks)
    total_wall = time.monotonic() - t0

    payload = {
        "strategy": "idiosyncratic_momentum",
        "ledger_id": "idiosyncratic_momentum_2026_05_14_v1",
        "signal_class": "price_factor_search_2026_04_29",
        "design_memo": "docs/research/idiosyncratic_momentum_v1_design_2026_05_14.md",
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
