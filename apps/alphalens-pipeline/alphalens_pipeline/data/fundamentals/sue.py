"""Foster (1977) SUE with first-filed PIT snapshots.

Standardised Unexpected Earnings (SUE) per Foster (1977 The Accounting Review).
Construction:

  forecast(Q_t)  = Q_{t-4} + drift                    (seasonal random walk + drift)
  drift          = mean( Q_{t-i} - Q_{t-i-4} ) over 4 prior seasonal pairs
  surprise(Q_t)  = actual(Q_t) - forecast(Q_t)
  SUE(Q_t)       = surprise(Q_t) / std( surprises(Q_{t-1}, ..., Q_{t-W}) )

PIT contract (per perplexity adversarial review on v4 v2 pre-reg):

  For each historical quarter q used in residual-std computation, the EPS value
  is the FIRST-FILED entry (earliest `filed` date among all entries for that
  period_end), NOT the latest-restated value. This implements Foster's original
  construction where surprises measure what the market reacted to at each
  historical filing.

  The "first-filed" entry for a given period_end p is fixed regardless of the
  asof at which we query (it is literally the entry with the earliest `filed`
  date), but visibility depends on asof: at asof t, only entries with
  `filed <= t` are considered. So a late-arriving original filing is invisible
  until its filed date passes, and amendments are NEVER substituted in.

This module is consumed by `alphalens_research/screeners/alt_data/features.py` to
populate the v4 v2 pre-registered feature `earnings_sue_naive_4q_decayed`.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Sequence
from datetime import date

import pyarrow as pa

from alphalens_pipeline.data.alt_data.ticker_cik_map import TickerCikMap
from alphalens_pipeline.data.fundamentals.companyfacts_parquet import (
    CompanyfactsParquetReader,
    filter_concept,
)
from alphalens_pipeline.data.fundamentals.edgar_companyfacts import (
    _Entry,
    _pit_filter,
)

logger = logging.getLogger(__name__)


_EPS_BASIC = "EarningsPerShareBasic"
_EPS_DILUTED = "EarningsPerShareDiluted"


# ---------------------------------------------------------------------------
# First-filed PIT helpers


def _first_filed_per_period_end(entries: Sequence[_Entry]) -> dict[str, _Entry]:
    """For each ``end`` date keep the entry with the earliest ``filed``.

    This is the inverse of `edgar_companyfacts._latest_per_period`, which keeps
    the latest filing (canonical PIT for level-of-state). For Foster SUE we
    need the first filing because surprises are defined against the EPS that
    was actually known at each historical announcement.
    """
    out: dict[str, _Entry] = {}
    for e in entries:
        cur = out.get(e.end)
        if cur is None or e.filed < cur.filed:
            out[e.end] = e
    return out


# ---------------------------------------------------------------------------
# Foster naive forecast


def _foster_naive_forecast(history: Sequence[float]) -> float | None:
    """Forecast Q_t given history = [Q_{t-N}, ..., Q_{t-1}] (oldest first).

    Forecast = Q_{t-4} + mean drift over 4 seasonal pairs:
      drift = mean( Q_{t-i} - Q_{t-i-4} ) for i in 1..4.

    Requires at least 8 prior quarters. Returns None if insufficient.
    """
    if len(history) < 8:
        return None
    # Seasonal pairs (i=1..4): pair_i = history[-i] - history[-i-4]
    drifts = [history[-i] - history[-i - 4] for i in range(1, 5)]
    drift = sum(drifts) / 4.0
    return history[-4] + drift


def _compute_sue(eps_series: Sequence[float], *, residual_window: int = 4) -> float | None:
    """Compute SUE for the LAST quarter in `eps_series` using prior history.

    Returns None when:
      - fewer than 8 prior quarters available for current forecast, OR
      - residual std cannot be computed (need `residual_window` prior surprises,
        each requiring 8-quarter history => need 8 + residual_window quarters
        before the current one), OR
      - residual std is zero (constant series).
    """
    n = len(eps_series)
    # Need: 8 prior for forecast(current), and residual_window prior surprises
    # each requiring 8 prior quarters. Minimum: 8 + residual_window + 1 (current).
    if n < 8 + residual_window + 1:
        return None
    actual_current = eps_series[-1]
    history_current = eps_series[:-1]
    forecast_current = _foster_naive_forecast(history_current)
    if forecast_current is None:
        return None
    surprise_current = actual_current - forecast_current

    # Compute residuals over the residual_window quarters preceding current.
    # For each q in [n-1-residual_window, ..., n-2], the forecast at q uses
    # eps_series[:q] and the actual is eps_series[q].
    residuals: list[float] = []
    for q in range(n - 1 - residual_window, n - 1):
        prior = eps_series[:q]
        f = _foster_naive_forecast(prior)
        if f is None:
            continue
        residuals.append(eps_series[q] - f)
    if len(residuals) < 2:
        return None
    sigma = statistics.stdev(residuals)
    if sigma <= 0.0:
        return None
    return surprise_current / sigma


# ---------------------------------------------------------------------------
# Store


class FosterSUEStore:
    """PIT Foster-SUE store backed by parquet companyfacts via reader injection.

    >>> reader = CompanyfactsParquetReader(parquet_dir)
    >>> store = FosterSUEStore(reader, cik_map)
    >>> store.sue("AAPL", date(2024, 6, 30))  # ~ +1.2 if positive surprise
    """

    def __init__(
        self,
        reader: CompanyfactsParquetReader,
        ticker_cik_map: TickerCikMap,
        *,
        residual_window: int = 4,
    ):
        self._reader = reader
        self._cik_map = ticker_cik_map
        self._residual_window = residual_window

    def sue(self, ticker: str, asof: date) -> float | None:
        """Foster SUE for the most recent first-filed quarter <= asof."""
        eps_series = self.eps_series_first_filed(ticker, asof)
        if eps_series is None:
            return None
        return _compute_sue(eps_series, residual_window=self._residual_window)

    def eps_series_first_filed(self, ticker: str, asof: date) -> list[float] | None:
        """Quarterly EPS series in chronological order using first-filed values.

        Returns None when the ticker isn't mapped or no EPS concept is present
        in the parquet table. Empty list when no quarterly entries are visible
        at asof.
        """
        cik = self._cik_map.lookup(ticker)
        if cik is None:
            return None
        table = self._reader.get_cik_table(cik)
        if table is None:
            return None

        # Prefer EPS Basic (more universally tagged); fall back to Diluted.
        entries = _eps_entries_from_table(table, _EPS_BASIC)
        if not entries:
            entries = _eps_entries_from_table(table, _EPS_DILUTED)
        if not entries:
            return None

        visible = _pit_filter(entries, asof)
        quarterly = [e for e in visible if _is_quarterly(e)]
        first_filed = _first_filed_per_period_end(quarterly)
        ordered_ends = sorted(first_filed.keys())
        return [first_filed[end].val for end in ordered_ends]


# ---------------------------------------------------------------------------
# Arrow Table -> _Entry adapter (preserves existing PIT helper contract).


def _eps_entries_from_table(table: pa.Table, concept: str) -> list[_Entry]:
    """Convert filtered Arrow rows for one EPS concept into _Entry list.

    Preference order on units mirrors the legacy JSON path (USD/shares before
    USD); EPS Basic is rarely tagged in the alt USD form but defensive
    fallback is preserved for symmetry with the previous parser.
    """
    for unit_key in ("USD/shares", "USD"):
        filtered = filter_concept(table, "us-gaap", concept, unit=unit_key)
        if filtered.num_rows == 0:
            continue
        return [
            _Entry(
                end=row["period_end"].isoformat(),
                val=float(row["val"]),
                filed=row["filed_date"].isoformat(),
                form=row["form"] or "",
                fp=row["fp"],
                start=row["period_start"].isoformat() if row["period_start"] is not None else None,
            )
            for row in filtered.to_pylist()
        ]
    return []


def _is_quarterly(entry: _Entry) -> bool:
    """True if entry represents a single fiscal quarter (Q1/Q2/Q3/Q4 or ~3mo span)."""
    if entry.fp in {"Q1", "Q2", "Q3", "Q4"}:
        return True
    if entry.start and entry.end:
        try:
            s = date.fromisoformat(entry.start)
            e = date.fromisoformat(entry.end)
        except ValueError:
            return False
        days = (e - s).days
        return 80 <= days <= 100  # ~3 months window
    return False
