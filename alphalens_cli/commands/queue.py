"""`alphalens queue` — historical viewer over the candidate queue."""

from __future__ import annotations

import typer

from alphalens.core.queue import default_queue_path

queue_app = typer.Typer(
    name="queue",
    help="Historical scorer-stats viewer over ~/.alphalens/candidates.db.",
    no_args_is_help=True,
)


@queue_app.command(name="scorer-stats")
def scorer_stats(
    since_days: int = typer.Option(
        30, help="Only count Layer 3 runs finished within the last N days"
    ),
) -> None:
    """Layer 3 acceptance rate per scorer (historical viewer).

    Queries the candidate queue for completed analysis runs, groups by
    `source` (e.g. 'momentum' vs 'early-stage'), and reports decision
    distribution + accept rate (BUY+OVERWEIGHT / total). The Layer 3
    worker that populated this table is archived; this command remains
    as a viewer over historical runs already on disk.
    """
    from alphalens.core.scorer_stats import compute_scorer_stats, format_stats_table

    stats = compute_scorer_stats(default_queue_path(), since_days=since_days)
    typer.echo(f"=== Scorer stats — last {since_days} days ===")
    typer.echo(format_stats_table(stats))
