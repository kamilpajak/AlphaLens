"""Real-time market-cap bracket filter for thematic candidates.

LLM-side mcap constraints in the Pro mapper prompt are unreliable: the model
filters against its training-cutoff snapshot, not current prices. A 2026-05-17
probe showed Pro believing QUBT mcap = $50M (May 2024 snapshot) versus the
real $1.78B as of April 2026 — a 35x miss that systematically excluded names
that rallied since the model's cutoff.

This module is the orchestrator's post-LLM filter: yfinance fast_info lookup,
drop tickers outside ``[min_cap, max_cap]`` (or with no mcap available).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def fetch_mcap(ticker: str) -> float | None:
    """Fetch current market cap for ``ticker`` via yfinance fast_info.

    Returns ``None`` on any failure (delisted, network error, missing field)
    so callers can drop the candidate rather than crash the batch.
    """
    try:
        import yfinance as yf

        # FastInfo dict-style `.get("market_cap")` returns None because the
        # dict keys are different from the attribute names; use attribute
        # access which is the actual yfinance contract.
        mc = yf.Ticker(ticker).fast_info.market_cap
        if mc is None:
            return None
        return float(mc)
    except Exception as exc:
        logger.warning("mcap fetch failed for %s: %s", ticker, exc)
        return None


def filter_by_mcap(tickers: list[str], *, min_cap: int, max_cap: int) -> dict[str, float]:
    """Return ``{ticker: mcap}`` for tickers whose current mcap is in bracket.

    Tickers with mcap below ``min_cap``, above ``max_cap``, or unavailable are
    silently dropped — the gate's job is to enforce the bracket, not signal
    why a candidate was excluded.
    """
    kept: dict[str, float] = {}
    for t in tickers:
        mc = fetch_mcap(t)
        if mc is None:
            continue
        if min_cap <= mc <= max_cap:
            kept[t] = mc
    return kept


__all__ = ["fetch_mcap", "filter_by_mcap"]
