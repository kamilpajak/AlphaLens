"""5-phase parallel audit orchestrator for insider_pc_compound.

Pre-reg ledger entry: ``insider_pc_compound_2026_05_10`` (LOCKED 2026-05-10).
Adapted from ``scripts/run_insider_form4_phase_b.py`` to match the
compound-specific verdict matrix in memo §5.1 (different per-phase floor
thresholds for PASS vs PASS_MARGINAL than form4).

Why a custom orchestrator instead of the generic ``alphalens audit``
driver:

1. **Stride safety.** The generic driver
   (``phase_robust_backtesting.audit_multi_phase``) conflates "number of
   phases" with "rebalance day-step" — both are the same
   ``--rebalance-stride`` arg. Passing ``--rebalance-stride 5`` to the
   driver intending "5 phase offsets" silently runs the experiment with
   5-day rebalance cadence, deviating from memo §3.1's locked 21d
   monthly stride. Discovery cost: 2 hours of pod compute on 2026-05-11
   before the mismatch was caught. See postmortem
   ``docs/research/insider_pc_compound_audit_launch_postmortem_2026_05_11.md``.

2. **Block-bootstrap requirement.** Memo §5.4 requires
   "Romano-Wolf bootstrap with block_size = 126 trading days... 1000
   reps... Synchronous-across-phases resampling". The generic driver
   does not perform this — it only summarises per-phase stderr outputs.
   This orchestrator implements the synchronous block-bootstrap
   inline (identical algorithm to ``run_insider_form4_phase_b.py`` —
   methodology verified there).

Operationally identical to the form4 sibling except: the compound's
verdict matrix per memo §5.1 uses PER-PHASE floors {≥1.5 for PASS,
≥0 for PASS_MARGINAL} rather than form4's single ≥1.5 floor for both
labels. See ``_classify_verdict`` for the matrix.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from alphalens.attribution.factor_analysis import run_regression  # noqa: E402
from alphalens.backtest.romano_wolf import stationary_bootstrap_indices  # noqa: E402
from alphalens.data.factors import load_carhart_daily  # noqa: E402

logger = logging.getLogger(__name__)

EXPERIMENT_SCRIPT = REPO / "scripts" / "experiment_insider_pc_compound.py"
DEFAULT_ARTIFACT_ROOT = Path.home() / ".alphalens" / "audit" / "insider_pc_compound"

# Pre-reg locked constants (memo `insider_pc_compound_design_2026_05_10.md`).
N_PHASES = 5
REBALANCE_STRIDE_DAYS = 21  # memo §3.1 — DO NOT pass anything else
HAC_MAXLAGS = 126  # memo §4
BLOCK_SIZE_TRADING_DAYS = 126  # memo §5.4
N_BOOTSTRAP = 1000  # memo §5.4
ALPHA_LEVEL = 0.05
RNG_SEED = 20260511

# Memo §2: pre-reg windows.
DEFAULT_IS_START = date(2018, 1, 1)
DEFAULT_IS_END = date(2023, 12, 31)

# Pre-reg §5.1 verdict gate thresholds.
G1_BONFERRONI_T = 2.974
G2_PASS_PER_PHASE_FLOOR = 1.5  # required for PASS
G2_MARGINAL_PER_PHASE_FLOOR = 0.0  # required for PASS_MARGINAL
G3_EXCESS_NET_FLOOR = 0.0
G4_DISPERSION_PP = 70.0

_RESULT_LINE = re.compile(
    r"Sh gross=(?P<sg>[-\d.]+) net=(?P<sn>[-\d.]+) \| "
    r"excess gross=(?P<eg>[-\d.]+)% net=(?P<en>[-\d.]+)% \| "
    r"α 4F=(?P<a>[-\d.]+)% t=(?P<t>[-\d.]+)"
)


def _phase_command(
    phase_offset: int,
    returns_parquet: Path,
    report_md: Path,
    *,
    is_start: date,
    is_end: date,
    skip_precheck: bool,
) -> list[str]:
    """Hardcoded subprocess argv. ``--rebalance-stride 21`` is LOCKED
    here, not parameterised — preventing a future caller from re-introducing
    the 2026-05-11 stride-mismatch bug.
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
        "--phase-offset",
        str(phase_offset),
        "--out",
        str(report_md),
        "--dump-returns",
        str(returns_parquet),
    ]
    if skip_precheck:
        cmd.append("--skip-precheck")
    return cmd


def _run_one_phase(
    phase_offset: int,
    *,
    artifact_root: Path,
    is_start: date,
    is_end: date,
    skip_precheck: bool,
) -> dict:
    """Spawn experiment subprocess for ``phase_offset``; parse stderr."""
    returns_parquet = artifact_root / f"phase_{phase_offset}_returns.parquet"
    report_md = artifact_root / f"phase_{phase_offset}_report.md"
    cmd = _phase_command(
        phase_offset,
        returns_parquet,
        report_md,
        is_start=is_start,
        is_end=is_end,
        skip_precheck=skip_precheck,
    )
    logger.info("phase %d: launching subprocess", phase_offset)
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    wall_seconds = time.monotonic() - t0
    logger.info(
        "phase %d: subprocess exited rc=%d wall=%.0fs",
        phase_offset,
        proc.returncode,
        wall_seconds,
    )

    if proc.returncode != 0:
        return {
            "phase_offset": phase_offset,
            "returncode": proc.returncode,
            "wall_seconds": wall_seconds,
            "stderr_tail": (proc.stderr or "")[-4000:],
            "parsed": None,
            "returns_parquet": str(returns_parquet),
        }

    parsed = None
    for line in (proc.stderr or "").splitlines():
        m = _RESULT_LINE.search(line)
        if m:
            parsed = {
                "sharpe_gross": float(m.group("sg")),
                "sharpe_net": float(m.group("sn")),
                "excess_gross_ann": float(m.group("eg")) / 100.0,
                "excess_net_ann": float(m.group("en")) / 100.0,
                "alpha_t": float(m.group("t")),
                "alpha_pct": float(m.group("a")) / 100.0,
                "raw_line": line.strip(),
            }
            break
    return {
        "phase_offset": phase_offset,
        "returncode": proc.returncode,
        "wall_seconds": wall_seconds,
        "stderr_tail": (proc.stderr or "")[-4000:],
        "parsed": parsed,
        "returns_parquet": str(returns_parquet),
    }


def _synchronous_bootstrap_pooled_alpha_t(
    per_phase_rets: list[pd.Series],
    factors: pd.DataFrame,
    *,
    block_size_trading_days: int = BLOCK_SIZE_TRADING_DAYS,
    n_reps: int = N_BOOTSTRAP,
    seed: int = RNG_SEED,
    alpha_level: float = ALPHA_LEVEL,
    hac_maxlags: int = HAC_MAXLAGS,
) -> dict:
    """Synchronous-across-phases stationary block bootstrap of pooled mean αt.

    Mirrors ``run_insider_form4_phase_b.py::_synchronous_bootstrap_pooled_alpha_t``
    byte-equivalent (methodology verified there).
    """
    if not per_phase_rets:
        raise ValueError("per_phase_rets must contain at least one series")
    rng = np.random.default_rng(seed)

    common_idx = per_phase_rets[0].index
    for s in per_phase_rets[1:]:
        common_idx = common_idx.intersection(s.index)
    if len(common_idx) == 0:
        raise ValueError("per_phase_rets share no common DatetimeIndex")

    aligned_rets = [s.reindex(common_idx) for s in per_phase_rets]
    aligned_factors = factors.reindex(common_idx)

    observed_alpha_ts: list[float] = []
    for s in aligned_rets:
        try:
            res = run_regression(
                s,
                aligned_factors[["Mkt-RF", "SMB", "HML", "Mom", "RF"]],
                ["Mkt-RF", "SMB", "HML", "Mom"],
                hac_maxlags=hac_maxlags,
                periods_per_year=252,
            )
            observed_alpha_ts.append(float(res.alpha_tstat))
        except Exception as exc:
            logger.warning("observed regression failed: %s", exc)
            observed_alpha_ts.append(float("nan"))
    observed_mean_t = float(np.nanmean(observed_alpha_ts))

    n_obs = len(common_idx)
    boot_means: list[float] = []
    for rep in range(n_reps):
        idx = stationary_bootstrap_indices(
            n_obs=n_obs,
            mean_block_length=float(block_size_trading_days),
            n_bootstrap=1,
            rng=rng,
        )[0]
        f_resample = aligned_factors.iloc[idx].copy()
        synthetic_idx = common_idx[: len(idx)]
        f_resample.index = synthetic_idx

        per_phase_t: list[float] = []
        for s in aligned_rets:
            r_resample = s.iloc[idx].copy()
            r_resample.index = synthetic_idx
            try:
                res = run_regression(
                    r_resample,
                    f_resample[["Mkt-RF", "SMB", "HML", "Mom", "RF"]],
                    ["Mkt-RF", "SMB", "HML", "Mom"],
                    hac_maxlags=hac_maxlags,
                    periods_per_year=252,
                )
                per_phase_t.append(float(res.alpha_tstat))
            except Exception:
                per_phase_t.append(float("nan"))
        if per_phase_t and not all(math.isnan(t) for t in per_phase_t):
            boot_means.append(float(np.nanmean(per_phase_t)))
        if (rep + 1) % 100 == 0:
            logger.info("bootstrap rep %d/%d", rep + 1, n_reps)

    arr = np.array(boot_means)
    return {
        "alpha_t_observed_mean": observed_mean_t,
        "alpha_t_per_phase_observed": observed_alpha_ts,
        "bounds_alpha_t_lower": float(np.quantile(arr, alpha_level / 2)),
        "bounds_alpha_t_upper": float(np.quantile(arr, 1 - alpha_level / 2)),
        "n_reps_used": len(arr),
        "block_size_trading_days": block_size_trading_days,
        "bootstrap_resampling": "synchronous_across_phases",
    }


def _classify_verdict(gates: dict) -> tuple[str, str]:
    """Apply memo §5.1's verdict matrix to per-phase gate outcomes.

    Compound-specific (different from form4): PASS requires every phase
    αt ≥ 1.5; PASS_MARGINAL requires every phase αt ≥ 0 (looser).

    Returns
    -------
    (label, rationale) where label is one of PASS / PASS_MARGINAL /
    INCONCLUSIVE / FAIL.
    """
    mean_t = gates["G1_pooled_alpha_t_mean"]
    g2_pass_15 = gates["G2_every_phase_pass_15"]
    g2_ge_0 = gates["G2_every_phase_ge_0"]
    excess_net_passed = gates["G3_excess_net_mean_passed"]
    dispersion_pp = gates["G4_dispersion_pp"]
    dispersion_passed = dispersion_pp <= G4_DISPERSION_PP

    # FAIL rules first (memo §5.1 final row):
    if not excess_net_passed:
        return (
            "FAIL",
            f"mean excess_net_ann < 0 (gate G3 failed); mean αt={mean_t:.2f}",
        )
    if mean_t < 2.50:
        return (
            "FAIL",
            f"mean αt={mean_t:.2f} < 2.50 — compound mechanism does not survive "
            "fresh OOS at the verdict floor",
        )

    # PASS rule (full Bonferroni):
    if mean_t >= G1_BONFERRONI_T and g2_pass_15 and dispersion_passed:
        return (
            "PASS",
            f"All primary gates clear: mean αt={mean_t:.2f} ≥ {G1_BONFERRONI_T}, "
            f"every phase αt ≥ {G2_PASS_PER_PHASE_FLOOR}, "
            f"mean excess_net ≥ 0, dispersion {dispersion_pp:.1f}pp "
            f"≤ {G4_DISPERSION_PP}",
        )

    # INCONCLUSIVE — dispersion failure even with valid mean:
    if not dispersion_passed:
        return (
            "INCONCLUSIVE",
            f"mean αt={mean_t:.2f} ≥ 2.50 but dispersion "
            f"{dispersion_pp:.1f}pp > {G4_DISPERSION_PP} → cross-phase "
            "instability",
        )

    # In-band PASS_MARGINAL vs INCONCLUSIVE based on per-phase floor:
    if 2.50 <= mean_t < G1_BONFERRONI_T:
        if g2_ge_0:
            return (
                "PASS_MARGINAL",
                f"mean αt={mean_t:.2f} ∈ [2.50, {G1_BONFERRONI_T}); every "
                f"phase ≥ 0; dispersion passed",
            )
        return (
            "INCONCLUSIVE",
            f"mean αt={mean_t:.2f} ∈ [2.50, {G1_BONFERRONI_T}) but ≥1 phase "
            "αt < 0 — phase-robustness compromised",
        )

    # Shouldn't reach here given the matrix is exhaustive over the
    # mean/dispersion/excess_net axes; defensive fallthrough.
    return ("INCONCLUSIVE", "boundary case — see per-gate detail")


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--is-start",
        type=date.fromisoformat,
        default=DEFAULT_IS_START,
        help=f"Audit window start (default: {DEFAULT_IS_START.isoformat()}).",
    )
    ap.add_argument(
        "--is-end",
        type=date.fromisoformat,
        default=DEFAULT_IS_END,
        help=f"Audit window end (default: {DEFAULT_IS_END.isoformat()}).",
    )
    ap.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
        help="Directory for per-phase returns parquet + report md.",
    )
    ap.add_argument(
        "--out-suffix",
        type=str,
        default=None,
        help="Suffix for canonical JSON output filename. Default: <today>.",
    )
    ap.add_argument(
        "--skip-precheck",
        action="store_true",
        help="Skip IS 2014-2017 precheck guard (use on RunPod where pre-2018 iVol absent).",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args.artifact_root.mkdir(parents=True, exist_ok=True)

    logger.info(
        "audit | window=%s..%s | n_phases=%d | stride=%dd | parallel | "
        "artifact_root=%s | skip_precheck=%s",
        args.is_start,
        args.is_end,
        N_PHASES,
        REBALANCE_STRIDE_DAYS,
        args.artifact_root,
        args.skip_precheck,
    )

    run_phase = partial(
        _run_one_phase,
        artifact_root=args.artifact_root,
        is_start=args.is_start,
        is_end=args.is_end,
        skip_precheck=args.skip_precheck,
    )
    t_total = time.monotonic()
    phase_results: list[dict] = []
    with ThreadPoolExecutor(max_workers=N_PHASES) as pool:
        futures = {pool.submit(run_phase, p): p for p in range(N_PHASES)}
        for fut in as_completed(futures):
            res = fut.result()
            phase_results.append(res)
            logger.info(
                "phase %d done | rc=%d | wall=%.0fs | parsed=%s",
                res["phase_offset"],
                res["returncode"],
                res["wall_seconds"],
                "yes" if res["parsed"] else "NO",
            )

    phase_results.sort(key=lambda r: r["phase_offset"])
    suffix = args.out_suffix or f"audit_{date.today()}"

    failed = [r for r in phase_results if r["parsed"] is None]
    if failed:
        logger.error("FAIL: %d phases did not produce parseable metrics", len(failed))
        for r in failed:
            logger.error("phase %d stderr tail:\n%s", r["phase_offset"], r["stderr_tail"])
        out_json = REPO / f"docs/research/insider_pc_compound_{suffix}_FAILED.json"
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(
            json.dumps(
                {
                    "experiment": "insider_pc_compound_2026_05_10",
                    "audit_status": "EXECUTION_FAIL",
                    "is_window": [args.is_start.isoformat(), args.is_end.isoformat()],
                    "failed_phases": [r["phase_offset"] for r in failed],
                    "phase_results": phase_results,
                },
                indent=2,
                default=str,
            )
        )
        return 3

    per_phase = [
        {
            "phase_offset": r["phase_offset"],
            **r["parsed"],
            "wall_seconds": r["wall_seconds"],
        }
        for r in phase_results
    ]
    alpha_ts = [p["alpha_t"] for p in per_phase]
    excess_nets = [p["excess_net_ann"] for p in per_phase]

    pooled_alpha_t_mean = float(np.mean(alpha_ts))
    excess_net_mean = float(np.mean(excess_nets))
    dispersion_pp = float((max(excess_nets) - min(excess_nets)) * 100)

    per_phase_rets: list[pd.Series] = []
    for r in phase_results:
        df = pd.read_parquet(r["returns_parquet"])
        s = df.iloc[:, 0]
        s.index = pd.DatetimeIndex(df.index)
        per_phase_rets.append(s)

    common_start = min(s.index.min() for s in per_phase_rets)
    common_end = max(s.index.max() for s in per_phase_rets)
    factors = load_carhart_daily(start=common_start.date(), end=common_end.date())

    logger.info(
        "Bootstrap: %d reps × %d phases × ~%d daily obs each, block_size=%d "
        "trading days, synchronous_across_phases",
        N_BOOTSTRAP,
        len(per_phase_rets),
        len(per_phase_rets[0]),
        BLOCK_SIZE_TRADING_DAYS,
    )
    bootstrap = _synchronous_bootstrap_pooled_alpha_t(per_phase_rets, factors)

    gates = {
        "G1_pooled_alpha_t_mean": pooled_alpha_t_mean,
        "G1_bonferroni_threshold": G1_BONFERRONI_T,
        "G2_every_phase_pass_15": all(t >= G2_PASS_PER_PHASE_FLOOR for t in alpha_ts),
        "G2_every_phase_ge_0": all(t >= G2_MARGINAL_PER_PHASE_FLOOR for t in alpha_ts),
        "G3_excess_net_mean_passed": excess_net_mean >= G3_EXCESS_NET_FLOOR,
        "G3_excess_net_mean": excess_net_mean,
        "G4_dispersion_pp": dispersion_pp,
        "G4_dispersion_pp_floor": G4_DISPERSION_PP,
        "G5_bounds_alpha_t_lower": bootstrap["bounds_alpha_t_lower"],
        "G5_bounds_alpha_t_upper": bootstrap["bounds_alpha_t_upper"],
    }

    verdict, reason = _classify_verdict(gates)
    total_wall = time.monotonic() - t_total
    logger.info("audit complete | total_wall=%.0fs | verdict=%s", total_wall, verdict)

    out_json = REPO / f"docs/research/insider_pc_compound_{suffix}.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "experiment": "insider_pc_compound_2026_05_10",
        "audit_status": "EXECUTION_OK",
        "is_window": [args.is_start.isoformat(), args.is_end.isoformat()],
        "n_phases": N_PHASES,
        "rebalance_stride_days": REBALANCE_STRIDE_DAYS,
        "verdict": verdict,
        "verdict_reason": reason,
        "gates": gates,
        "per_phase": per_phase,
        "bootstrap": bootstrap,
        "total_wall_seconds": total_wall,
    }
    out_json.write_text(json.dumps(payload, indent=2, default=str))
    logger.info("Wrote %s", out_json)
    print(f"\n=== VERDICT: {verdict} ===\n{reason}\n", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
