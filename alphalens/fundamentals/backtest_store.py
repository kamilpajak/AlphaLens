"""Point-in-time fundamentals store for backtest replay.

Preloads raw AV bundles for every ticker once (113 tickers × 4 endpoints =
~452 API calls, one-shot). Then `features_as_of(ticker, date)` filters each
bundle's quarterly reports to exclude fiscal periods ending after the asof
date and extracts canonical features.

This is a cheaper alternative to caching per (ticker, date) — we pay the API
cost once, do per-date work in memory.
"""

from __future__ import annotations

import copy
import logging
from datetime import date
from typing import Callable, Mapping

from .fetcher import extract_features, fetch_ticker_bundle

logger = logging.getLogger(__name__)


BundleFetcher = Callable[[str], Mapping]


class HistoricalFundamentalsStore:
    def __init__(self, fetcher: BundleFetcher | None = None):
        """fetcher(ticker, curr_date=None) -> bundle. Defaults to fetch_ticker_bundle."""
        self._fetcher = fetcher or (lambda t, curr_date=None: fetch_ticker_bundle(t))
        self._bundles: dict[str, dict] = {}

    def preload(self, tickers: list[str]) -> None:
        """Fetch and cache bundles for every ticker. Individual failures skip."""
        for ticker in tickers:
            up = ticker.upper()
            if up in self._bundles:
                continue
            try:
                bundle = self._fetcher(up)
            except Exception as exc:  # noqa: BLE001
                logger.warning("preload fetch failed for %s: %s", up, exc)
                continue
            self._bundles[up] = bundle

    def features_as_of(self, ticker: str, asof: date) -> dict | None:
        """Return feature dict using only reports with fiscalDateEnding ≤ asof."""
        bundle = self._bundles.get(ticker.upper())
        if bundle is None:
            return None
        filtered = _filter_bundle_by_date(bundle, asof.isoformat())
        return extract_features(filtered)


def _filter_bundle_by_date(bundle: Mapping, asof_iso: str) -> dict:
    """Deep-copy-shallow: clone each section's quarterlyReports trimmed to ≤asof.

    OVERVIEW snapshot is used as-is since AV returns current values (no
    historical override available). This mirrors the forward-bias the upstream
    `_filter_reports_by_date` accepts — documented in issue #14.
    """
    out: dict = {"overview": dict(bundle.get("overview") or {})}
    for key in ("balance_sheet", "cash_flow", "income_statement"):
        section = bundle.get(key) or {}
        cloned = copy.deepcopy(dict(section))
        reports = cloned.get("quarterlyReports") or []
        cloned["quarterlyReports"] = [
            r for r in reports
            if (r.get("fiscalDateEnding") or "") <= asof_iso
        ]
        out[key] = cloned
    return out
