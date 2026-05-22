"""Top-level `status` — globalny raport: queue + digest buffer + dedup count."""

from __future__ import annotations

from pathlib import Path

import typer
from alphalens_research.core.queue import default_queue_path


def status() -> None:
    """Report current state: queue, digest buffer, dedup count."""
    from alphalens_research.watchdog.status import collect_status, format_status

    home = Path.home() / ".alphalens" / "watchdog"
    result = collect_status(
        queue_path=default_queue_path(),
        digest_path=home / "digest.db",
        seen_path=home / "seen_events.db",
    )
    typer.echo(format_status(result))
