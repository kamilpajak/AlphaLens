"""Scuttlebutt layer — qualitative web-grounded context via Perplexity (#507 PR-7a).

Phil Fisher's "scuttlebutt" method (which Buffett adopted): gather the qualitative
picture of a business from sources around it — competitors, customers, suppliers,
the trade press — that the 10-K does not give you. This module fetches that
NARRATIVE per candidate through the canonical
:class:`~alphalens_pipeline.literature_scanner.perplexity_client.PerplexityClient`.
Perplexity is web-grounded, so it is the ONE legitimate LLM path for *recent*
qualitative facts (no training-cutoff blindness — it reads live sources).

DOCTRINE — the result is QUAL-ONLY. The :class:`Scuttlebutt` text is raw prose
fed to the qualitative classifier as context; it is NEVER parsed for a number and
the dataclass carries no numeric field, so a figure Perplexity happens to return
can never become a quant panel value. The query explicitly steers Perplexity away
from precise figures; authoritative numbers stay sourced from XBRL/yfinance/SEC.

Fail-soft: a client error or an empty response degrades to ``ok=False`` with empty
text (never raises) — a thematic basket of small names will often have thin web
coverage, and that absence is honest signal, not a crash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alphalens_pipeline.literature_scanner.perplexity_client import (
    PerplexityClient,
    SearchContextSize,
)

logger = logging.getLogger(__name__)

# Grounded-search knobs. ``recency="year"`` keeps the picture current without
# discarding the last few quarters; ``context="medium"`` balances cost vs depth
# (one call per candidate, ~$0.02-0.05/ticker).
DEFAULT_SCUTTLEBUTT_RECENCY = "year"
DEFAULT_SCUTTLEBUTT_CONTEXT: SearchContextSize = "medium"


@dataclass(frozen=True, slots=True)
class Scuttlebutt:
    """One candidate's web-grounded qualitative context.

    ``text`` is raw prose for the classifier — never parsed for numbers. ``ok`` is
    ``False`` (with empty ``text``) on any fail-soft path (client error / empty
    response), so the caller simply omits the scuttlebutt section.
    """

    ticker: str
    text: str
    ok: bool


def build_scuttlebutt_query(ticker: str, company_name: str | None) -> str:
    """Build the grounded scuttlebutt query — qualitative signal only.

    Asks for the three Fisher dimensions (competitive position, customer/supplier
    concentration, management reputation/candor) as PROSE, and explicitly tells
    Perplexity to avoid precise figures so no number it surfaces is mistaken for
    an authoritative value.
    """
    who = f"{company_name} ({ticker})" if company_name else ticker
    return (
        f"You are doing 'scuttlebutt' research on {who} for a long-term investor. "
        "Summarize, in prose, the qualitative picture from recent third-party sources "
        "(competitors, customers, suppliers, trade press, analysts):\n"
        "1. Competitive position — who is gaining or losing share against this company, and why; "
        "how durable its advantage looks versus rivals.\n"
        "2. Customer and supplier concentration — any heavy dependence on a single large customer, "
        "channel, or single-source supplier, as a qualitative risk.\n"
        "3. Management reputation and candor — how management is regarded on execution, capital "
        "allocation, and honesty about problems.\n"
        "Write 2-4 short paragraphs of narrative. Do NOT report precise figures, percentages, or "
        "dollar amounts — describe direction and magnitude qualitatively (e.g. 'heavily reliant', "
        "'losing ground'); authoritative numbers are sourced elsewhere."
    )


def fetch_scuttlebutt(
    ticker: str,
    *,
    client: PerplexityClient,
    company_name: str | None = None,
    recency: str = DEFAULT_SCUTTLEBUTT_RECENCY,
    context_size: SearchContextSize = DEFAULT_SCUTTLEBUTT_CONTEXT,
) -> Scuttlebutt:
    """Fetch one candidate's scuttlebutt context (fail-soft, qual-only).

    Routes through the injected canonical ``PerplexityClient``. Returns
    ``Scuttlebutt(ok=True, text=...)`` on a non-empty response, else
    ``Scuttlebutt(ok=False, text="")`` — a client exception or blank/whitespace
    answer is swallowed and logged, never raised.
    """
    query = build_scuttlebutt_query(ticker, company_name)
    try:
        answer = client.ask(
            query,
            search_context_size=context_size,
            search_recency_filter=recency,
        )
    except Exception as exc:  # fail-soft: thin web coverage is not a crash
        logger.warning("scuttlebutt fetch failed for %s: %s", ticker, exc)
        return Scuttlebutt(ticker=ticker, text="", ok=False)
    if not isinstance(answer, str) or not answer.strip():
        logger.info("scuttlebutt: empty response for %s", ticker)
        return Scuttlebutt(ticker=ticker, text="", ok=False)
    return Scuttlebutt(ticker=ticker, text=answer.strip(), ok=True)


__all__ = [
    "DEFAULT_SCUTTLEBUTT_CONTEXT",
    "DEFAULT_SCUTTLEBUTT_RECENCY",
    "Scuttlebutt",
    "build_scuttlebutt_query",
    "fetch_scuttlebutt",
]
