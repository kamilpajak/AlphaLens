"""Multi-phase audit runner — AlphaLens-side thin wrapper.

The aggregation logic lives in the external ``phase-robust-backtesting``
package (per ADR 0006). This wrapper only adds the AlphaLens-specific
syntactic sugar of resolving a short strategy name (e.g. ``tri_factor``)
to the corresponding ``scripts/experiment_*.py`` path before delegating
to :func:`phase_robust_backtesting.audit_multi_phase.run_audit`.

Usage::

  .venv/bin/python scripts/audit_multi_phase.py tri_factor \\
      --is-start 2019-01-08 --is-end 2022-12-31 \\
      --oos-start 2023-01-01 --oos-end 2023-06-30 \\
      --rebalance-stride 5

  .venv/bin/python scripts/audit_multi_phase.py insider_form4_opportunistic \\
      --is-start 2018-01-01 --is-end 2023-12-31

The delegation is **in-process** (``run_audit()`` is imported and called
directly, not invoked via subprocess) — preserves traceback fidelity and
Ctrl+C signal propagation. The OSS module already spawns one subprocess
per phase to invoke the experiment script; nesting our own subprocess
on top would double-fork and swallow tracebacks.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from phase_robust_backtesting.audit_multi_phase import run_audit  # noqa: E402

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
    "multi_source_two_stage": REPO / "scripts" / "experiment_multi_source_two_stage.py",
    "multi_source_global_lasso": REPO / "scripts" / "experiment_multi_source_global_lasso.py",
    "multi_source_global_lasso_20d": REPO
    / "scripts"
    / "experiment_multi_source_global_lasso_20d.py",
    "v7_options_implied": REPO / "scripts" / "experiment_v7_options_implied.py",
    "v8_literature_direct": REPO / "scripts" / "experiment_v8_literature_direct.py",
    "v9_sign_constrained": REPO / "scripts" / "experiment_v9_sign_constrained.py",
    "v9_cross_sectional_residual": REPO / "scripts" / "experiment_v9_cross_sectional_residual.py",
    "insider_form4_opportunistic": REPO / "scripts" / "experiment_insider_form4_opportunistic.py",
}


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
    return run_audit(
        _SCRIPTS[args.strategy],
        forwarded,
        rebalance_stride=args.rebalance_stride,
        out=args.out,
    )


if __name__ == "__main__":
    sys.exit(main())
