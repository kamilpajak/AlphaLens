"""`alphalens templates` — structured event templates (#143 / epic #321).

PR-1 ships two subcommands:

- ``validate <path>`` — JSON-Schema-validate a single YAML file or every
  ``*.yaml`` under a directory. Suitable as a pre-commit hook. Non-zero
  exit on any schema violation.
- ``evaluate <corpus-parquet>`` — run the engine over an existing
  ``~/.alphalens/thematic_news`` parquet + print per-template match rate
  + per-reason holdout summary. Makes PR-1 independently demonstrable —
  the analyst can iterate on a YAML without waiting for the PR-2 pipeline
  integration.

Pipeline integration of the engine happens in PR-2. This CLI surface
stays independent of that wiring so the analyst can iterate on the
template library against any historical corpus snapshot.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import typer
from alphalens_pipeline.thematic.extraction.templates.engine import TemplateEngine
from alphalens_pipeline.thematic.extraction.templates.entity_resolver import (
    EntityResolver,
)
from alphalens_pipeline.thematic.extraction.templates.holdout import (
    ALL_HOLDOUT_REASONS,
)
from alphalens_pipeline.thematic.extraction.templates.spec import Article
from alphalens_pipeline.thematic.extraction.templates.yaml_schema import (
    validate_template_file,
)

templates_app = typer.Typer(
    name="templates",
    help="Structured event templates (issue #143).",
    no_args_is_help=True,
)

logger = logging.getLogger(__name__)


# Default ship templates dir, resolved relative to the pipeline package so
# ``alphalens templates evaluate`` works out of the box without --templates-dir.
DEFAULT_TEMPLATES_DIR = (
    Path(__file__).parent.parent.parent
    / "alphalens_pipeline"
    / "thematic"
    / "extraction"
    / "templates"
    / "templates"
)


@templates_app.callback()
def _templates_callback() -> None:
    """Force multi-command behaviour even when only one command is registered."""


def _gather_yaml_files(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(target.glob("*.yaml"))
    if target.is_file():
        return [target]
    raise typer.BadParameter(f"path does not exist: {target}")


@templates_app.command("validate")
def validate(
    path: Path = typer.Argument(
        DEFAULT_TEMPLATES_DIR,
        help="Path to a YAML file or a directory of YAML files. "
        "Defaults to the shipped templates dir.",
    ),
) -> None:
    """JSON-Schema-validate template files. Non-zero exit on any violation."""
    files = _gather_yaml_files(path)
    if not files:
        typer.echo(f"no *.yaml files found under {path}")
        raise typer.Exit(code=1)

    total_errors = 0
    for f in files:
        errors = validate_template_file(f)
        if not errors:
            typer.echo(f"  ok  {f.name}")
            continue
        total_errors += len(errors)
        for err in errors:
            typer.echo(f"  ERR {err}")

    if total_errors:
        typer.echo(f"\n{total_errors} error(s) across {len(files)} file(s)")
        raise typer.Exit(code=1)
    typer.echo(f"\nok — {len(files)} template(s) validated")


@templates_app.command("evaluate")
def evaluate(
    corpus: Path = typer.Argument(
        ...,
        help="Path to a thematic_news parquet (one day, or a directory of days).",
    ),
    templates_dir: Path = typer.Option(
        DEFAULT_TEMPLATES_DIR,
        "--templates-dir",
        help="Template library directory.",
    ),
    company_tickers: Path | None = typer.Option(
        None,
        "--company-tickers",
        help="Path to SEC company_tickers.json (defaults to the EDGAR detector copy).",
    ),
    emit_metrics: bool = typer.Option(
        False,
        "--emit-metrics",
        help="Flush accumulator to Prometheus textfile (off by default for analyst iteration).",
    ),
) -> None:
    """Run the engine over a parquet corpus, print match-rate summary."""
    if not corpus.exists():
        raise typer.BadParameter(f"corpus path does not exist: {corpus}")

    # Load corpus — single file or every parquet in a dir (mirrors news_ingest
    # layout: one parquet per UTC day).
    if corpus.is_dir():
        frames = [pd.read_parquet(p) for p in sorted(corpus.glob("*.parquet"))]
        if not frames:
            raise typer.BadParameter(f"no *.parquet under {corpus}")
        df = pd.concat(frames, ignore_index=True)
    else:
        df = pd.read_parquet(corpus)

    if df.empty:
        typer.echo("corpus is empty — nothing to evaluate")
        return

    # Resolver: feed-tagged tickers are SoT, EDGAR detector's company_tickers
    # enriches the human-readable name.
    resolver_kwargs: dict = {}
    if company_tickers is not None:
        resolver_kwargs["company_tickers_path"] = company_tickers
    resolver = EntityResolver(**resolver_kwargs)

    engine = TemplateEngine.from_dir(templates_dir)
    if not engine.specs:
        raise typer.BadParameter(f"no templates found in {templates_dir}")

    matched_events: list = []
    for _, row in df.iterrows():
        # ``row.get("tickers")`` returns a numpy array for list-typed parquet
        # columns; bare ``or []`` raises "truth value ambiguous" on arrays.
        raw_tickers = row.get("tickers")
        tickers_list = list(raw_tickers) if raw_tickers is not None else []
        article = Article(
            id=str(row.get("id", "")),
            source=str(row.get("source", "")),
            title=str(row.get("title", "")),
            body=str(row.get("body", "")),
            url=str(row.get("url", "")),
            published_at=row.get("timestamp"),
            tickers_raw=tickers_list,
        )
        entities = resolver.resolve(article)
        event = engine.match(article, entities)
        if event is not None:
            matched_events.append(event)

    snap = engine.metrics.snapshot()
    total_articles = len(df)
    total_matched = len(matched_events)

    typer.echo("")
    typer.echo(f"corpus rows: {total_articles}")
    typer.echo(f"matched:     {total_matched} ({total_matched / total_articles:.1%})")
    typer.echo("")
    typer.echo("per-template:")
    for spec in engine.specs:
        attempts = snap["attempts"].get(spec.template_id, 0)
        matches = snap["matches"].get(spec.template_id, 0)
        rate = (matches / attempts) if attempts else 0.0
        typer.echo(
            f"  {spec.template_id:<32} attempts={attempts:<6} matches={matches:<6} rate={rate:.1%}"
        )

    typer.echo("")
    typer.echo("holdout reasons:")
    for reason in sorted(ALL_HOLDOUT_REASONS):
        typer.echo(f"  {reason:<32} {snap['holdout'].get(reason, 0)}")

    if emit_metrics:
        engine.metrics.flush(job="template-engine-evaluate")
        typer.echo("\nmetrics flushed to Prometheus textfile.")
