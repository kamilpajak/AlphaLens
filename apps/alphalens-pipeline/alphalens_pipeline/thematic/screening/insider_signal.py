"""Layer 4 signal 1 — Cohen-Malloy opportunistic insider buying (paradigm #11).

Two-stage Form-4 load pattern from the neutral
:mod:`alphalens_pipeline.thematic.sources.form4_store` (shared with the Layer 3
``verification.insider`` gate). The v2 signal (see ``INSIDER_SIGNAL_VERSION``):

- ``score_usd`` = **buy-only** opportunistic USD over a **180d** window (was net
  buy−sell over 90d). Sales are weakly/not informative and netting injects
  noise (Jeng-Metrick-Zeckhauser 2003; Lakonishok-Lee 2001).
- ``sector_percentile`` ranks the candidate ONLY among the cohort's net buyers,
  and is ``None`` unless the candidate itself is a net buyer — a zero / no-data
  ticker no longer borrows a high rank from net-selling peers (the v1
  ``<=``-percentile pathology where a 0-buy name ranked ~100th).

Returned shape:
``{"score_usd": float|None, "sector_percentile": float|None, "signal_version": str}``.
``None`` for ``score_usd`` means "no Form-4 data for ticker" (C5 tri-state —
distinct from ``0.0`` "no buying in window"). ``signal_version`` is the
poolability key stamped on every row. The locked, SHA-pinned net aggregator
(``compute_net_opportunistic_usd``) is retained for the paradigm-#11 audit +
the Phase-4 re-integration.
"""

from __future__ import annotations

import datetime as dt
import logging
from datetime import date as _date
from pathlib import Path

# LOAD-BEARING private imports: the buy-only aggregator below reuses these so
# its Cohen-Malloy classification + price resolution stay byte-identical to the
# SHA-locked opportunistic_form4 scorer (which must NOT be edited — its bytes
# define a pre-registered paradigm-#11 audit verdict). Do not rename/remove
# `_is_eligible_record` / `_resolve_price` / `_ClassifierCache` without updating
# this consumer in lockstep.
from alphalens_pipeline.scorers.opportunistic_form4 import (
    _ClassifierCache,
    _is_eligible_record,
    _resolve_price,
    aggregate_opportunistic_signal,
)
from alphalens_pipeline.thematic.screening._common import percentile_rank
from alphalens_pipeline.thematic.sources import form4_store
from alphalens_pipeline.thematic.sources.form4_store import (
    DEFAULT_FORM4_ROOT,
    DEFAULT_LOOKBACK_DAYS,
    filter_records,
)

logger = logging.getLogger(__name__)

# Poolability key for the Layer-4 insider signal — the SOLE discriminator the
# deferred Insider×EDGE calibration partitions by (mirrors disagreement.
# PANEL_CONFIG_VERSION). Bump on ANY change to how score_usd / sector_percentile
# are produced so old rows are never pooled with new ones. v2 = buy-only
# magnitude (was net buy−sell) over a 180d window (was 90d), ranked WITHIN the
# cohort's net buyers (was a `<=`-percentile over all peers that ranked a zero
# at ~100th whenever peers were net sellers).
INSIDER_SIGNAL_VERSION = "insider-v2-buyonly-180d-withinbuyers"

# The insider signal's own look-back. Distinct from the shared
# ``DEFAULT_LOOKBACK_DAYS`` (90) used by the Layer-3 verification gate: the
# opportunistic-buy alpha effect is concentrated 6–12 months out (Cohen-Malloy
# 2012; Lakonishok-Lee 2001), so 90d under-captures it.
INSIDER_SIGNAL_LOOKBACK_DAYS = 180


def compute_net_opportunistic_usd(
    *,
    ticker: str,
    asof: dt.date,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    form4_root: Path = DEFAULT_FORM4_ROOT,
) -> float | None:
    """Compute the scalar net opportunistic USD signal for ``ticker`` at ``asof``.

    ``None`` when no Form-4 history exists for ``ticker`` at all (unknown).
    ``0.0`` when history exists but the lookback window is empty (real "no
    activity" signal — peer-comparable). Any non-zero value reflects net
    opportunistic insider USD (sum of signed P/S transactions classified by
    Cohen-Malloy as OPPORTUNISTIC).
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
        return None

    recent = filter_records(ticker_history, ticker=ticker, asof=asof, lookback_days=lookback_days)
    if recent.empty:
        return 0.0

    active_insiders = set(recent["reporting_owner_cik"].dropna().astype(str))
    try:
        full_history = form4_store.load_form4_for_insiders(
            active_insiders, form4_root=form4_root, years=years
        )
    except Exception as exc:
        logger.warning("form4 cross-ticker load failed for %s: %s", ticker, exc)
        return None
    if full_history.empty:
        full_history = ticker_history

    classifier_cache = form4_store.MemoizedClassifier(full_history)
    return float(
        aggregate_opportunistic_signal(recent, asof=asof, classifier_cache=classifier_cache)
    )


def _aggregate_opportunistic_buys_usd(
    records,
    *,
    asof: _date,
    classifier_cache: _ClassifierCache,
) -> float:
    """Buy-only opportunistic USD for one ticker's windowed Form-4 ``records``.

    Sums only opportunistic open-market PURCHASE ('P') legs (sales ignored —
    buys and sells are asymmetric; netting injects noise). Lives HERE, not in
    the SHA-locked ``opportunistic_form4`` scorer (whose bytes define a
    pre-registered paradigm-#11 audit verdict), but reuses that module's
    ``_is_eligible_record`` + ``_resolve_price`` so the Cohen-Malloy
    classification + price-resolution rules stay byte-identical to the pinned
    net aggregator. Returns ``0.0`` on empty input (>= 0 always).
    """
    if records.empty:
        return 0.0
    classification_year = asof.year
    total = 0.0
    for row in records.itertuples(index=False):
        if row.transaction_code != "P":
            continue
        if not _is_eligible_record(row, classifier_cache, classification_year):
            continue
        price = _resolve_price(row, None)
        if price is None:
            continue
        total += float(row.transaction_shares) * float(price)
    return total


def compute_opportunistic_buy_usd(
    *,
    ticker: str,
    asof: dt.date,
    lookback_days: int = INSIDER_SIGNAL_LOOKBACK_DAYS,
    form4_root: Path = DEFAULT_FORM4_ROOT,
) -> float | None:
    """Buy-only sibling of :func:`compute_net_opportunistic_usd`.

    Sums only opportunistic open-market PURCHASE USD over the window (sales
    ignored, never netted). ``None`` when no Form-4 history exists for the
    ticker (unknown); ``0.0`` when history exists but no opportunistic buy
    falls in the window (peer-comparable "no buying"); otherwise ``> 0``.
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
        return None

    recent = filter_records(ticker_history, ticker=ticker, asof=asof, lookback_days=lookback_days)
    if recent.empty:
        return 0.0

    active_insiders = set(recent["reporting_owner_cik"].dropna().astype(str))
    try:
        full_history = form4_store.load_form4_for_insiders(
            active_insiders, form4_root=form4_root, years=years
        )
    except Exception as exc:
        logger.warning("form4 cross-ticker load failed for %s: %s", ticker, exc)
        return None
    if full_history.empty:
        full_history = ticker_history

    classifier_cache = form4_store.MemoizedClassifier(full_history)
    return float(
        _aggregate_opportunistic_buys_usd(recent, asof=asof, classifier_cache=classifier_cache)
    )


# Backwards-compatible private alias — existing call sites and tests inside
# this module reference _percentile_rank; the implementation now lives in
# _common.
_percentile_rank = percentile_rank


def score_insider(
    *,
    ticker: str,
    asof: dt.date,
    peers: list[str],
    lookback_days: int = INSIDER_SIGNAL_LOOKBACK_DAYS,
    form4_root: Path = DEFAULT_FORM4_ROOT,
) -> dict[str, float | str | None]:
    """Compute the ticker's buy-only score + WITHIN-BUYERS percentile rank.

    Returns ``{"score_usd", "sector_percentile", "signal_version"}``:

    - ``score_usd`` is the buy-only opportunistic USD (``None`` = no Form-4
      data; ``0.0`` = data but no buying in window; ``> 0`` = net buying).
    - ``sector_percentile`` ranks the candidate ONLY among the cohort's net
      buyers (peers with ``buy_usd > 0``). It is ``None`` unless the candidate
      itself is a net buyer — a zero / no-data ticker no longer borrows a high
      rank from net-selling peers (the v1 ``<=``-percentile pathology). Peers
      without Form-4 data or without buying are excluded from the buyer cohort.
    - ``signal_version`` is the poolability key stamped on every row.

    Tradeability filter (issue #197) is applied upstream in
    :func:`sic_index.iter_peers_fallback`; ``peers`` is already filtered.
    """
    candidate = compute_opportunistic_buy_usd(
        ticker=ticker, asof=asof, lookback_days=lookback_days, form4_root=form4_root
    )
    if candidate is None or candidate <= 0:
        # No data, or no opportunistic buying — not a buy signal, so no rank.
        return {
            "score_usd": candidate,
            "sector_percentile": None,
            "signal_version": INSIDER_SIGNAL_VERSION,
        }

    buyer_peers: list[float] = []
    for p in peers:
        if p.upper() == ticker.upper():
            continue
        ps = compute_opportunistic_buy_usd(
            ticker=p, asof=asof, lookback_days=lookback_days, form4_root=form4_root
        )
        if ps is not None and ps > 0:
            buyer_peers.append(ps)

    # A LONE buyer (no peer buyers to rank against) gets an explicitly absent
    # rank, not percentile_rank's empty-peers 50.0 midpoint — 50.0 would read
    # as "median buyer" when there is in fact no cohort to compare against.
    percentile = percentile_rank(candidate, buyer_peers) if buyer_peers else None
    return {
        "score_usd": candidate,
        "sector_percentile": percentile,
        "signal_version": INSIDER_SIGNAL_VERSION,
    }


__all__ = [
    "INSIDER_SIGNAL_LOOKBACK_DAYS",
    "INSIDER_SIGNAL_VERSION",
    "compute_net_opportunistic_usd",
    "compute_opportunistic_buy_usd",
    "score_insider",
]
