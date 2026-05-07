"""`alphalens archive` — research replay tooling for ARCHIVED layers.

Per ADR 0005 (anti-pattern catalog policy) the closed paradigm-failure
strategies (Layer 2b themed, Layer 2d insider, Layer 2e rotation) keep
their scorer code in ``alphalens/archive/`` so future researchers can
replay the experiments and confirm the kill verdicts. Their CLI runners
live here under a single ``archive`` namespace to keep the top-level
``alphalens --help`` output focused on LIVE / ACTIVE workloads.

These commands are **NOT for capital deploy**. They exist to reproduce
historical results during postmortems or when revisiting the closure
verdict; do not wire them into launchd or use them to drive new
positioning.
"""

from __future__ import annotations

import typer

from alphalens_cli.commands.insider import insider_app
from alphalens_cli.commands.rotation import rotation_app
from alphalens_cli.commands.themed import themed_app

archive_app = typer.Typer(
    name="archive",
    help="Research replay for ARCHIVED layers (per ADR 0005). NOT for capital deploy.",
    no_args_is_help=True,
)

archive_app.add_typer(themed_app, name="themed")
archive_app.add_typer(insider_app, name="insider")
archive_app.add_typer(rotation_app, name="rotation")
