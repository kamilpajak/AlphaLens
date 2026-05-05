"""`alphalens preregister` — strategy hypothesis ledger.

Closes Gap #1 of `docs/research/strategy_validation_playbook.md`. Use:

    alphalens preregister add --id ... --signal-class ... --params-file ...
    alphalens preregister threshold --signal-class momentum
    alphalens preregister complete <id> --verdict PASS|MID|FAIL ...
    alphalens preregister list [--signal-class CLASS]
    alphalens preregister show <id>

Default ledger lives at ``docs/research/preregistration/ledger.json``.

Imports of `alphalens.preregistration.ledger` are deliberately scoped
inside command bodies — same lazy-import discipline as
`alphalens_cli/commands/research.py` (Layer 1 watchdog cron must not pay
the import cost on every invoke).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import typer

_LEDGER_ROOT_HELP = "Override ledger directory."

preregister_app = typer.Typer(
    name="preregister",
    help="Strategy pre-registration ledger (multiple-testing accountability).",
    no_args_is_help=True,
)

DEFAULT_LEDGER_DIR = Path("docs/research/preregistration")


@preregister_app.callback()
def _preregister_callback() -> None:
    """Force multi-command behaviour even when only one command is registered."""


def _resolve_root(custom: Path | None) -> Path:
    if custom is not None:
        return custom
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / DEFAULT_LEDGER_DIR


@preregister_app.command(name="add")
def add(
    id: str = typer.Option(..., "--id", help="Unique slug for the hypothesis."),
    signal_class: str = typer.Option(
        ...,
        "--signal-class",
        help="Signal taxonomy bucket (e.g. momentum, fundamental_quality, insider_activity).",
    ),
    hypothesis: str = typer.Option(..., "--hypothesis", help="Plain-English claim."),
    scorer_path: str = typer.Option(..., "--scorer-path", help="Path to experiment script."),
    params_file: Path = typer.Option(
        ...,
        "--params-file",
        help="JSON with params_frozen, periods, success_criteria.",
        exists=True,
        readable=True,
    ),
    registered_at: str = typer.Option(
        "",
        "--registered-at",
        help="ISO date (YYYY-MM-DD); defaults to today.",
    ),
    ledger_root: Path = typer.Option(
        None,
        "--ledger-root",
        help="Override ledger directory (defaults to docs/research/preregistration).",
    ),
) -> None:
    """Register a frozen hypothesis BEFORE running the multi-phase audit."""
    from alphalens.preregistration.ledger import Ledger, Registration

    payload = json.loads(params_file.read_text())
    reg_date = date.fromisoformat(registered_at) if registered_at else date.today()

    reg = Registration(
        id=id,
        signal_class=signal_class,
        hypothesis=hypothesis,
        scorer_path=scorer_path,
        params_frozen=payload["params_frozen"],
        periods=payload["periods"],
        success_criteria=payload["success_criteria"],
        registered_at=reg_date,
    )
    ledger = Ledger(_resolve_root(ledger_root))
    try:
        ledger.add(reg)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Registered {id!r} in signal class {signal_class!r}.")
    n_in_class = ledger.count_in_class(signal_class)
    typer.echo(
        f"Signal class now has {n_in_class} hypothesis "
        f"{'tests' if n_in_class != 1 else 'test'} on record."
    )


@preregister_app.command(name="list")
def list_cmd(
    signal_class: str = typer.Option("", "--signal-class", help="Filter by class."),
    ledger_root: Path = typer.Option(None, "--ledger-root", help=_LEDGER_ROOT_HELP),
) -> None:
    """List registrations (optionally filtered by signal class)."""
    from alphalens.preregistration.ledger import Ledger

    ledger = Ledger(_resolve_root(ledger_root))
    entries = ledger.list(signal_class=signal_class or None)
    if not entries:
        typer.echo("(no registrations)")
        return
    for reg in entries:
        outcome = ""
        if reg.outcome:
            verdict = reg.outcome.get("verdict", "?")
            alpha_t = reg.outcome.get("mean_alpha_t")
            if alpha_t is None:
                alpha_t = reg.outcome.get("primary_alpha_t_U3")
            alpha_str = f"{alpha_t:.2f}" if isinstance(alpha_t, (int, float)) else "—"
            outcome = f" → {verdict} (αt={alpha_str})"
        typer.echo(f"{reg.id}\t{reg.signal_class}\t{reg.status}{outcome}")


@preregister_app.command(name="show")
def show(
    id: str = typer.Argument(..., help="Registration id to show."),
    ledger_root: Path = typer.Option(None, "--ledger-root", help=_LEDGER_ROOT_HELP),
) -> None:
    """Print full registration record as JSON."""
    from alphalens.preregistration.ledger import Ledger

    ledger = Ledger(_resolve_root(ledger_root))
    try:
        reg = ledger.get(id)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(reg.to_dict(), indent=2))


@preregister_app.command(name="complete")
def complete(
    id: str = typer.Argument(..., help="Registration id to complete."),
    verdict: str = typer.Option(..., "--verdict", help="PASS | MID | FAIL"),
    mean_alpha_t: float = typer.Option(..., "--mean-alpha-t", help="Multi-phase mean α t-stat."),
    mean_excess_net: float = typer.Option(
        ..., "--mean-excess-net", help="Multi-phase mean excess net return (decimal)."
    ),
    audit_path: str = typer.Option(..., "--audit-path", help="Path to multi-phase audit JSON."),
    completed_at: str = typer.Option("", "--completed-at", help="ISO date; defaults to today."),
    notes: str = typer.Option("", "--notes", help="Optional free-text notes."),
    ledger_root: Path = typer.Option(None, "--ledger-root", help=_LEDGER_ROOT_HELP),
) -> None:
    """Record one-shot verdict for a previously registered hypothesis."""
    from alphalens.preregistration.ledger import Ledger

    ledger = Ledger(_resolve_root(ledger_root))
    completion_date = date.fromisoformat(completed_at) if completed_at else date.today()
    try:
        ledger.complete(
            id,
            verdict=verdict,
            mean_alpha_t=mean_alpha_t,
            mean_excess_net=mean_excess_net,
            audit_path=audit_path,
            completed_at=completion_date,
            notes=notes,
        )
    except (KeyError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Completed {id!r} with verdict {verdict}.")


@preregister_app.command(name="threshold")
def threshold(
    signal_class: str = typer.Option(..., "--signal-class", help="Signal class to query."),
    alpha: float = typer.Option(0.05, "--alpha", help="Family-wise error rate."),
    ledger_root: Path = typer.Option(None, "--ledger-root", help=_LEDGER_ROOT_HELP),
) -> None:
    """Print Bonferroni-adjusted critical |t| for hypotheses currently in this class."""
    from alphalens.preregistration.ledger import Ledger

    ledger = Ledger(_resolve_root(ledger_root))
    n = max(1, ledger.count_in_class(signal_class))
    crit = ledger.bonferroni_threshold(signal_class, alpha=alpha)
    typer.echo(f"Signal class {signal_class!r}: {n} tests at α={alpha} → critical |t| ≈ {crit:.2f}")
