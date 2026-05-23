"""`alphalens audit` — multi-phase audit driver.

First-class CLI entry point for the OSS phase-robust-backtesting
``run_audit`` driver. Resolves a short strategy name (e.g. ``tri_factor``)
to the corresponding ``scripts/experiment_*.py`` path before delegating
to :func:`phase_robust_backtesting.audit_multi_phase.run_audit`.

The delegation is **in-process** (``run_audit`` is imported and called
directly, not invoked via subprocess) — preserves traceback fidelity and
Ctrl+C signal propagation. The OSS module already spawns one subprocess
per phase to invoke the experiment script; nesting another subprocess
on top would double-fork and swallow tracebacks.

Usage::

    alphalens audit tri_factor \\
        --is-start 2019-01-08 --is-end 2022-12-31 \\
        --oos-start 2023-01-01 --oos-end 2023-06-30 \\
        --rebalance-stride 5

    alphalens audit insider_form4_opportunistic \\
        --is-start 2018-01-01 --is-end 2023-12-31

Extra args after ``--rebalance-stride`` / ``--out`` are forwarded to the
experiment script as positional arguments via ``ctx.args``.
"""

from __future__ import annotations

from pathlib import Path

import typer

# Experiment scripts live in the alphalens-research workspace member (they
# import from both alphalens_research.* and alphalens_pipeline.*; keeping
# them with the research lab matches their development cadence). This CLI
# command resolves their paths via the workspace root.
#   apps/alphalens-pipeline/alphalens_cli/commands/audit.py  (this file)
#   apps/alphalens-research/scripts/                          (target dir)
_WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
_RESEARCH_SCRIPTS = _WORKSPACE_ROOT / "apps" / "alphalens-research" / "scripts"

# Mapping of short strategy names to their experiment script paths.
# Single source of truth — moved here from scripts/audit_multi_phase.py
# so that adding a new strategy means editing exactly one file.
_SCRIPTS: dict[str, Path] = {
    "tri_factor": _RESEARCH_SCRIPTS / "experiment_tri_factor_edgar.py",
    "momentum_lowvol": _RESEARCH_SCRIPTS / "experiment_momentum_lowvol_combo.py",
    "constrained_momentum": _RESEARCH_SCRIPTS / "experiment_constrained_momentum.py",
    "constrained_contrarian": _RESEARCH_SCRIPTS / "experiment_constrained_contrarian.py",
    "quality_momentum": _RESEARCH_SCRIPTS / "experiment_quality_momentum_combo.py",
    "longshort_mom_lowvol": _RESEARCH_SCRIPTS / "experiment_longshort_mom_lowvol.py",
    "regime_overlay": _RESEARCH_SCRIPTS / "experiment_regime_overlay.py",
    "vol_target_overlay": _RESEARCH_SCRIPTS / "experiment_vol_target_overlay.py",
    "v7_options_implied": _RESEARCH_SCRIPTS / "experiment_v7_options_implied.py",
    "v8_literature_direct": _RESEARCH_SCRIPTS / "experiment_v8_literature_direct.py",
    "v9_sign_constrained": _RESEARCH_SCRIPTS / "experiment_v9_sign_constrained.py",
    "v9_cross_sectional_residual": _RESEARCH_SCRIPTS / "experiment_v9_cross_sectional_residual.py",
    "insider_form4_opportunistic": _RESEARCH_SCRIPTS / "experiment_insider_form4_opportunistic.py",
    "insider_pc_compound": _RESEARCH_SCRIPTS / "experiment_insider_pc_compound.py",
    "ev_fcff_yield": _RESEARCH_SCRIPTS / "experiment_ev_fcff_yield.py",
    "pead_pss_v2_2026_05_13": _RESEARCH_SCRIPTS / "experiment_pead_pss_v2.py",
    "idiosyncratic_momentum_2026_05_14_v1": _RESEARCH_SCRIPTS
    / "experiment_idiosyncratic_momentum.py",
}


def audit_command(
    ctx: typer.Context,
    strategy: str = typer.Argument(
        ...,
        help="Strategy name; see scripts/experiment_*.py for the full list.",
    ),
    rebalance_stride: int = typer.Option(
        5,
        "--rebalance-stride",
        help="Stride to sweep across (default 5 = weekly cadence).",
    ),
    out: Path = typer.Option(
        _WORKSPACE_ROOT / "docs/research/multi_phase_audit.json",
        "--out",
        help="Output JSON path (default: <workspace_root>/docs/research/multi_phase_audit.json).",
    ),
) -> None:
    """Run the multi-phase audit driver for a registered strategy."""
    # Lazy import — running `alphalens_research --help` should not import the OSS
    # methodology bundle (statsmodels, scipy) which adds startup overhead.
    from phase_robust_backtesting.audit_multi_phase import run_audit

    if strategy not in _SCRIPTS:
        typer.echo(
            f"Unknown strategy {strategy!r}. Choices: {sorted(_SCRIPTS)}",
            err=True,
        )
        raise typer.Exit(code=2)

    raise typer.Exit(
        run_audit(
            _SCRIPTS[strategy],
            ctx.args,
            rebalance_stride=rebalance_stride,
            out=out,
        )
    )
