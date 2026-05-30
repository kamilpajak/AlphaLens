"""`alphalens literature` — periodic literature scan (weekly + monthly)."""

from __future__ import annotations

import logging
import os
from datetime import date
from enum import StrEnum
from pathlib import Path

import typer
from alphalens_pipeline.literature_scanner.runner import (
    default_period,
    run_monthly,
    run_weekly,
)
from alphalens_pipeline.observability.textfile import emit_domain_metrics

literature_app = typer.Typer(
    name="literature",
    help="Periodic literature scan via Perplexity + Telegram digest.",
    no_args_is_help=True,
)

DEFAULT_OUTPUT_DIR = Path("docs/research/literature_review")

logger = logging.getLogger(__name__)


class ScanWindow(StrEnum):
    weekly = "weekly"
    monthly = "monthly"


@literature_app.callback()
def _literature_callback() -> None:
    """Force multi-command behaviour even when only one command is registered."""


def _resolve_credentials() -> tuple[str, str, str]:
    api_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not api_key:
        raise typer.BadParameter("PERPLEXITY_API_KEY missing from environment (set in .env).")
    return (
        api_key,
        os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        os.environ.get("TELEGRAM_CHAT_ID", ""),
    )


def _resolve_output_dir(custom: Path | None) -> Path:
    if custom is not None:
        return custom
    # File lives at apps/alphalens-pipeline/alphalens_cli/commands/literature.py;
    # repo root is four parents up (commands → alphalens_cli → alphalens-pipeline → apps → root).
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / DEFAULT_OUTPUT_DIR


@literature_app.command(name="scan")
def scan(
    window: ScanWindow = typer.Option(
        ...,
        "--window",
        help="Scan cadence: 'weekly' (RSS top-3, ~15min) or 'monthly' (5-filter triage, ~1h).",
    ),
    period: str = typer.Option(
        "",
        help="Explicit period override; YYYY-Www for weekly, YYYY-MM for monthly (defaults to today).",
    ),
    output_dir: Path = typer.Option(
        None,
        "--output-dir",
        help="Override output directory (defaults to docs/research/literature_review).",
    ),
) -> None:
    """Scan recent literature: Perplexity search → markdown report → Telegram digest."""
    api_key, bot_token, chat_id = _resolve_credentials()
    cadence = window.value
    resolved_period = period or default_period(date.today(), cadence=cadence)
    out_dir = _resolve_output_dir(output_dir)

    logger.info("Running %s literature scan for %s", cadence, resolved_period)
    runner = run_weekly if window is ScanWindow.weekly else run_monthly
    result = runner(
        output_dir=out_dir,
        perplexity_api_key=api_key,
        telegram_bot_token=bot_token,
        telegram_chat_id=chat_id,
        period=resolved_period,
    )
    logger.info("Wrote %s; trigger=%s", result.path, result.has_trigger)

    # Domain counters for the cron-observability dashboard (PR-2 of
    # the epic). ``has_trigger`` is exposed as 0/1 so Grafana can
    # plot "weeks with at least one trigger" without a separate
    # boolean datatype. Paper count is deliberately not emitted —
    # the runner does not currently surface it through ReviewResult;
    # adding it would require touching the scanner internals and is
    # better split into a follow-up PR.
    emit_domain_metrics(
        job=f"literature-scan-{cadence}",
        metrics={
            f'alphalens_literature_last_run_trigger{{window="{cadence}"}}': int(result.has_trigger),
        },
    )
