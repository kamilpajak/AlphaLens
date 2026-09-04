"""``alphalens events`` — the event-sourced candidate lane (epic #1293).

``insider-clusters`` detects insider purchase clusters for one brief date and
writes ``~/.alphalens/event_candidates/<date>.parquet``. ``thematic score``
merges the eligible rows only when ``ALPHALENS_EVENT_LANE=1`` (PR #1296); this
command never touches the thematic candidates parquet.

The detection module is imported inside the command body: it pulls the Form-4
store reader and the yfinance-backed helpers, which the frequently-invoked CLI
must not pay for at import time (same rule as the research commands).
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import typer
from alphalens_pipeline.events import DEFAULT_ACCEPTANCE_CACHE_DIR, DEFAULT_EVENT_CANDIDATES_DIR

logger = logging.getLogger(__name__)

events_app = typer.Typer(
    name="events",
    help="Event-sourced candidate lane: structured-filing triggers beside the thematic pipeline.",
    no_args_is_help=True,
)


@events_app.callback()
def _events_callback() -> None:
    """Force multi-command behaviour even when only one command is registered."""


@events_app.command("insider-clusters")
def insider_clusters(
    date: str = typer.Option(
        None, "--date", help="Brief date (asof) in YYYY-MM-DD (default: yesterday UTC)."
    ),
    form4_root: Path = typer.Option(
        None, "--form4-root", help="Form-4 store root (default ~/.alphalens/form4_parquet)."
    ),
    output_dir: Path = typer.Option(
        DEFAULT_EVENT_CANDIDATES_DIR, "--output-dir", help="Event-candidates parquet root."
    ),
    acceptance_cache_dir: Path = typer.Option(
        DEFAULT_ACCEPTANCE_CACHE_DIR,
        "--acceptance-cache-dir",
        help="Per-accession EDGAR acceptance-time cache.",
    ),
) -> None:
    """Detect insider purchase clusters for one brief date -> event_candidates parquet."""
    target = (
        dt.date.fromisoformat(date)
        if date
        else dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    )
    from alphalens_pipeline.events import insider_cluster_detect as det

    kwargs = {"asof": target, "acceptance_cache_dir": acceptance_cache_dir}
    if form4_root is not None:
        kwargs["form4_root"] = form4_root
    df = det.build_event_candidates(**kwargs)
    path = det.write_event_candidates(df, asof=target, output_dir=output_dir)
    eligible = int(df["eligible"].astype(bool).sum()) if len(df) else 0
    typer.echo(f"insider-clusters {target}: {len(df)} cluster(s), {eligible} eligible -> {path}")
    for r in df.to_dict("records"):
        status = "eligible" if bool(r["eligible"]) else str(r["exclusion_reason"])
        usd = float(r["event_cluster_usd"] or 0.0)
        typer.echo(
            f"  {r['ticker']!s:<8} insiders={r['event_n_insiders']} usd=${usd:,.0f} {status}"
        )
