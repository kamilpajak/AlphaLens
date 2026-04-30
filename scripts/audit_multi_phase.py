"""Multi-phase audit runner — eliminate phase-aliasing in stability checks.

Wraps `experiment_tri_factor_edgar.py` or `experiment_momentum_lowvol_combo.py`
to loop over `--phase-offset 0..stride-1`, parse the per-phase headline stats
out of the script's stdout, and emit an aggregated mean ± std ± verdict
report.

Usage::

  .venv/bin/python scripts/audit_multi_phase.py tri_factor \\
      --is-start 2019-01-08 --is-end 2022-12-31 \\
      --oos-start 2023-01-01 --oos-end 2023-06-30 \\
      --adv-thresholds 5000000 \\
      --roe-weights 1.0 \\
      --cost-half-spreads 5 \\
      --rebalance-stride 5

Or for mom+lowvol::

  .venv/bin/python scripts/audit_multi_phase.py momentum_lowvol \\
      --is-start 2019-01-02 --is-end 2022-12-31 \\
      --oos-start 2023-01-01 --oos-end 2023-06-30 \\
      --vol-weights 1.0 --adv-thresholds 5000000 --cost-half-spreads 5

Closes the gap from `docs/research/methodology_audit_2026_04_29.md`: any
single-phase Sharpe is unreliable; the aggregator provides phase-dispersion
estimates plus a robust verdict.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alphalens.backtest.multi_phase import robust_verdict, summarise_phase_results  # noqa: E402

_SCRIPTS = {
    "tri_factor": REPO / "scripts" / "experiment_tri_factor_edgar.py",
    "momentum_lowvol": REPO / "scripts" / "experiment_momentum_lowvol_combo.py",
    "constrained_momentum": REPO / "scripts" / "experiment_constrained_momentum.py",
    "constrained_contrarian": REPO / "scripts" / "experiment_constrained_contrarian.py",
    "quality_momentum": REPO / "scripts" / "experiment_quality_momentum_combo.py",
    "longshort_mom_lowvol": REPO / "scripts" / "experiment_longshort_mom_lowvol.py",
    "regime_overlay": REPO / "scripts" / "experiment_regime_overlay.py",
    "layer2d_prior_returns": REPO / "scripts" / "experiment_layer2d_prior_returns.py",
    "layer2d_random_null": REPO / "scripts" / "experiment_layer2d_random_null.py",
    "layer2d_str_and_contrarian": REPO / "scripts" / "experiment_layer2d_str_and_contrarian.py",
    "layer2d_variants": REPO / "scripts" / "experiment_layer2d_variants.py",
    "vol_target_overlay": REPO / "scripts" / "experiment_vol_target_overlay.py",
}

# Parses lines like:
#   "IS 2019-2022 | rw=1.0 vw=1.0 ADV≥$5M cost=5bps | n=201 ... Sh gross=0.83 net=0.65 |
#    excess gross=42.1% net=39.6% | α 4F=63.1% t=2.24 R²=0.049"
# and:
#   "IS 2015-2022 | vw=1.0 ADV≥$5M cost=5bps | ... Sh gross=0.42 net=0.21 |
#    excess gross=18.7% net=16.1% | α 4F=27.8% t=1.37"
_RESULT_LINE = re.compile(
    r"Sh gross=(?P<sg>[-\d.]+) net=(?P<sn>[-\d.]+) \| "
    r"excess gross=(?P<eg>[-\d.]+)% net=(?P<en>[-\d.]+)% \| "
    r"α 4F=(?P<a>[-\d.]+)% t=(?P<t>[-\d.]+)"
)

# Log lines come prefixed with `<timestamp> INFO <name>: <content>`. Strip the
# prefix when grouping per-phase results — otherwise every subprocess
# invocation produces a unique config key and the aggregator never aggregates.
_LOG_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ \w+ [\w._]+: ")


def _parse_results(stderr_text: str, phase_offset: int) -> list[dict[str, float]]:
    """Run `_RESULT_LINE` over each stderr line and emit one row per match.

    Pure function — split out from `_run_one_phase` for unit testing.
    """
    rows: list[dict[str, float]] = []
    for line in stderr_text.splitlines():
        m = _RESULT_LINE.search(line)
        if not m:
            continue
        rows.append(
            {
                "sharpe_gross": float(m.group("sg")),
                "sharpe_net": float(m.group("sn")),
                "excess_gross_ann": float(m.group("eg")) / 100.0,
                "excess_net_ann": float(m.group("en")) / 100.0,
                "alpha_t": float(m.group("t")),
                "phase_offset": phase_offset,
                "raw_line": line.strip(),
            }
        )
    return rows


def _config_key_from_line(raw_line: str) -> str:
    """Strip the timestamp/log prefix and trailing per-phase stats; what's
    left is the parameter combo (period + ROE/vol weight + ADV + cost) that
    is shared across all phases of the same config — the right grouping key.
    """
    stripped = _LOG_PREFIX.sub("", raw_line)
    return stripped.split(" | n=")[0]


def _group_by_config(rows_per_phase: list[list[dict]]) -> dict[str, list[dict]]:
    """Flatten per-phase row batches and bucket by config key."""
    by_config: dict[str, list[dict]] = {}
    for phase_rows in rows_per_phase:
        for r in phase_rows:
            by_config.setdefault(_config_key_from_line(r["raw_line"]), []).append(r)
    return by_config


def _run_one_phase(
    script: Path,
    forwarded_args: list[str],
    phase_offset: int,
    stride: int,
) -> list[dict[str, float]]:
    """Invoke the experiment script with --phase-offset and parse result rows.

    Passes a per-phase --out under /tmp so subprocess invocations cannot
    clobber the canonical research docs (the experiment scripts' default
    --out paths point to docs/research/, which would overwrite historical
    sweeps with single-phase audit data).
    """
    out_path = Path(f"/tmp/audit_multi_phase_{script.stem}_p{phase_offset}.md")
    cmd = [
        ".venv/bin/python",
        str(script),
        "--rebalance-stride",
        str(stride),
        "--phase-offset",
        str(phase_offset),
        "--out",
        str(out_path),
        *forwarded_args,
    ]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"phase {phase_offset} run failed (exit {proc.returncode}):\n"
            f"stderr tail:\n{proc.stderr[-2000:]}"
        )
    return _parse_results(proc.stderr, phase_offset)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("strategy", choices=sorted(_SCRIPTS))
    ap.add_argument(
        "--rebalance-stride",
        type=int,
        default=5,
        help="Stride to sweep across (default 5 = weekly cadence).",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("docs/research/multi_phase_audit.json"),
    )
    args, forwarded = ap.parse_known_args()

    script = _SCRIPTS[args.strategy]

    print(f"\n>>> Multi-phase audit: {args.strategy}", flush=True)
    print(f"    script: {script}", flush=True)
    print(f"    stride: {args.rebalance_stride}", flush=True)
    print(f"    phases: 0..{args.rebalance_stride - 1}", flush=True)

    all_rows: list[list[dict]] = []
    for phase in range(args.rebalance_stride):
        print(f"\n>>> phase {phase}/{args.rebalance_stride - 1}", flush=True)
        rows = _run_one_phase(script, forwarded, phase, args.rebalance_stride)
        if not rows:
            print(f"    WARNING: no result rows parsed for phase {phase}", flush=True)
        for r in rows:
            print(f"    {r['raw_line']}", flush=True)
        all_rows.append(rows)

    by_config = _group_by_config(all_rows)

    output: dict = {
        "strategy": args.strategy,
        "rebalance_stride": args.rebalance_stride,
        "configs": [],
    }
    for config_key, phase_rows in by_config.items():
        summary = summarise_phase_results(phase_rows)
        verdict = robust_verdict(phase_rows)
        output["configs"].append(
            {
                "config": config_key,
                "n_phases": len(phase_rows),
                "summary": summary,
                "verdict": verdict,
                "per_phase": [
                    {
                        "phase_offset": r["phase_offset"],
                        "sharpe_gross": r["sharpe_gross"],
                        "sharpe_net": r["sharpe_net"],
                        "excess_gross_ann": r["excess_gross_ann"],
                        "excess_net_ann": r["excess_net_ann"],
                        "alpha_t": r["alpha_t"],
                    }
                    for r in phase_rows
                ],
            }
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2))
    print(f"\n>>> wrote {args.out}", flush=True)

    print("\n>>> Verdict summary")
    for entry in output["configs"]:
        cfg = entry["config"]
        v = entry["verdict"]
        s = entry["summary"]
        if "alpha_t" in s and "excess_net_ann" in s:
            print(
                f"  {cfg}\n"
                f"    verdict: {v} | "
                f"α t mean={s['alpha_t']['mean']:+.2f} (±{s['alpha_t']['std']:.2f}, "
                f"min={s['alpha_t']['min']:+.2f}, max={s['alpha_t']['max']:+.2f}) | "
                f"excess net mean={s['excess_net_ann']['mean'] * 100:+.1f}% "
                f"(±{s['excess_net_ann']['std'] * 100:.1f}pp)"
            )
        else:
            print(f"  {cfg}\n    verdict: {v} | (incomplete summary)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
