"""The ``experts`` CLI app — registry-driven enrichment over the daily brief.

``experts enrich DATE (--expert <id> | --all)`` runs the qualitative enrichment for
the chosen expert(s) over a brief parquet; ``experts migrate-qual-cache`` relocates
an expert's legacy cache into the versioned layout. Replaces the old
``buffett qual-enrich`` / ``buffett migrate-qual-cache`` (no alias). The ad-hoc
``buffett lens`` observational table stays under the ``buffett`` app.

Heavy imports are lazy inside the command bodies (the frequent edgar-detect cron
must not pay store / pandas import cost it never uses).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import typer

experts_app = typer.Typer(
    name="experts",
    help="Expert-panel enrichment over the thematic brief (Buffett value/quality; more to come).",
    no_args_is_help=True,
)

_DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"


@experts_app.command(name="enrich")
def enrich_command(
    brief_date: str = typer.Argument(
        ..., metavar="DATE", help="Brief date (YYYY-MM-DD) whose survivors to enrich."
    ),
    expert: str | None = typer.Option(
        None, "--expert", help="Enrich with a single expert id (mutually exclusive with --all)."
    ),
    all_experts_flag: bool = typer.Option(
        False,
        "--all",
        help="Enrich with every registered expert (mutually exclusive with --expert).",
    ),
    briefs_dir: Path = typer.Option(
        _DEFAULT_BRIEFS_DIR,
        "--briefs-dir",
        help="Directory of daily thematic brief parquets (stamped in place).",
    ),
    scuttlebutt: bool = typer.Option(
        False,
        "--scuttlebutt",
        help="Also feed Perplexity web-grounded context to the classifier (needs PERPLEXITY_API_KEY).",
    ),
    cache_dir: Path | None = typer.Option(
        None,
        "--cache-dir",
        help="Qual-result cache root (default ~/.alphalens/buffett_qual).",
    ),
) -> None:
    """Eagerly compute + cache each expert's qualitative layer and stamp it into the brief.

    Reads the brief parquet ONCE, lets each qual-capable expert stamp its columns
    into one frame, then writes ONCE atomically. Each result is cached immutably so
    the 6x/day pipeline reruns never re-pay the LLM. Fail-soft per expert.
    """
    if expert is not None and all_experts_flag:
        raise typer.BadParameter("pass exactly one of --expert <id> or --all, not both")
    if expert is None and not all_experts_flag:
        raise typer.BadParameter("pass exactly one of --expert <id> or --all")
    try:
        target = dt.date.fromisoformat(brief_date)
    except ValueError as exc:
        raise typer.BadParameter(f"DATE must be YYYY-MM-DD: {exc}") from exc

    # Lazy imports — keep the frequent-cron `alphalens` startup cheap.
    from alphalens_pipeline.data.alt_data.yfinance_client import get_default_yfinance_client
    from alphalens_pipeline.data.store.edgar_fundamentals import EdgarFundamentalsStore
    from alphalens_pipeline.experts.base import QualEnrichExpert
    from alphalens_pipeline.experts.enrich import enrich_briefs
    from alphalens_pipeline.experts.registry import all_experts, get_expert
    from alphalens_pipeline.thematic.verification.mcap_filter import fetch_mcap

    from alphalens_cli.commands.buffett import _build_exec_comp_fn

    if all_experts_flag:
        # --all enriches every QUAL-capable expert; a numeric-only expert (O'Neil)
        # is simply skipped inside enrich_briefs — no error, its numerics are
        # stamped at the score stage, not here.
        experts = all_experts()
    else:
        try:
            chosen = get_expert(expert)  # type: ignore[arg-type]  # guarded non-None above
        except KeyError as exc:
            raise typer.BadParameter(f"unknown expert id: {expert!r}") from exc
        # A single numeric-only expert has no qualitative layer to eager-enrich;
        # reject explicitly rather than silently no-op (its numerics ride the score
        # stage). --all stays tolerant; this guard is for the targeted single case.
        if not isinstance(chosen, QualEnrichExpert):
            raise typer.BadParameter(
                f"expert {expert!r} is numeric-only — it has no qualitative layer to "
                f"enrich (its numerics are stamped at the score stage)"
            )
        experts = (chosen,)

    store = EdgarFundamentalsStore(with_prices=True)
    dividends_fn = get_default_yfinance_client().dividends

    try:
        counts = enrich_briefs(
            target,
            experts=experts,
            briefs_dir=briefs_dir,
            store=store,
            mcap_fn=fetch_mcap,
            dividends_fn=dividends_fn,
            exec_comp_fn=_build_exec_comp_fn(),
            scuttlebutt=scuttlebutt,
            cache_dir=cache_dir,
        )
    except FileNotFoundError as exc:
        raise typer.BadParameter(
            f"brief parquet not found for {target.isoformat()}: {exc}"
        ) from exc

    out_path = briefs_dir / f"{target.isoformat()}.parquet"
    for expert_id, n_classified in counts.items():
        typer.echo(
            f"experts enrich {target.isoformat()} [{expert_id}]: "
            f"classified {n_classified} names → {out_path}"
        )


@experts_app.command(name="migrate-qual-cache")
def migrate_qual_cache_command(
    expert: str = typer.Option(
        "buffett", "--expert", help="Expert whose legacy qual cache to migrate."
    ),
    cache_dir: Path | None = typer.Option(
        None,
        "--cache-dir",
        help="Qual-result cache root (default ~/.alphalens/buffett_qual).",
    ),
) -> None:
    """One-shot, idempotent move of an expert's legacy untagged qual cache into version tiers.

    MUST run before the first ``experts enrich`` of a deploy carrying the cache-key
    change, so cached names short-circuit instead of recomputing a possibly-different
    verdict. Safe to re-run.
    """
    # Lazy import — keep the frequent-cron `alphalens` startup cheap.
    from alphalens_pipeline.experts.base import QualEnrichExpert
    from alphalens_pipeline.experts.registry import get_expert

    try:
        chosen = get_expert(expert)
    except KeyError as exc:
        raise typer.BadParameter(f"unknown expert id: {expert!r}") from exc
    if not isinstance(chosen, QualEnrichExpert):
        raise typer.BadParameter(f"expert {expert!r} has no qualitative cache to migrate")

    n_migrated = chosen.migrate_qual_cache(cache_dir)
    typer.echo(f"experts migrate-qual-cache [{expert}]: moved {n_migrated} legacy file(s)")


__all__ = ["experts_app"]
