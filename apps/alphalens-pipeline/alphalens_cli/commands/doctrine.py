"""`alphalens audit-verdict` — apply the pre-registered DOCTRINE bars to the
four per-window ``run_audit`` JSONs.

Distinct from ``alphalens audit`` (whose ``robust_verdict`` only checks
offset-phase stability at ``alpha_t >= 1.5``): this command enforces the
project-doctrine stack (3.5 full-sample / 2.5 phase-mean / per-phase > 0 /
net-15bps >= 2.0 / AV-PIT). Exits non-zero on a doctrine FAIL so a launch
script cannot silently treat a 1.5-gate "PASS" as a doctrine PASS.

Run the four audits first (each is a separate ``alphalens audit`` invocation):
the full span 2018..2026 (gate 1), then the IS / OOS / FL phase windows
(gates 2-4). Then::

    alphalens audit-verdict --full full.json --is is.json --oos oos.json \\
        --fl fl.json --av-pit-passed --out doctrine_verdict.json
"""

from __future__ import annotations

import json
from pathlib import Path

import typer


def audit_verdict_command(
    full: Path = typer.Option(
        ..., "--full", help="Full-span (2018..2026) run_audit JSON — doctrine gate 1."
    ),
    is_window: Path = typer.Option(..., "--is", help="IS-phase run_audit JSON."),
    oos: Path = typer.Option(..., "--oos", help="OOS-phase run_audit JSON."),
    fl: Path = typer.Option(..., "--fl", help="FL-phase run_audit JSON."),
    av_pit_passed: bool = typer.Option(
        False,
        "--av-pit-passed/--no-av-pit-passed",
        help="AV PIT validation gate (§3.1) status from the ledger (gate 5).",
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Optional path to write the doctrine verdict JSON."
    ),
) -> None:
    """Apply the doctrine PASS bars to per-window audit JSONs; exit non-zero on FAIL."""
    # Lazy import keeps the pipeline CLI free of top-level research imports.
    from alphalens_research.backtest.doctrine_verdict import evaluate_doctrine_from_jsons

    verdict = evaluate_doctrine_from_jsons(
        full=full, is_=is_window, oos=oos, fl=fl, av_pit_passed=av_pit_passed
    )
    payload = verdict.to_dict()
    typer.echo(json.dumps(payload, indent=2))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2))
    # PASS / PASS_MARGINAL → 0 (PASS_MARGINAL is paper-trade-only, not a hard
    # fail); doctrine FAIL → 1 so a launch script gates on it.
    raise typer.Exit(0 if verdict.verdict in ("PASS", "PASS_MARGINAL") else 1)
