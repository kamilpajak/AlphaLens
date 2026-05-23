"""Orchestration for monthly and weekly literature review runs.

Flow:
1. Build prompt (monthly = 4 baskets + 5-filter triage; weekly = top-3 RSS scan).
2. Call Perplexity HTTP API.
3. Persist response to ``output_dir / period.md`` (or ``output_dir / weekly / period.md``).
4. If TRIGGER_REACTIVATION section has actual content, mark trigger flag.
5. Dispatch terse Telegram digest (skipped when bot_token / chat_id missing).

The runner is pure side-effects-on-the-edges: Perplexity client and Telegram
handler are dependency-injected via patches in tests.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from alphalens_pipeline.watchdog.dispatch.handlers.telegram import TelegramHandler

from .perplexity_client import PerplexityClient
from .prompts import build_monthly_prompt, build_weekly_prompt

logger = logging.getLogger(__name__)

Cadence = Literal["monthly", "weekly"]

TRIGGER_HEADING_RE = re.compile(
    r"^#{1,3}\s*TRIGGER_REACTIVATION[^\n]*$",
    re.MULTILINE,
)
NEXT_HEADING_RE = re.compile(r"^#{1,3}\s", re.MULTILINE)
TELEGRAM_DIGEST_LIMIT = 600
WEEKLY_TELEGRAM_LIMIT = 400


@dataclass
class ReviewResult:
    path: Path
    has_trigger: bool
    cadence: Cadence
    period: str


_DISMISSAL_RE = re.compile(
    r"^\s*(none|no\s+\S+|nothing)\b",
    re.IGNORECASE,
)


def has_reactivation_trigger(response: str) -> bool:
    """Return True iff the TRIGGER_REACTIVATION section has substantive content
    beyond a 'none' / 'no candidates' / 'nothing' disclaimer.

    Accepts variants like 'None of the above', 'None at this time',
    'No papers this period', 'No relevant work surfaced', 'Nothing meaningful'.
    """
    heading = TRIGGER_HEADING_RE.search(response)
    if not heading:
        return False
    body_start = heading.end()
    next_heading = NEXT_HEADING_RE.search(response, body_start)
    body_end = next_heading.start() if next_heading else len(response)
    body = response[body_start:body_end].strip()
    if not body:
        return False
    return not _DISMISSAL_RE.match(body)


def default_period(today: date, cadence: Cadence) -> str:
    if cadence == "monthly":
        return f"{today.year:04d}-{today.month:02d}"
    iso_year, iso_week, _ = today.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


def _digest_for(period: str, response: str, has_trigger: bool, limit: int, cadence: Cadence) -> str:
    header = "monthly" if cadence == "monthly" else "weekly"
    flag = "TRIGGER" if has_trigger else "no trigger"
    head = f"Literature {header} {period} — {flag}\n\n"
    excerpt = response.strip()
    remaining = limit - len(head) - 4
    if len(excerpt) > remaining:
        excerpt = excerpt[: max(remaining, 0)] + "..."
    return head + excerpt


def _persist(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _maybe_dispatch(bot_token: str, chat_id: str, digest: str) -> None:
    if not bot_token or not chat_id:
        logger.info("Telegram credentials missing; skipping digest dispatch")
        return
    handler = TelegramHandler(bot_token=bot_token, chat_id=chat_id)
    handler.send_message(digest)


def run_monthly(
    output_dir: Path,
    perplexity_api_key: str,
    telegram_bot_token: str,
    telegram_chat_id: str,
    period: str,
) -> ReviewResult:
    prompt = build_monthly_prompt(period=period)
    client = PerplexityClient(api_key=perplexity_api_key)
    response = client.ask(prompt, search_context_size="high", search_recency_filter="year")

    path = output_dir / f"{period}.md"
    _persist(path, response)
    has_trigger = has_reactivation_trigger(response)
    digest = _digest_for(period, response, has_trigger, TELEGRAM_DIGEST_LIMIT, "monthly")
    _maybe_dispatch(telegram_bot_token, telegram_chat_id, digest)

    return ReviewResult(path=path, has_trigger=has_trigger, cadence="monthly", period=period)


def run_weekly(
    output_dir: Path,
    perplexity_api_key: str,
    telegram_bot_token: str,
    telegram_chat_id: str,
    period: str,
) -> ReviewResult:
    prompt = build_weekly_prompt(period=period)
    client = PerplexityClient(api_key=perplexity_api_key)
    response = client.ask(prompt, search_context_size="medium", search_recency_filter="week")

    path = output_dir / "weekly" / f"{period}.md"
    _persist(path, response)
    has_trigger = has_reactivation_trigger(response)
    digest = _digest_for(period, response, has_trigger, WEEKLY_TELEGRAM_LIMIT, "weekly")
    _maybe_dispatch(telegram_bot_token, telegram_chat_id, digest)

    return ReviewResult(path=path, has_trigger=has_trigger, cadence="weekly", period=period)
