"""Layer 4 signal 1 — Cohen-Malloy opportunistic insider buying (paradigm #11).

Wraps :func:`alphalens_pipeline.scorers.opportunistic_form4.aggregate_opportunistic_signal`
with the two-stage Form-4 load pattern from the neutral
:mod:`alphalens_pipeline.thematic.sources.form4_store` (shared with the Layer 3
``verification.insider`` gate) to produce a scalar ``net_oppor_usd`` per
ticker, then ranks the candidate within its industry peer set via simple
percentile-of-the-cohort (no Bayesian shrinkage; small cohorts get noisy
ranks — design accepted).

Returned shape: ``{"score_usd": float | None, "sector_percentile": float | None}``.

``None`` for ``score_usd`` means "no Form-4 data available for ticker" (per
the C5 tri-state semantic established for verification gates — distinct from
"signal present but small"). When ``score_usd`` is ``None`` the percentile is
also ``None``.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from alphalens_pipeline.scorers.opportunistic_form4 import (
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


# Backwards-compatible private alias — existing call sites and tests inside
# this module reference _percentile_rank; the implementation now lives in
# _common.
_percentile_rank = percentile_rank


def score_insider(
    *,
    ticker: str,
    asof: dt.date,
    peers: list[str],
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    form4_root: Path = DEFAULT_FORM4_ROOT,
) -> dict[str, float | None]:
    """Compute the ticker's score + percentile rank within ``peers``.

    Peers without Form-4 data are skipped (do not anchor the cohort at zero).
    Candidate ``ticker`` is auto-included even if absent from ``peers``.

    Tradeability filter (issue #197) is applied upstream in
    :func:`sic_index.iter_peers_fallback`; ``peers`` is already filtered.
    """
    candidate = compute_net_opportunistic_usd(
        ticker=ticker, asof=asof, lookback_days=lookback_days, form4_root=form4_root
    )
    if candidate is None:
        return {"score_usd": None, "sector_percentile": None}

    peer_scores: list[float] = []
    for p in peers:
        if p.upper() == ticker.upper():
            continue
        ps = compute_net_opportunistic_usd(
            ticker=p, asof=asof, lookback_days=lookback_days, form4_root=form4_root
        )
        if ps is not None:
            peer_scores.append(ps)

    percentile = percentile_rank(candidate, peer_scores)
    return {"score_usd": candidate, "sector_percentile": percentile}


__all__ = ["compute_net_opportunistic_usd", "score_insider"]
