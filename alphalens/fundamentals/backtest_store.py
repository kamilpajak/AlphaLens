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

    # Raise if more than this fraction of tickers fail to preload — silent
    # degradation would make Phase 2 backtest falsely report "gate has no
    # effect" when in reality the gate never ran on any ticker.
    PRELOAD_FAILURE_THRESHOLD = 0.15

    def preload(self, tickers: list[str]) -> None:
        """Fetch and cache bundles for every ticker. Individual failures skip,
        but aborts if more than PRELOAD_FAILURE_THRESHOLD fraction fail."""
        failures = 0
        attempted = 0
        for ticker in tickers:
            up = ticker.upper()
            if up in self._bundles:
                continue
            attempted += 1
            try:
                bundle = self._fetcher(up)
            except Exception as exc:  # noqa: BLE001
                logger.warning("preload fetch failed for %s: %s", up, exc)
                failures += 1
                continue
            self._bundles[up] = bundle
        loaded = attempted - failures
        logger.info(
            "HistoricalFundamentalsStore preload: %d/%d tickers loaded (failures=%d)",
            loaded, attempted, failures,
        )
        if attempted > 0 and failures / attempted > self.PRELOAD_FAILURE_THRESHOLD:
            raise RuntimeError(
                f"Fundamental preload failed for {failures}/{attempted} tickers "
                f"(> {self.PRELOAD_FAILURE_THRESHOLD * 100:.0f}% threshold). "
                "Backtest with gate would measure gate-inactive runs — aborting."
            )

    def features_as_of(self, ticker: str, asof: date) -> dict | None:
        """Return feature dict using only reports with fiscalDateEnding ≤ asof."""
        bundle = self._bundles.get(ticker.upper())
        if bundle is None:
            return None
        filtered = _filter_bundle_by_date(bundle, asof.isoformat())
        return extract_features(filtered)


def _filter_bundle_by_date(bundle: Mapping, asof_iso: str) -> dict:
    """Clone each quarterly section's reports trimmed to fiscalDateEnding ≤ asof.

    OVERVIEW is stripped of forward-looking TTM values (PriceToSalesRatioTTM,
    NetIncomeTTM) — those are always current, so using them in a backtest
    leaks future information. `extract_features` gracefully falls back to
    summing trimmed quarterly income reports for net_income_ttm; ps_ratio
    then returns None and the P/S penalty becomes a no-op for backtest.
    Documented PIT compromise per issue #14 / CR (Phase 1.5).
    """
    overview = dict(bundle.get("overview") or {})
    for forward_key in ("PriceToSalesRatioTTM", "NetIncomeTTM"):
        overview.pop(forward_key, None)
    out: dict = {"overview": overview}
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
