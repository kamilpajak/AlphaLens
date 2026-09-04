"""Point-in-time market-cap bracket filter for thematic candidates.

LLM-side mcap constraints in the Pro mapper prompt are unreliable: the model
filters against its training-cutoff snapshot, not current prices. A 2026-05-17
probe showed Pro believing QUBT mcap = $50M (May 2024 snapshot) versus the
real $1.78B as of April 2026 — a 35x miss that systematically excluded names
that rallied since the model's cutoff.

This module is the orchestrator's post-LLM filter: yfinance lookup, drop
tickers outside ``[min_cap, max_cap]`` (or with no mcap available).

When ``asof`` is given and predates today, mcap is recomputed as
``close(asof) × shares_outstanding_on_or_before(asof)`` so historical
replay isn't biased by today's price. When ``asof`` is None or in the
present, the faster ``fast_info.market_cap`` live path is used.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
from pathlib import Path
from typing import NamedTuple

from alphalens_pipeline.data.alt_data.yfinance_client import get_default_yfinance_client

logger = logging.getLogger(__name__)


def _finite_or_none(mcap: float | None) -> float | None:
    """Collapse a non-finite mcap (NaN / ±inf) to ``None``.

    A throttled yfinance response can yield a NaN price or NaN shares count, so
    ``close × shares`` comes back as NaN rather than a clean ``None``. NaN
    silently survives an ``mcap is not None`` check and then poisons the
    ``min_cap <= mcap <= max_cap`` bracket test — every comparison with NaN is
    False, so the candidate is dropped with no trace (incident 2026-07-25 —
    NaN PIT mcap collapsed a whole day's briefs). Routing every fetched value
    through this guard turns a non-finite result into an honest failure that
    callers can fall back on. ``math.isfinite`` is the check (NOT ``x == x``,
    which Sonar S1764 flags as identical sub-expressions).
    """
    if mcap is None or not math.isfinite(mcap):
        return None
    return mcap


# Persistent last-known live mcap, so a TRANSIENT yfinance/Yahoo outage does not
# silently drop every candidate (and collapse the brief to zero). On the live
# path we cache each success and fall back to a recent cached value on failure.
# mcap is slow-moving, so a value up to two weeks stale is a fine approximation
# for a wide market-cap BRACKET filter — far better than dropping the candidate.
_MCAP_CACHE_PATH = Path.home() / ".alphalens" / "mcap_cache.json"
_MCAP_CACHE_MAX_STALE_DAYS = 14
# Recency window within which a PIT-mcap failure may substitute today's LIVE mcap
# (the daily T-1 pipeline). A distinct knob from the cache-staleness tolerance
# above — same value today (mcap is slow-moving, so ≤2 weeks is a fine proxy for
# a wide BRACKET filter) but a separate concept: this bounds FORWARD BIAS, and an
# older backtest date must NEVER take the live proxy. Shorten it to tighten
# research purity at the cost of resilience over longer weekend/holiday gaps.
_PIT_LIVE_FALLBACK_MAX_AGE_DAYS = _MCAP_CACHE_MAX_STALE_DAYS


def fetch_mcap(ticker: str, *, asof: dt.date | None = None) -> float | None:
    """Fetch market cap for ``ticker``.

    Returns ``None`` on an unrecoverable failure so callers can drop the
    candidate rather than crash the batch.

    ``asof`` selects between the live and PIT paths:
    - ``None`` or today/future → ``fast_info.market_cap``, with a persistent
      cache fallback (a transient yfinance failure returns the last-known value
      if it is ≤ ``_MCAP_CACHE_MAX_STALE_DAYS`` old).
    - past date → ``close(asof) × shares_outstanding(≤ asof)``. On a PIT failure
      for a RECENT date (within ``_PIT_LIVE_FALLBACK_MAX_AGE_DAYS`` of today — i.e.
      the daily T-1 pipeline), fall back to the live mcap: yfinance's history
      endpoint rate-limits far more readily than the lighter ``fast_info`` one,
      and for a near-today date the live mcap is a fine proxy for the PIT mcap on
      a wide market-cap BRACKET filter — far better than dropping every candidate
      and collapsing the day's briefs to zero (incident 2026-07-25). An OLD
      backtest date gets NO live fallback: a today mcap there would be look-ahead
      forward bias.
    """
    if asof is None or asof >= dt.date.today():
        return _finite_or_none(_fetch_live_mcap_with_cache(ticker))
    mcap = _finite_or_none(_fetch_pit_mcap(ticker, asof))
    if mcap is not None:
        return mcap
    if asof >= dt.date.today() - dt.timedelta(days=_PIT_LIVE_FALLBACK_MAX_AGE_DAYS):
        live = _finite_or_none(_fetch_live_mcap_with_cache(ticker))
        if live is not None:
            logger.warning(
                "mcap PIT fetch failed for %s (asof %s); using live mcap %.0f as a "
                "recent-date proxy (PIT history endpoint likely rate-limited)",
                ticker,
                asof.isoformat(),
                live,
            )
        return live
    return None


def _fetch_live_mcap(ticker: str) -> float | None:
    """One live market-cap lookup via the canonical client; ``None`` on failure.

    The client owns the shared throttle + retry that keeps this filter's
    ~hundreds-of-tickers batch from tripping Yahoo's rate limit.
    """
    return get_default_yfinance_client().market_cap(ticker)


def _fetch_live_mcap_with_cache(ticker: str) -> float | None:
    """Live mcap with a persistent fallback for transient yfinance failures."""
    mc = _finite_or_none(_fetch_live_mcap(ticker))
    if mc is not None:
        _mcap_cache_put(ticker, mc)
        return mc
    cached = _mcap_cache_get(ticker)
    if cached is not None:
        logger.info(
            "mcap live fetch failed for %s; using cached %.0f (≤%dd old)",
            ticker,
            cached,
            _MCAP_CACHE_MAX_STALE_DAYS,
        )
    return cached


def _fetch_pit_mcap(ticker: str, asof: dt.date) -> float | None:
    """PIT mcap = ``close(asof) × shares_outstanding(≤ asof)``; ``None`` on failure.

    Shares come from ``Ticker.get_shares_full`` (SC-13D/G driven series); when
    that yields nothing, fall back to ``fast_info.shares`` (latest count, so
    mildly forward-biased on the shares axis but better than the live mcap which
    carries forward bias on BOTH price and shares).
    """
    try:
        import pandas as pd

        client = get_default_yfinance_client()
        asof_ts = pd.Timestamp(asof)
        # Pull a 7-day window so a Friday close covers a Saturday asof. The
        # client normalises to lowercase columns + a tz-naive index.
        hist = client.daily_ohlcv(
            ticker,
            start=(asof_ts - pd.Timedelta(days=7)).date(),
            end=(asof_ts + pd.Timedelta(days=1)).date(),
        )
        if hist.empty:
            return None
        hist = hist[hist.index <= asof_ts]
        if hist.empty:
            return None
        close = float(hist["close"].iloc[-1])

        shares = client.shares(ticker, asof=asof)
        if not shares:
            return None
        # Honour the "None on failure" contract: a NaN close or NaN shares makes
        # the product NaN, which must read as a failure, not a valid mcap.
        return _finite_or_none(close * shares)
    except Exception as exc:
        logger.warning("mcap PIT fetch failed for %s: %s", ticker, exc)
        return None


def _mcap_cache_load() -> dict:
    """Read the mcap cache (``{ticker: {mcap, ts}}``); ``{}`` if absent/corrupt."""
    if not _MCAP_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(_MCAP_CACHE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _mcap_cache_put(ticker: str, mcap: float, *, now: dt.datetime | None = None) -> None:
    """Persist ``ticker``'s live mcap with a UTC timestamp. Never raises."""
    now = now or dt.datetime.now(dt.UTC)
    try:
        cache = _mcap_cache_load()
        cache[ticker] = {"mcap": float(mcap), "ts": now.isoformat()}
        _MCAP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _MCAP_CACHE_PATH.parent / f"{_MCAP_CACHE_PATH.name}.tmp"
        # Path is the fixed, operator-owned `~/.alphalens` data root (the same
        # root every other cache uses) — `ticker` is a dict key, never part of
        # the path. The Sonar path-injection taint on `Path.home()` is a false
        # positive in this single-operator, non-web context.
        tmp.write_text(json.dumps(cache))  # NOSONAR
        os.replace(tmp, _MCAP_CACHE_PATH)  # NOSONAR
    except Exception:
        logger.exception("mcap cache write failed for %s; the fetch succeeded", ticker)


def _mcap_cache_get(ticker: str, *, now: dt.datetime | None = None) -> float | None:
    """Last-known mcap for ``ticker`` if ≤ ``_MCAP_CACHE_MAX_STALE_DAYS`` old."""
    now = now or dt.datetime.now(dt.UTC)
    try:
        entry = _mcap_cache_load().get(ticker)
        if not entry:
            return None
        ts = dt.datetime.fromisoformat(entry["ts"])
        if now - ts > dt.timedelta(days=_MCAP_CACHE_MAX_STALE_DAYS):
            return None
        # Neutralise a non-finite value at the read source — an entry written by
        # an OLD build (before the finiteness guard) could hold a NaN, and a
        # future direct reader must never be served NaN as a "recent" value.
        return _finite_or_none(float(entry["mcap"]))
    except (ValueError, KeyError, TypeError) as exc:
        # The ticker IS present (entry was truthy) but its record is malformed
        # — distinguish corruption / a manual-edit typo from a plain cache miss.
        logger.warning("mcap cache entry for %s is malformed (%s); ignoring", ticker, exc)
        return None


IN_BRACKET = "in_bracket"
TOO_SMALL = "too_small"
TOO_BIG = "too_big"
NO_MCAP = "no_mcap"
NOT_LISTED = "not_listed"


class McapVerdict(NamedTuple):
    """One proposal's bracket outcome, and the reason for it.

    ``market_cap`` is ``None`` exactly when ``verdict`` is :data:`NO_MCAP` or
    :data:`NOT_LISTED` — never ``0.0``, which would read as :data:`TOO_SMALL`
    in any downstream aggregate and silently turn a data outage into a fleet of
    tiny companies. :data:`NOT_LISTED` keeps :data:`NO_MCAP` meaning "priced
    but the fetch failed / out of data", not "no longer exists" (#1074).
    """

    ticker: str
    market_cap: float | None
    verdict: str


def _listing_status(ticker: str, asof: dt.date | None, root: Path | None) -> bool | None:
    """The deterministic listing pre-check (#1074), or ``None`` when disabled.

    ``root=None`` (the default everywhere below) disables the check entirely so
    library callers and tests never read the operator's home store; the
    production pipeline opts in by wiring the grouped-daily store root down from
    the CLI. The live path (``asof is None``) has no PIT session to check
    against and always falls through.
    """
    if root is None or asof is None:
        return None
    from alphalens_pipeline.data import rs_history

    return rs_history.was_listed_on_or_before(root, ticker, asof)


def classify_by_mcap(
    tickers: list[str],
    *,
    min_cap: int,
    max_cap: int,
    asof: dt.date | None = None,
    listing_root: Path | None = None,
) -> list[McapVerdict]:
    """Return one :class:`McapVerdict` per ticker, in input order.

    The bracket is the single largest sink in the candidate funnel — on
    2026-08-05 it dropped 17 of 19 proposals — and ``filter_by_mcap`` discards
    everything it rejects, so answering "were the dropped names bad, or merely
    the wrong size?" meant re-running the whole funnel offline. This function
    keeps the verdict and the fetched mcap so the question is answerable from
    disk, and so a mass drop can be told apart from a yfinance outage
    (:data:`TOO_BIG` everywhere versus :data:`NO_MCAP` everywhere).

    Pass ``asof`` for historical replay so the bracket is evaluated against PIT
    mcap rather than today's mcap. Both bounds are inclusive.

    Pass ``listing_root`` (the grouped-daily store root) to run the
    deterministic listing pre-check BEFORE the yfinance round-trip (#1074): the
    permissive stage-A proposer includes training-snapshot names that no longer
    trade, and each one costs three failing yfinance calls plus three ERROR
    journal lines before the bracket drops it as :data:`NO_MCAP`. A ticker
    absent from a fresh whole-market snapshot is :data:`NOT_LISTED` with no
    fetch; an unknown status (no store, stale snapshot, live path) falls
    through to the fetch unchanged.
    """
    verdicts: list[McapVerdict] = []
    for t in tickers:
        if _listing_status(t, asof, listing_root) is False:
            verdicts.append(McapVerdict(ticker=t, market_cap=None, verdict=NOT_LISTED))
            continue
        mc = fetch_mcap(t, asof=asof)
        if mc is None:
            verdict = NO_MCAP
        elif mc < min_cap:
            verdict = TOO_SMALL
        elif mc > max_cap:
            verdict = TOO_BIG
        else:
            verdict = IN_BRACKET
        verdicts.append(McapVerdict(ticker=t, market_cap=mc, verdict=verdict))
    return verdicts


def filter_by_mcap(
    tickers: list[str],
    *,
    min_cap: int,
    max_cap: int,
    asof: dt.date | None = None,
    listing_root: Path | None = None,
) -> dict[str, float]:
    """Return ``{ticker: mcap}`` for tickers whose mcap is in bracket.

    A pure projection of :func:`classify_by_mcap` — the gate's job is to enforce
    the bracket, and callers that only need the survivors should not have to
    filter verdicts themselves. Keeping it a projection (rather than a second
    implementation of the same comparison) is what stops the telemetry from ever
    disagreeing with what the pipeline actually keeps.

    Pass ``asof`` for historical replay so the bracket is evaluated against
    PIT mcap rather than today's mcap; ``listing_root`` as in
    :func:`classify_by_mcap`.
    """
    kept: dict[str, float] = {}
    for v in classify_by_mcap(
        tickers, min_cap=min_cap, max_cap=max_cap, asof=asof, listing_root=listing_root
    ):
        if v.verdict == IN_BRACKET and v.market_cap is not None:
            kept[v.ticker] = v.market_cap
    return kept


__all__ = [
    "IN_BRACKET",
    "NOT_LISTED",
    "NO_MCAP",
    "TOO_BIG",
    "TOO_SMALL",
    "McapVerdict",
    "classify_by_mcap",
    "fetch_mcap",
    "filter_by_mcap",
]
