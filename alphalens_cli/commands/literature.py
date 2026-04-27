"""`alphalens literature` — periodic literature review (monthly + weekly)."""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

import typer

from alphalens.literature_review.runner import (
    default_period,
    run_monthly,
    run_weekly,
)

literature_app = typer.Typer(
    name="literature",
    help="Periodic literature review via Perplexity + Telegram digest.",
    no_args_is_help=True,
)

DEFAULT_OUTPUT_DIR = Path("docs/research/literature_review")

logger = logging.getLogger(__name__)


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
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / DEFAULT_OUTPUT_DIR


@literature_app.command(name="monthly")
def monthly(
    period: str = typer.Option(
        "",
        help="Period as YYYY-MM (defaults to today).",
    ),
    output_dir: Path = typer.Option(
        None,
        "--output-dir",
        help="Override output directory (defaults to docs/research/literature_review).",
    ),
) -> None:
    """Run monthly deep literature review (5-filter triage, ~1h Perplexity)."""
    api_key, bot_token, chat_id = _resolve_credentials()
    resolved_period = period or default_period(date.today(), cadence="monthly")
    out_dir = _resolve_output_dir(output_dir)

    logger.info("Running monthly literature review for %s", resolved_period)
    result = run_monthly(
        output_dir=out_dir,
        perplexity_api_key=api_key,
        telegram_bot_token=bot_token,
        telegram_chat_id=chat_id,
        period=resolved_period,
    )
    logger.info("Wrote %s; trigger=%s", result.path, result.has_trigger)


@literature_app.command(name="weekly")
def weekly(
    period: str = typer.Option(
        "",
        help="Period as YYYY-Www (defaults to today's ISO week).",
    ),
    output_dir: Path = typer.Option(
        None,
        "--output-dir",
        help="Override output directory.",
    ),
) -> None:
    """Run weekly RSS scan (top-3 papers, ~15min Perplexity)."""
    api_key, bot_token, chat_id = _resolve_credentials()
    resolved_period = period or default_period(date.today(), cadence="weekly")
    out_dir = _resolve_output_dir(output_dir)

    logger.info("Running weekly literature scan for %s", resolved_period)
    result = run_weekly(
        output_dir=out_dir,
        perplexity_api_key=api_key,
        telegram_bot_token=bot_token,
        telegram_chat_id=chat_id,
        period=resolved_period,
    )
    logger.info("Wrote %s; trigger=%s", result.path, result.has_trigger)
