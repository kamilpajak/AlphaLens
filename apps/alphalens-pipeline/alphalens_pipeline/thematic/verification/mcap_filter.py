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
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Persistent last-known live mcap, so a TRANSIENT yfinance/Yahoo outage does not
# silently drop every candidate (and collapse the brief to zero). On the live
# path we cache each success and fall back to a recent cached value on failure.
# mcap is slow-moving, so a value up to two weeks stale is a fine approximation
# for a wide market-cap BRACKET filter — far better than dropping the candidate.
_MCAP_CACHE_PATH = Path.home() / ".alphalens" / "mcap_cache.json"
_MCAP_CACHE_MAX_STALE_DAYS = 14


def fetch_mcap(ticker: str, *, asof: dt.date | None = None) -> float | None:
    """Fetch market cap for ``ticker``.

    Returns ``None`` on an unrecoverable failure so callers can drop the
    candidate rather than crash the batch.

    ``asof`` selects between the live and PIT paths:
    - ``None`` or today/future → ``fast_info.market_cap``, with a persistent
      cache fallback (a transient yfinance failure returns the last-known value
      if it is ≤ ``_MCAP_CACHE_MAX_STALE_DAYS`` old).
    - past date → ``close(asof) × shares_outstanding(≤ asof)``; NO cache (PIT
      mcap is per-date, the live cache would inject forward bias).
    """
    if asof is None or asof >= dt.date.today():
        return _fetch_live_mcap_with_cache(ticker)
    return _fetch_pit_mcap(ticker, asof)


def _fetch_live_mcap(ticker: str) -> float | None:
    """One live ``fast_info.market_cap`` lookup; ``None`` on any failure."""
    try:
        import yfinance as yf

        # FastInfo dict-style `.get("market_cap")` returns None because the dict
        # keys differ from the attribute names; attribute access is the contract.
        mc = yf.Ticker(ticker).fast_info.market_cap
        return float(mc) if mc is not None else None
    except Exception as exc:
        logger.warning("mcap live fetch failed for %s: %s", ticker, exc)
        return None


def _fetch_live_mcap_with_cache(ticker: str) -> float | None:
    """Live mcap with a persistent fallback for transient yfinance failures."""
    mc = _fetch_live_mcap(ticker)
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
        import yfinance as yf

        tk = yf.Ticker(ticker)
        asof_ts = pd.Timestamp(asof)
        # Pull a 7-day window so a Friday close covers a Saturday asof.
        hist = tk.history(
            start=(asof_ts - pd.Timedelta(days=7)).date(),
            end=(asof_ts + pd.Timedelta(days=1)).date(),
            auto_adjust=False,
        )
        if hist is None or hist.empty:
            return None
        hist.index = pd.to_datetime(hist.index).tz_localize(None)
        hist = hist[hist.index <= asof_ts]
        if hist.empty:
            return None
        close = float(hist["Close"].iloc[-1])

        shares = _pit_shares(tk, asof_ts)
        if not shares:
            return None
        return close * shares
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
        tmp.write_text(json.dumps(cache))
        os.replace(tmp, _MCAP_CACHE_PATH)
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
        return float(entry["mcap"])
    except (ValueError, KeyError, TypeError) as exc:
        # The ticker IS present (entry was truthy) but its record is malformed
        # — distinguish corruption / a manual-edit typo from a plain cache miss.
        logger.warning("mcap cache entry for %s is malformed (%s); ignoring", ticker, exc)
        return None


def _pit_shares(tk, asof_ts):
    """Latest shares_outstanding update on or before ``asof_ts``.

    Returns ``float`` or ``None``. Tries ``get_shares_full`` first (the
    only yfinance API that exposes a dated series); falls back to
    ``fast_info.shares`` when the series is empty.
    """
    import pandas as pd

    try:
        series = tk.get_shares_full(
            start=(asof_ts - pd.Timedelta(days=400)).date().isoformat(),
            end=(asof_ts + pd.Timedelta(days=1)).date().isoformat(),
        )
    except Exception:
        series = None
    if series is not None and len(series) > 0:
        series.index = pd.to_datetime(series.index).tz_localize(None)
        pit = series[series.index <= asof_ts]
        if not pit.empty:
            return float(pit.iloc[-1])
    # No PIT shares series → fall back to fast_info.shares.
    fallback = getattr(tk.fast_info, "shares", None)
    return float(fallback) if fallback else None


def filter_by_mcap(
    tickers: list[str],
    *,
    min_cap: int,
    max_cap: int,
    asof: dt.date | None = None,
) -> dict[str, float]:
    """Return ``{ticker: mcap}`` for tickers whose mcap is in bracket.

    Tickers with mcap below ``min_cap``, above ``max_cap``, or unavailable
    are silently dropped — the gate's job is to enforce the bracket, not
    signal why a candidate was excluded.

    Pass ``asof`` for historical replay so the bracket is evaluated against
    PIT mcap rather than today's mcap.
    """
    kept: dict[str, float] = {}
    for t in tickers:
        mc = fetch_mcap(t, asof=asof)
        if mc is None:
            continue
        if min_cap <= mc <= max_cap:
            kept[t] = mc
    return kept


__all__ = ["fetch_mcap", "filter_by_mcap"]
