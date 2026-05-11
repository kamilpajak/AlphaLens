"""`alphalens preaudit` — fail-fast environment check before a real audit.

Two stages, in order:

1. Coverage — :func:`alphalens.preaudit.coverage.check_all_deps` peeks
   the strategy's data deps under ``~/.alphalens/`` for existence +
   date-range coverage. Cheap (~1 s). Fails before subprocess spawn.
2. Smoke — :func:`alphalens.preaudit.runner.run_smoke` executes the
   experiment script on a tiny universe + short window. Catches any
   end-to-end pipeline failure missed by coverage (import errors, hash
   drift, schema breakage, …).

Exit codes:
    0  — both stages pass
    1  — coverage fail OR smoke fail (detail printed to stderr)
    2  — unknown strategy / no SmokeProfile registered
"""

from __future__ import annotations

from pathlib import Path

import typer

from alphalens.preaudit.coverage import check_all_deps
from alphalens.preaudit.profiles import SMOKE_PROFILES, SmokeStatus
from alphalens.preaudit.runner import DEFAULT_SMOKE_TIMEOUT_S, run_smoke
from alphalens_cli.commands.audit import _SCRIPTS


def _resolve_profile(strategy: str):
    """Return the SmokeProfile or raise typer.Exit(2) with a helpful msg."""
    if strategy not in _SCRIPTS:
        typer.echo(
            f"ERROR: strategy {strategy!r} not in audit._SCRIPTS. Known: {sorted(_SCRIPTS)}",
            err=True,
        )
        raise typer.Exit(code=2)
    profile = SMOKE_PROFILES.get(strategy)
    if profile is None:
        typer.echo(
            f"ERROR: strategy {strategy!r} is registered for "
            f"`alphalens audit` but no SmokeProfile exists. "
            f"Add one to alphalens/preaudit/profiles.py::SMOKE_PROFILES "
            f"before using `alphalens preaudit {strategy}`.",
            err=True,
        )
        raise typer.Exit(code=2)
    return profile


def _run_coverage_stage(profile, root: Path) -> None:
    """Run stage 1 (coverage). Exit 1 on failure."""
    typer.echo(">>> stage 1/2: coverage check")
    report = check_all_deps(profile, root=root)
    for c in report.checks:
        ok = "PASS" if c.passed else c.status.value.upper()
        line = f"  [{ok:13s}] {c.dep.name:30s} {c.detail}".rstrip()
        typer.echo(line, err=not c.passed)
    if not report.passed:
        typer.echo(
            f"ERROR: coverage check failed; aborting before smoke "
            f"(would have wasted ~{DEFAULT_SMOKE_TIMEOUT_S}s of compute).",
            err=True,
        )
        raise typer.Exit(code=1)


def _run_smoke_stage(strategy: str, profile, timeout_s: int) -> None:
    """Run stage 2 (smoke subprocess). Exit 1 on failure/timeout."""
    typer.echo(">>> stage 2/2: smoke subprocess")
    result = run_smoke(strategy, profile=profile, timeout_s=timeout_s)
    duration_str = f"{result.duration_s:.1f}s" if result.duration_s is not None else "?"
    if result.status is SmokeStatus.PASS:
        typer.echo(f"  [PASS] smoke completed in {duration_str}")
        return
    typer.echo(
        f"  [{result.status.value.upper()}] exit={result.exit_code} duration={duration_str}",
        err=True,
    )
    if result.detail:
        typer.echo(result.detail, err=True)
    raise typer.Exit(code=1)


def preaudit_command(
    strategy: str = typer.Argument(
        ...,
        help="Strategy name — must match `alphalens audit` choices.",
    ),
    root: Path = typer.Option(
        Path.home() / ".alphalens",
        "--root",
        help="Data root containing the strategy's required dirs (default: ~/.alphalens).",
    ),
    timeout_s: int = typer.Option(
        DEFAULT_SMOKE_TIMEOUT_S,
        "--timeout-s",
        help=f"Wall-clock budget for the smoke subprocess (default: {DEFAULT_SMOKE_TIMEOUT_S}s).",
    ),
    skip_coverage: bool = typer.Option(
        False,
        "--skip-coverage",
        help="Skip the coverage check stage (smoke-only).",
    ),
    skip_smoke: bool = typer.Option(
        False,
        "--skip-smoke",
        help="Skip the smoke subprocess (coverage-only).",
    ),
) -> None:
    """Run pre-audit smoke for a registered strategy."""
    profile = _resolve_profile(strategy)
    typer.echo(
        f">>> preaudit {strategy} (smoke window {profile.smoke_window[0]}..{profile.smoke_window[1]})"
    )
    if not skip_coverage:
        _run_coverage_stage(profile, root)
    if not skip_smoke:
        _run_smoke_stage(strategy, profile, timeout_s)
    typer.echo(">>> preaudit OK — strategy is ready for `alphalens audit`")
