"""5-phase parallel audit orchestrator for insider_form4_opportunistic.

Pre-reg ledger entry: ``insider_form4_opportunistic_2026_05_08_v2``.

Reads the same scorer + classifier modules locked in pre-reg (no drift —
only orchestrates 5 phase-offset subprocess invocations of
``scripts/experiment_insider_form4_opportunistic.py``).

Differences from the OSS ``run_audit`` driver:
- runs the 5 phases in parallel (Mac M2 has 10 cores, plenty of headroom)
- collects per-phase daily continuous-holding return parquets (via
  ``--dump-returns``) for downstream block-bootstrap
- applies the 5 R2000 primary gates G1-G5 from the pre-reg lock,
  including G5 stationary block-bootstrap CI on the pooled mean alpha_t.

Outputs (default suffix = ``phase_b_<DATE>``; override with ``--out-suffix``):
- ``docs/research/insider_form4_opportunistic_<suffix>.json`` — canonical
  verdict (signed by gates, per-phase metrics, bootstrap CI)
- ``docs/research/insider_form4_opportunistic_<suffix>.md`` — human report

Run (Phase B 2018-2023, default)::

    .venv/bin/python scripts/run_insider_form4_phase_b.py

Run (final lock 2024-2026)::

    .venv/bin/python scripts/run_insider_form4_phase_b.py \\
        --is-start 2024-01-01 --is-end 2026-03-31 \\
        --artifact-root ~/.alphalens/audit/insider_form4_opportunistic_final_lock \\
        --out-suffix final_lock_2024_2026
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

EXPERIMENT_SCRIPT = REPO / "scripts" / "experiment_insider_form4_opportunistic.py"
DEFAULT_ARTIFACT_ROOT = Path.home() / ".alphalens" / "audit" / "insider_form4_opportunistic_phase_b"
N_PHASES = 5
REBALANCE_STRIDE_DAYS = 21
HAC_MAXLAGS = 126
BLOCK_SIZE_TRADING_DAYS = 126
N_BOOTSTRAP = 1000
ALPHA_LEVEL = 0.05
RNG_SEED = 20260508  # reproducibility — Phase B execution date

DEFAULT_IS_START = date(2018, 1, 1)
DEFAULT_IS_END = date(2023, 12, 31)

# Pre-reg gate thresholds (R2000 primary)
G1_BONFERRONI_T = 3.1237  # naive |t| at program-level n=28 (v2 ledger lock)
G2_PER_PHASE_FLOOR = 1.5
G3_EXCESS_NET_FLOOR = 0.0
G4_DISPERSION_PP = 70.0
# G5: bounds_alpha_t_lower > 0

# Aggregation of per-phase headline metrics emitted by the experiment script.
_RESULT_LINE = re.compile(
    r"Sh gross=(?P<sg>[-\d.]+) net=(?P<sn>[-\d.]+) \| "
    r"excess gross=(?P<eg>[-\d.]+)% net=(?P<en>[-\d.]+)% \| "
    r"α 4F=(?P<a>[-\d.]+)% t=(?P<t>[-\d.]+)"
)
# Strip statsmodels HAC warning that says "alpha must be in [0..1]" (different alpha).


def _phase_command(
    phase_offset: int,
    returns_parquet: Path,
    report_md: Path,
    *,
    is_start: date,
    is_end: date,
) -> list[str]:
    return [
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


def _run_one_phase(
    phase_offset: int,
    *,
    artifact_root: Path,
    is_start: date,
    is_end: date,
) -> dict:
    """Spawn experiment script subprocess for given phase. Capture stderr.

    Returns a dict with: phase_offset, returncode, stderr_tail, parsed metrics
    (sharpe_gross, sharpe_net, excess_gross_ann, excess_net_ann, alpha_t),
    and returns_parquet path.
    """
    returns_parquet = artifact_root / f"phase_{phase_offset}_returns.parquet"
    report_md = artifact_root / f"phase_{phase_offset}_report.md"
    cmd = _phase_command(phase_offset, returns_parquet, report_md, is_start=is_start, is_end=is_end)
    logger.info("phase %d: launching subprocess", phase_offset)
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    wall_seconds = time.monotonic() - t0
    logger.info(
        "phase %d: subprocess exited rc=%d wall=%.1fs", phase_offset, proc.returncode, wall_seconds
    )
    if proc.returncode != 0:
        return {
            "phase_offset": phase_offset,
            "returncode": proc.returncode,
            "wall_seconds": wall_seconds,
            "stderr_tail": proc.stderr[-4000:],
            "parsed": None,
            "returns_parquet": str(returns_parquet),
        }
    parsed = None
    for line in proc.stderr.splitlines():
        m = _RESULT_LINE.search(line)
        if m:
            parsed = {
                "sharpe_gross": float(m.group("sg")),
                "sharpe_net": float(m.group("sn")),
                "excess_gross_ann": float(m.group("eg")) / 100.0,
                "excess_net_ann": float(m.group("en")) / 100.0,
                "alpha_t": float(m.group("t")),
                "alpha_pct": float(m.group("a")),
                "raw_line": line.strip(),
            }
            break
    return {
        "phase_offset": phase_offset,
        "returncode": proc.returncode,
        "wall_seconds": wall_seconds,
        "stderr_tail": proc.stderr[-4000:],
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

    Pre-reg ledger ``insider_form4_opportunistic_2026_05_08_v2`` mandates one
    block-index sequence per replicate, applied **synchronously** across all
    phases — independent per-phase resampling would destroy the cross-phase
    covariance structure (phases hold near-identical baskets on any given
    calendar day at daily cadence) and artificially narrow the pooled CI.

    Each replicate:
    1. Compute the master block index sequence on the common DatetimeIndex
       (intersection of all phases — handles rare 1-day length differences
       at phase-offset boundaries).
    2. Apply the SAME positional indices to each phase's series (each phase
       reindexed to the common index first).
    3. Per phase: run the Carhart 4F regression with HAC; record αt.
    4. Pooled αt for this replicate = mean across phases.

    Returns
    -------
    dict
        Keys: alpha_t_observed_mean, alpha_t_per_phase_observed,
        bounds_alpha_t_lower, bounds_alpha_t_upper, n_reps_used,
        block_size_trading_days, bootstrap_resampling.
    """
    if not per_phase_rets:
        raise ValueError("per_phase_rets must contain at least one series")
    rng = np.random.default_rng(seed)

    # Common index for synchronous resampling: intersection across phases.
    common_idx = per_phase_rets[0].index
    for s in per_phase_rets[1:]:
        common_idx = common_idx.intersection(s.index)
    if len(common_idx) == 0:
        raise ValueError("per_phase_rets share no common DatetimeIndex")

    aligned_rets = [s.reindex(common_idx) for s in per_phase_rets]
    aligned_factors = factors.reindex(common_idx)

    # Per-phase observed αt on full-sample
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
            logger.warning("observed regression failed phase: %s", exc)
            observed_alpha_ts.append(float("nan"))
    observed_mean_t = float(np.nanmean(observed_alpha_ts))

    n_obs = len(common_idx)
    boot_means: list[float] = []
    for rep in range(n_reps):
        # Master block sequence — generated once, applied to ALL phases.
        idx = stationary_bootstrap_indices(
            n_obs=n_obs,
            mean_block_length=float(block_size_trading_days),
            n_bootstrap=1,
            rng=rng,
        )[0]
        f_resample = aligned_factors.iloc[idx]
        # Replace the (unsorted) re-indexed dates with a synthetic clean index
        # so run_regression's inner concat([], join="inner") aligns perfectly.
        synthetic_idx = common_idx[: len(idx)]
        f_resample = f_resample.copy()
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
    """Apply pre-reg verdict_classification rules to the gate outcomes."""
    mean_t = gates["G1_pooled_alpha_t_mean"]
    g2 = gates["G2_every_phase_floor_passed"]
    g3 = gates["G3_excess_net_mean_passed"]
    g4 = gates["G4_dispersion_pp"] <= G4_DISPERSION_PP
    g5 = gates["G5_bounds_alpha_t_lower_passed"]
    dispersion_pp = gates["G4_dispersion_pp"]

    # Pre-reg verdict_classification (full PASS / PASS_MARGINAL / INCONCLUSIVE / FAIL)
    if mean_t >= G1_BONFERRONI_T and g2 and g3 and g4 and g5:
        return "PASS", (
            f"All R2000 primary gates passed: αt={mean_t:.2f} ≥ {G1_BONFERRONI_T}, "
            f"every phase ≥ {G2_PER_PHASE_FLOOR}, mean excess_net ≥ 0, "
            f"dispersion {dispersion_pp:.1f}pp ≤ {G4_DISPERSION_PP}, "
            f"bounds_alpha_t_lower > 0"
        )
    if 2.50 <= mean_t < G1_BONFERRONI_T and g2 and g3 and g4 and g5:
        return "PASS_MARGINAL", (f"αt={mean_t:.2f} in [2.50, {G1_BONFERRONI_T}); other gates pass")
    if dispersion_pp > G4_DISPERSION_PP and mean_t >= 2.50:
        return "INCONCLUSIVE", (
            f"mean αt={mean_t:.2f} ≥ 2.50 but dispersion {dispersion_pp:.1f}pp "
            f"> {G4_DISPERSION_PP} → cross-phase instability"
        )
    if mean_t < 2.50:
        return "FAIL", (
            f"mean αt={mean_t:.2f} < 2.50 — Cohen-Malloy mechanism does not "
            f"survive fresh OOS post-2012"
        )
    return "INCONCLUSIVE", "boundary case — see per-gate detail"


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--is-start",
        type=date.fromisoformat,
        default=DEFAULT_IS_START,
        help=f"Audit window start (YYYY-MM-DD). Default: {DEFAULT_IS_START.isoformat()}.",
    )
    ap.add_argument(
        "--is-end",
        type=date.fromisoformat,
        default=DEFAULT_IS_END,
        help=f"Audit window end (YYYY-MM-DD). Default: {DEFAULT_IS_END.isoformat()}.",
    )
    ap.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
        help=(
            "Directory for per-phase returns parquet + report md. "
            f"Default: {DEFAULT_ARTIFACT_ROOT}."
        ),
    )
    ap.add_argument(
        "--out-suffix",
        type=str,
        default=None,
        help=(
            "Suffix for canonical JSON+MD output filenames "
            "(insider_form4_opportunistic_<suffix>.{json,md}). "
            "Default: phase_b_<today>."
        ),
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
        "audit | window=%s..%s | n_phases=%d | stride=%d | parallel | artifact_root=%s",
        args.is_start,
        args.is_end,
        N_PHASES,
        REBALANCE_STRIDE_DAYS,
        args.artifact_root,
    )

    run_phase = partial(
        _run_one_phase,
        artifact_root=args.artifact_root,
        is_start=args.is_start,
        is_end=args.is_end,
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

    suffix = args.out_suffix or f"phase_b_{date.today()}"

    # Validate all phases produced parsed metrics
    failed = [r for r in phase_results if r["parsed"] is None]
    if failed:
        logger.error("FAIL: %d phases did not produce parseable metrics", len(failed))
        for r in failed:
            logger.error("phase %d stderr tail:\n%s", r["phase_offset"], r["stderr_tail"])
        # Still write what we have for postmortem
        out_json = REPO / f"docs/research/insider_form4_opportunistic_{suffix}_FAILED.json"
        out_json.write_text(
            json.dumps(
                {
                    "experiment": "insider_form4_opportunistic_2026_05_05",
                    "audit_status": "EXECUTION_FAIL",
                    "is_window": [args.is_start.isoformat(), args.is_end.isoformat()],
                    "failed_phases": [r["phase_offset"] for r in failed],
                    "phase_results": phase_results,
                },
                indent=2,
            )
        )
        return 3

    # Aggregate per-phase metrics
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

    # Load returns for bootstrap
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
        "Bootstrap: %d reps × %d phases × ~%d daily obs each, block_size=%d trading days, "
        "synchronous_across_phases",
        N_BOOTSTRAP,
        len(per_phase_rets),
        len(per_phase_rets[0]),
        BLOCK_SIZE_TRADING_DAYS,
    )
    bootstrap = _synchronous_bootstrap_pooled_alpha_t(per_phase_rets, factors)

    # Compose gate verdicts
    every_phase_floor_passed = all(t >= G2_PER_PHASE_FLOOR for t in alpha_ts)
    excess_net_floor_passed = excess_net_mean >= G3_EXCESS_NET_FLOOR
    dispersion_passed = dispersion_pp <= G4_DISPERSION_PP
    bounds_lower_passed = bootstrap["bounds_alpha_t_lower"] > 0

    gates = {
        "G1_pooled_alpha_t_mean": pooled_alpha_t_mean,
        "G1_threshold": G1_BONFERRONI_T,
        "G1_passed": pooled_alpha_t_mean >= G1_BONFERRONI_T,
        "G2_per_phase_alpha_ts": alpha_ts,
        "G2_floor": G2_PER_PHASE_FLOOR,
        "G2_every_phase_floor_passed": every_phase_floor_passed,
        "G3_excess_net_mean": excess_net_mean,
        "G3_floor": G3_EXCESS_NET_FLOOR,
        "G3_excess_net_mean_passed": excess_net_floor_passed,
        "G4_dispersion_pp": dispersion_pp,
        "G4_threshold_pp": G4_DISPERSION_PP,
        "G4_dispersion_passed": dispersion_passed,
        "G5_bounds_alpha_t_lower": bootstrap["bounds_alpha_t_lower"],
        "G5_bounds_alpha_t_upper": bootstrap["bounds_alpha_t_upper"],
        "G5_bounds_alpha_t_lower_passed": bounds_lower_passed,
    }

    verdict, reason = _classify_verdict(gates)

    out_json = REPO / f"docs/research/insider_form4_opportunistic_{suffix}.json"
    out_md = REPO / f"docs/research/insider_form4_opportunistic_{suffix}.md"

    payload = {
        "experiment": "insider_form4_opportunistic_2026_05_05",
        "audit_status": "evaluated",
        "out_suffix": suffix,
        "executed_at": date.today().isoformat(),
        "is_window": [args.is_start.isoformat(), args.is_end.isoformat()],
        "universe_mode": "R2000",
        "n_phases": N_PHASES,
        "rebalance_stride_days": REBALANCE_STRIDE_DAYS,
        "hac_maxlags": HAC_MAXLAGS,
        "verdict": verdict,
        "verdict_reason": reason,
        "gates": gates,
        "per_phase": per_phase,
        "bootstrap": bootstrap,
        "wall_total_seconds": round(time.monotonic() - t_total, 1),
        "wall_phases_max_seconds": max(r["wall_seconds"] for r in phase_results),
        "preregistration_ledger_id": "insider_form4_opportunistic_2026_05_08_v2",
        "preregistration_ledger_path": "docs/research/preregistration/ledger.json",
        "phase_a_canonical_json": "docs/research/insider_form4_opportunistic_phase_a_2026_05_08.json",
    }
    out_json.write_text(json.dumps(payload, indent=2))
    logger.info("Wrote %s", out_json)

    md = _format_markdown_report(payload)
    out_md.write_text(md)
    logger.info("Wrote %s", out_md)
    print(f"\n=== VERDICT: {verdict} ===\n{reason}\n")
    return 0


def _format_markdown_report(p: dict) -> str:
    g = p["gates"]
    title_label = p.get("out_suffix", "audit")
    lines = [
        f"# insider_form4_opportunistic — {title_label} verdict",
        "",
        f"**Verdict:** `{p['verdict']}` — {p['verdict_reason']}",
        "",
        f"**Window:** {p['is_window'][0]} → {p['is_window'][1]} (R2000 PIT, 5 phases, "
        f"stride={p['rebalance_stride_days']}d)",
        f"**Wall total:** {p['wall_total_seconds'] / 60:.1f} min "
        f"(max single phase {p['wall_phases_max_seconds'] / 60:.1f} min — parallel)",
        "",
        "## Gates (R2000 primary, pre-reg lock)",
        "",
        "| Gate | Metric | Threshold | Result |",
        "|---|---|---|---|",
        f"| G1 pooled αt | {g['G1_pooled_alpha_t_mean']:+.3f} | ≥ {g['G1_threshold']} (Bonferroni n=28) | "
        f"{'✅' if g['G1_passed'] else '❌'} |",
        f"| G2 per-phase αt floor | min={min(g['G2_per_phase_alpha_ts']):+.2f} max={max(g['G2_per_phase_alpha_ts']):+.2f} | every ≥ {g['G2_floor']} | "
        f"{'✅' if g['G2_every_phase_floor_passed'] else '❌'} |",
        f"| G3 excess_net mean | {g['G3_excess_net_mean'] * 100:+.2f}% | ≥ {g['G3_floor'] * 100:.1f}% | "
        f"{'✅' if g['G3_excess_net_mean_passed'] else '❌'} |",
        f"| G4 dispersion (excess_net) | {g['G4_dispersion_pp']:.1f}pp | ≤ {g['G4_threshold_pp']}pp | "
        f"{'✅' if g['G4_dispersion_passed'] else '❌'} |",
        f"| G5 bounds αt lower (block-boot) | {g['G5_bounds_alpha_t_lower']:+.3f} | > 0 | "
        f"{'✅' if g['G5_bounds_alpha_t_lower_passed'] else '❌'} |",
        "",
        "## Per-phase results",
        "",
        "| Phase | αt | Sh gross | Sh net | excess gross | excess net |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for ph in p["per_phase"]:
        lines.append(
            f"| {ph['phase_offset']} | {ph['alpha_t']:+.2f} | {ph['sharpe_gross']:+.2f} | "
            f"{ph['sharpe_net']:+.2f} | {ph['excess_gross_ann'] * 100:+.1f}% | "
            f"{ph['excess_net_ann'] * 100:+.1f}% |"
        )
    bs = p["bootstrap"]
    lines.extend(
        [
            "",
            "## Bootstrap detail (G5)",
            "",
            "- Method: stationary block bootstrap (Politis-Romano), single-strategy CI on mean αt",
            f"- Block size: {bs['block_size_trading_days']} trading days "
            f"(daily-cadence input, native unit per v2 ledger lock)",
            f"- Resampling: {bs['bootstrap_resampling']}",
            f"- Reps: {bs['n_reps_used']}",
            f"- Observed mean αt across phases: {bs['alpha_t_observed_mean']:+.3f}",
            f"- 2.5% / 97.5% bounds: {bs['bounds_alpha_t_lower']:+.3f} / {bs['bounds_alpha_t_upper']:+.3f}",
            f"- Per-phase observed αt (Carhart 4F, HAC={p['hac_maxlags']}): "
            + ", ".join(f"{t:+.2f}" for t in bs["alpha_t_per_phase_observed"]),
            "",
            "## References",
            "",
            f"- Phase A canonical: `{p['phase_a_canonical_json']}`",
            f"- Pre-reg ledger: `{p['preregistration_ledger_path']}` entry `{p['preregistration_ledger_id']}`",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
