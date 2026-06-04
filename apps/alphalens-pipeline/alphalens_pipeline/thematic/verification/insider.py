"""Form-4 opportunistic-insider verification gate (paradigm #11 reuse).

Wraps :func:`alphalens_pipeline.scorers.opportunistic_form4.aggregate_opportunistic_signal`
to deliver a simple yes/no signal for the Layer 3 verification orchestrator.

Reuses paradigm #11 (gross αt +2.71 OOS validated) as a corroboration signal —
NOT as a standalone strategy. The companion ledger entry in CLAUDE.md /
project memory documents the policy: scorer reuse OK, compound architecture
NOT.

The Form-4 load + Cohen-Malloy classification primitives live in the neutral
:mod:`alphalens_pipeline.thematic.sources.form4_store` so the Layer 4
screening signal can share them without reaching into this gate's internals.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from alphalens_pipeline.scorers.opportunistic_form4 import (
    aggregate_opportunistic_signal,
)
from alphalens_pipeline.thematic.sources import form4_store
from alphalens_pipeline.thematic.sources.form4_store import (
    DEFAULT_FORM4_ROOT,
    DEFAULT_LOOKBACK_DAYS,
    filter_records,
)

logger = logging.getLogger(__name__)

DEFAULT_USD_THRESHOLD = 50_000  # $50k net opportunistic buy = meaningful


def has_opportunistic_buy(
    *,
    ticker: str,
    asof: dt.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    usd_threshold: float = DEFAULT_USD_THRESHOLD,
    form4_root: Path = DEFAULT_FORM4_ROOT,
) -> bool | None:
    """Layer 3 verification gate: net opportunistic insider buy over threshold?

    Tri-state: ``True`` (net buy ≥ threshold), ``False`` (window had trades
    but didn't qualify — sold, routine class, or below threshold), ``None``
    (no Form-4 data available for ticker, or loader exception — orchestrator
    records as unknown, NOT a false negative).

    Two-step Form-4 load so Cohen-Malloy sees each insider's FULL cross-ticker
    history. A March-every-year trader is ROUTINE regardless of WHICH ticker
    they touch; a ticker-restricted view would mislabel them as opportunistic
    on whichever ticker first breaks the pattern.
    """
    years = form4_store.classification_years(asof)
    try:
        ticker_history = form4_store.load_form4_for_ticker(
            ticker, form4_root=form4_root, years=years
        )
    except Exception as exc:
        logger.warning("form4 load failed for %s: %s", ticker, exc)
        return None
    if ticker_history.empty:
        # No data anywhere for this ticker — distinct from "data present but
        # window/threshold rejected". Surface as unknown so the operator can
        # tell the difference downstream.
        return None

    recent = filter_records(ticker_history, ticker=ticker, asof=asof, lookback_days=lookback_days)
    if recent.empty:
        # Ticker has Form-4 history but no trades in the lookback window —
        # that IS a real "no recent insider activity" signal, not missing data.
        return False

    active_insiders = set(recent["reporting_owner_cik"].dropna().astype(str))
    try:
        full_history = form4_store.load_form4_for_insiders(
            active_insiders, form4_root=form4_root, years=years
        )
    except Exception as exc:
        logger.warning("form4 cross-ticker load failed for %s: %s", ticker, exc)
        return None
    if full_history.empty:
        # Degrade gracefully — at least classify on the visible trades.
        full_history = ticker_history

    classifier_cache = form4_store.MemoizedClassifier(full_history)
    net_usd = aggregate_opportunistic_signal(recent, asof=asof, classifier_cache=classifier_cache)
    return net_usd >= usd_threshold


__all__ = [
    "DEFAULT_FORM4_ROOT",
    "DEFAULT_LOOKBACK_DAYS",
    "DEFAULT_USD_THRESHOLD",
    "filter_records",
    "has_opportunistic_buy",
]
