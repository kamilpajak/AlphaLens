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
        --n-phases 5 --rebalance-stride 5

    alphalens audit insider_form4_opportunistic \\
        --is-start 2018-01-01 --is-end 2023-12-31

A quarterly-cadence audit is ``--n-phases 5 --rebalance-stride 63`` —
since PRB v0.3.0 the sweep size and the cadence are independent kwargs,
so no bespoke orchestrator script is needed to pin the phase count.

Extra args after ``--n-phases`` / ``--rebalance-stride`` / ``--out`` are
forwarded to the experiment script as positional arguments via ``ctx.args``.
"""

from __future__ import annotations

from pathlib import Path

import typer

# Experiment scripts live in the alphalens-research workspace member (they
# import from both alphalens_research.* and alphalens_pipeline.*; keeping
# them with the research lab matches their development cadence). The registry
# that maps a short strategy name to its script lives in the lab, beside the
# scripts it names:
#   apps/alphalens-research/alphalens_research/preaudit/strategies.py
# It is read lazily inside the command body, the way this module already reads
# the rest of the lab, so `alphalens_cli` keeps no top-level import of
# `alphalens_research` (ADR 0011).

# Still needed at module level: the `--out` default below is a `typer.Option`,
# and typer evaluates option defaults at import time, so this cannot be
# resolved lazily the way the registry is.
_WORKSPACE_ROOT = Path(__file__).resolve().parents[4]


def audit_command(
    ctx: typer.Context,
    strategy: str = typer.Argument(
        ...,
        help="Strategy name; see scripts/experiment_*.py for the full list.",
    ),
    n_phases: int = typer.Option(
        5,
        "--n-phases",
        help="Phase offsets to sweep (default 5; must be <= --rebalance-stride).",
    ),
    rebalance_stride: int = typer.Option(
        5,
        "--rebalance-stride",
        help="Rebalance cadence in trading days, forwarded to the experiment script.",
    ),
    out: Path = typer.Option(
        _WORKSPACE_ROOT / "docs/research/multi_phase_audit.json",
        "--out",
        help="Output JSON path (default: <workspace_root>/docs/research/multi_phase_audit.json).",
    ),
) -> None:
    """Run the multi-phase audit driver for a registered strategy."""
    # Both imports are lazy, for two DIFFERENT reasons — do not promote either
    # to module level:
    #   - `phase_robust_backtesting`: `alphalens --help` should not pay for the
    #     OSS methodology bundle (statsmodels, scipy) on every CLI invocation.
    #   - `alphalens_research`: ADR 0011 forbids a top-level `alphalens_cli` ->
    #     `alphalens_research` import, and `test_module_dependencies` enforces
    #     it with `top_level_only`, so moving this up turns the gate red.
    from alphalens_research.preaudit.strategies import AUDIT_SCRIPTS
    from phase_robust_backtesting.audit_multi_phase import run_audit

    if strategy not in AUDIT_SCRIPTS:
        typer.echo(
            f"Unknown strategy {strategy!r}. Choices: {sorted(AUDIT_SCRIPTS)}",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        rc = run_audit(
            AUDIT_SCRIPTS[strategy],
            ctx.args,
            n_phases=n_phases,
            rebalance_stride=rebalance_stride,
            out=out,
        )
    except ValueError as exc:
        # e.g. n_phases > rebalance_stride — a usage error, not a crash:
        # surface the driver's message without a traceback.
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    raise typer.Exit(rc)
