"""SEC EDGAR companyfacts -> PIT TTM ROE.

Reads ``~/.alphalens/companyfacts/{CIK}.json`` (raw SEC bulk dump) and exposes
``roe_ttm(ticker, asof) -> float | None`` matching
``scripts.experiment_quality_momentum_combo.FundamentalsROEStore``.

Design decisions (validated 2026-04-29 vs Gemini 3 Pro peer review):

  1. Matched-pair concept hierarchy. Parent ROE uses parent attribution on
     both numerator and denominator: ``NetIncomeLoss`` /
     ``StockholdersEquity``. Consolidated fallback uses ``ProfitLoss`` /
     ``StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest``.
     Never mix parent NI with consolidated equity.

  2. PIT correctness via ``filed`` date. For each (start, end) period (or each
     ``end`` for instant concepts), keep the entry with the latest filed date
     <= asof. Restatements after asof are ignored.

  3. TTM = current_YTD + prior_FY - prior_YTD (Compustat / WorldQuant
     convention). Avoids the fragility of stitching four single-quarter
     deltas where intra-year restatements distort the deduced quarter.

  4. End-date matching. ROE is computed at the latest fiscal period end that
     has BOTH a NI YTD/FY entry AND an Equity instant entry. Prevents pairing
     a preliminary 8-K NI with stale prior-quarter Equity.

  5. Common-equity adjustment when preferred capital is present:
     numerator -> ``NetIncomeLossAvailableToCommonStockholdersBasic`` (which
     SEC reports only when preferred dividends are non-trivial; otherwise
     identical to NetIncomeLoss), denominator -> Equity - PreferredStockValue.

  6. Fiscal-year drift tolerance. Companies' FY end dates can drift by a few
     days year-on-year (52/53-week calendars). Prior-period lookups search a
     +/- 30-day window around the naive year-shifted target.

  7. Negative or zero equity -> ``None`` (standard FF / Asness QMJ practice).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from alphalens.alt_data.ticker_cik_map import TickerCikMap

logger = logging.getLogger(__name__)


def _evict_to_capacity(cache: dict, max_size: int) -> int:
    """FIFO eviction: drop oldest entries until ``len(cache) <= max_size``.

    Duplicate of the same helper in ``alphalens.alt_data.sec_edgar_client``;
    kept independent to avoid coupling ``alphalens.fundamentals`` to
    ``alphalens.alt_data``. Returns the number of entries actually evicted
    so the caller can warn on the anomaly.
    """
    max_size = max(max_size, 0)
    evicted = 0
    while len(cache) > max_size:
        cache.pop(next(iter(cache)))
        evicted += 1
    return evicted


# --- Concept names (US-GAAP taxonomy) ---------------------------------------

_NI_PARENT = "NetIncomeLoss"
_EQ_PARENT = "StockholdersEquity"

_NI_CONSOLIDATED = "ProfitLoss"
_EQ_CONSOLIDATED = "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"

_NI_COMMON = "NetIncomeLossAvailableToCommonStockholdersBasic"
_PREFERRED_VALUE = "PreferredStockValue"


# --- Internal entry record ---------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    """Normalised companyfacts USD entry. Strings stay ISO so lex order ==
    chronological order, dodging unnecessary parsing in hot paths."""

    end: str  # always present (period end for duration, instant date for balance)
    val: float
    filed: str  # ISO date the SEC accepted the filing
    form: str  # 10-K / 10-Q / 10-K/A / 10-Q/A / 8-K / ...
    fp: str | None  # FY / Q1 / Q2 / Q3 / None
    start: str | None  # only set for duration concepts


def _parse_entries(units_block: dict) -> list[_Entry]:
    out: list[_Entry] = []
    for raw in units_block.get("USD", []) or []:
        end = raw.get("end")
        filed = raw.get("filed")
        if not end or not filed or "val" not in raw:
            continue
        try:
            val = float(raw["val"])
        except (TypeError, ValueError):
            continue
        out.append(
            _Entry(
                end=end,
                val=val,
                filed=filed,
                form=raw.get("form", ""),
                fp=raw.get("fp"),
                start=raw.get("start"),
            )
        )
    return out


# --- PIT helpers -------------------------------------------------------------


def _pit_filter(entries: Sequence[_Entry], asof: date) -> list[_Entry]:
    asof_str = asof.isoformat()
    return [e for e in entries if e.filed <= asof_str]


def _latest_per_period(entries: Sequence[_Entry]) -> dict[tuple[str | None, str], _Entry]:
    """Per (start, end) keep the entry with latest filed date.

    Restatements / amendments supersede originals when filed later (and still
    visible at asof, since callers PIT-filter first).
    """
    out: dict[tuple[str | None, str], _Entry] = {}
    for e in entries:
        key = (e.start, e.end)
        cur = out.get(key)
        if cur is None or e.filed > cur.filed:
            out[key] = e
    return out


def _latest_per_end(entries: Sequence[_Entry]) -> dict[str, _Entry]:
    """For instant concepts (Equity, PreferredStockValue) only ``end`` matters."""
    out: dict[str, _Entry] = {}
    for e in entries:
        cur = out.get(e.end)
        if cur is None or e.filed > cur.filed:
            out[e.end] = e
    return out


# --- Period maths ------------------------------------------------------------


def _months_in_period(entry: _Entry) -> int:
    if not entry.start or not entry.end:
        return 0
    try:
        s = date.fromisoformat(entry.start)
        e = date.fromisoformat(entry.end)
    except ValueError:
        return 0
    return max(0, (e - s).days // 30)


def _is_fy_like(entry: _Entry) -> bool:
    """FY entries are usually fp='FY' but 8-K supplements may have fp=None
    spanning ~12 months. Trust either."""
    if entry.fp == "FY":
        return True
    return _months_in_period(entry) >= 11


def _shift_year_iso(date_str: str, years: int) -> date:
    d = date.fromisoformat(date_str)
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        # Feb 29 -> Feb 28 in non-leap target year.
        return d.replace(year=d.year + years, day=28)


def _find_near_end(
    entries: Sequence[_Entry],
    target: date,
    *,
    window_days: int = 30,
) -> _Entry | None:
    """Pick the entry whose `end` is closest to `target` within +/- window.

    Tolerates 52/53-week fiscal calendars where FY end drifts by a few days
    year-on-year (e.g. AAPL Sep 26 / Sep 25 / Sep 24).
    """
    best: _Entry | None = None
    best_delta: int | None = None
    for e in entries:
        try:
            d = date.fromisoformat(e.end)
        except ValueError:
            continue
        delta = abs((d - target).days)
        if delta > window_days:
            continue
        if best_delta is None or delta < best_delta:
            best = e
            best_delta = delta
    return best


def _ttm_net_income(
    ni_per_period: dict[tuple[str | None, str], _Entry],
    *,
    end_date: str,
) -> float | None:
    """Compute TTM NI at end_date using `current YTD + prior FY - prior YTD`.

    Returns None if the prior FY or prior YTD is missing -- never extrapolates
    or annualises.
    """
    current: _Entry | None = None
    for (_, end), entry in ni_per_period.items():
        if end == end_date:
            current = entry
            break
    if current is None:
        return None
    if _is_fy_like(current):
        return current.val
    # Q1/Q2/Q3 cumulative -> need prior FY (the most recent FY whose end falls
    # before this YTD's start, i.e. the fiscal year just closed) and prior
    # YTD with same fp ending ~12 months before current end.
    if not current.start:
        return None
    prior_fy_candidates = [
        e for e in ni_per_period.values() if _is_fy_like(e) and e.end < current.start
    ]
    if not prior_fy_candidates:
        return None
    prior_fy = max(prior_fy_candidates, key=lambda e: e.end)
    target = _shift_year_iso(current.end, -1)
    same_fp_candidates = [
        e
        for e in ni_per_period.values()
        if e.fp == current.fp and not _is_fy_like(e) and e.end != current.end
    ]
    prior_ytd = _find_near_end(same_fp_candidates, target)
    if prior_ytd is None:
        return None
    return current.val + prior_fy.val - prior_ytd.val


# --- Public store ------------------------------------------------------------


class EdgarCompanyfactsROEStore:
    """PIT TTM-ROE store backed by SEC EDGAR companyfacts JSON.

    Lazy: only loads (and caches) the JSON for tickers actually queried.

    >>> store = EdgarCompanyfactsROEStore(cf_dir, cik_map)
    >>> store.roe_ttm("AAPL", date(2024, 1, 31))  # ~1.56
    """

    def __init__(
        self,
        companyfacts_dir: Path,
        ticker_cik_map: TickerCikMap,
    ):
        self._dir = Path(companyfacts_dir)
        self._cik_map = ticker_cik_map
        self._facts_cache: dict[str, dict | None] = {}
        # Working set on a typical R2000 backtest is ~1200 unique CIKs ×
        # ~3MB JSON ≈ 3.6GB. Capacity 1500 (25% margin) caps RAM around
        # 4.5GB while leaving room for legitimate cross-period universe
        # expansion. Override per instance for stress tests.
        self._facts_cache_capacity = 1500

    # ---- top-level ----

    def roe_ttm(self, ticker: str, asof: date) -> float | None:
        resolved = self._resolve_components(ticker, asof)
        if resolved is None:
            return None
        gaap, ttm_ni, equity, target_end = resolved
        ttm_ni, equity = self._apply_common_equity_adjustment(
            gaap, ttm_ni, equity, target_end, asof
        )
        if equity <= 0:
            return None
        return ttm_ni / equity

    # ---- helpers ----

    def _resolve_components(self, ticker: str, asof: date) -> tuple[dict, float, float, str] | None:
        """Look up CIK + matched pair + matched period end + TTM NI + equity.

        Returns ``(gaap, ttm_ni, equity, target_end)`` or ``None`` if any of
        the resolution steps fails — this collapses the seven-way None return
        in ``roe_ttm`` into a single guard.
        """
        cik = self._cik_map.lookup(ticker)
        if cik is None:
            return None
        gaap = self._load_gaap(cik)
        if gaap is None:
            return None
        pair = self._select_pair(gaap)
        if pair is None:
            return None
        ni_concept, eq_concept = pair
        ni_entries = _pit_filter(_parse_entries(gaap[ni_concept]["units"]), asof)
        eq_entries = _pit_filter(_parse_entries(gaap[eq_concept]["units"]), asof)
        if not ni_entries or not eq_entries:
            return None
        ni_per_period = _latest_per_period(ni_entries)
        eq_per_end = _latest_per_end(eq_entries)
        ni_ends = {end for (_, end), entry in ni_per_period.items() if entry.start}
        common_ends = sorted(ni_ends & eq_per_end.keys())
        if not common_ends:
            return None
        target_end = common_ends[-1]
        ttm_ni = _ttm_net_income(ni_per_period, end_date=target_end)
        if ttm_ni is None:
            return None
        return gaap, ttm_ni, eq_per_end[target_end].val, target_end

    @staticmethod
    def _apply_common_equity_adjustment(
        gaap: dict, ttm_ni: float, equity: float, target_end: str, asof: date
    ) -> tuple[float, float]:
        """Subtract preferred dividends from numerator and preferred capital
        from denominator when the SEC tags those concepts. When absent (most
        non-preferred-issuing firms) the inputs pass through unchanged.

        Matched-pair invariant: if `_NI_COMMON` is reported but cannot be
        resolved at `target_end` (e.g., the tag has only Q1 entries while
        target_end is FY), we must NOT subtract preferred from equity —
        doing so would pair parent NI (which still includes preferred
        dividends) with common equity. Track resolution state explicitly.
        """
        ni_common_block = gaap.get(_NI_COMMON)
        common_resolved = False
        if ni_common_block is not None:
            ni_common_entries = _pit_filter(_parse_entries(ni_common_block["units"]), asof)
            ni_common_per_period = _latest_per_period(ni_common_entries)
            common_ttm = _ttm_net_income(ni_common_per_period, end_date=target_end)
            if common_ttm is not None:
                ttm_ni = common_ttm
                common_resolved = True
        preferred_block = gaap.get(_PREFERRED_VALUE)
        # Subtract preferred only when the firm doesn't tag preferred dividends
        # (so parent NI already represents common shareholders) OR when we
        # successfully replaced parent NI with common NI. Otherwise keep
        # parent NI / parent equity to preserve the matched-pair invariant.
        if preferred_block is not None and (ni_common_block is None or common_resolved):
            pref_entries = _pit_filter(_parse_entries(preferred_block["units"]), asof)
            pref_per_end = _latest_per_end(pref_entries)
            pref_at_end = pref_per_end.get(target_end)
            if pref_at_end is not None:
                equity = equity - pref_at_end.val
        return ttm_ni, equity

    def _load_gaap(self, cik: str) -> dict | None:
        cached = self._facts_cache.get(cik)
        if cached is not None:
            return cached
        path = self._dir / f"{cik}.json"
        if not path.exists():
            self._store_in_cache(cik, None)
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            logger.warning("companyfacts unreadable: %s", path)
            self._store_in_cache(cik, None)
            return None
        gaap = payload.get("facts", {}).get("us-gaap")
        self._store_in_cache(cik, gaap)
        return gaap

    def _store_in_cache(self, cik: str, gaap: dict | None) -> None:
        evicted = _evict_to_capacity(self._facts_cache, self._facts_cache_capacity - 1)
        if evicted > 0:
            logger.warning(
                "_facts_cache evicted %d entries (capacity=%d); working-set "
                "assumption violated — investigate universe size or raise capacity",
                evicted,
                self._facts_cache_capacity,
            )
        self._facts_cache[cik] = gaap

    @staticmethod
    def _select_pair(gaap: dict) -> tuple[str, str] | None:
        if _NI_PARENT in gaap and _EQ_PARENT in gaap:
            return _NI_PARENT, _EQ_PARENT
        if _NI_CONSOLIDATED in gaap and _EQ_CONSOLIDATED in gaap:
            return _NI_CONSOLIDATED, _EQ_CONSOLIDATED
        return None
