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

REPO = Path(__file__).resolve().parent.parent.parent

# Mapping of short strategy names to their experiment script paths.
# Single source of truth — moved here from scripts/audit_multi_phase.py
# so that adding a new strategy means editing exactly one file.
_SCRIPTS: dict[str, Path] = {
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
    "insider_pc_compound": REPO / "scripts" / "experiment_insider_pc_compound.py",
    "ev_fcff_yield": REPO / "scripts" / "experiment_ev_fcff_yield.py",
    "pead_pss_v2_2026_05_13": REPO / "scripts" / "experiment_pead_pss_v2.py",
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
        REPO / "docs/research/multi_phase_audit.json",
        "--out",
        help="Output JSON path (default: REPO/docs/research/multi_phase_audit.json).",
    ),
) -> None:
    """Run the multi-phase audit driver for a registered strategy."""
    # Lazy import — running `alphalens --help` should not import the OSS
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
