"""`alphalens api` — REST API server + cache rebuild."""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from alphalens.api import cache as cache_module
from alphalens.api import db as db_module

api_app = typer.Typer(
    name="api",
    help="Briefs REST API (FastAPI + SQLite cache).",
    no_args_is_help=True,
)

logger = logging.getLogger(__name__)


@api_app.callback()
def _api_callback() -> None:
    """Multi-command shape even with two commands."""


@api_app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind interface."),
    port: int = typer.Option(8000, "--port", help="TCP port."),
    db_path: Path = typer.Option(
        db_module.DEFAULT_DB_PATH,
        "--db",
        help="SQLite cache path (read-only at request time).",
    ),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload (development)."),
) -> None:
    """Run the FastAPI server with uvicorn.

    The DB path is passed via the ``ALPHALENS_CACHE_DB`` env var so the factory
    picks it up. Use ``alphalens api rebuild-cache`` before serving.
    """
    import os

    import uvicorn

    os.environ[db_module.ENV_DB_PATH] = str(db_path)
    typer.echo(f"AlphaLens Briefs API → http://{host}:{port}  (db={db_path})")
    uvicorn.run(
        "alphalens.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
    )


@api_app.command("rebuild-cache")
def rebuild_cache(
    briefs_dir: Path = typer.Option(
        cache_module.DEFAULT_BRIEFS_DIR,
        "--briefs-dir",
        help="Parquet root produced by `alphalens thematic brief`.",
    ),
    db_path: Path = typer.Option(
        db_module.DEFAULT_DB_PATH,
        "--db",
        help="SQLite cache destination.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Ignore parquet mtime gate; rebuild every date."
    ),
) -> None:
    """Bring the SQLite cache in line with the parquet directory.

    Incremental by default: only dates whose parquet ``mtime`` differs from the
    cached value are re-loaded. ``--force`` rebuilds everything (use after a
    schema bump or when debugging).
    """
    result = cache_module.rebuild_from_parquet(briefs_dir=briefs_dir, db_path=db_path, force=force)
    typer.echo(
        f"cache: {result.n_rebuilt} rebuilt, {result.n_skipped} skipped, "
        f"{result.n_deleted} dropped, {result.total_briefs} rows written → {result.db_path}"
    )
    if result.rebuilt_dates:
        typer.echo("  rebuilt: " + ", ".join(result.rebuilt_dates))
    if result.deleted_dates:
        typer.echo("  dropped: " + ", ".join(result.deleted_dates))
