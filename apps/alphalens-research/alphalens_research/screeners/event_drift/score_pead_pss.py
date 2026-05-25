"""Price-scaled earnings surprise (PSS) rank scorer — paradigm-14 PEAD v2 B1.

At each snapshot date ``asof``, the scorer:

  1. Collects every announcement in the trailing 45-calendar-day cohort
     (``asof - cohort_window_days < reported_date <= asof``). PIT-strict:
     events with ``reported_date > asof`` are excluded.
  2. Computes ``PSS = (reported_eps - estimated_eps) / close(reported_date - 1)``
     for each surviving event. Tickers with multiple events in the cohort
     keep only the most recent one (multi-event overlap is rare).
  3. Applies eligibility filters:
     - ``close(reported_date - 1) >= $5`` (drop penny-stock noise)
     - ``|PSS| < 0.20`` (drop reporting errors / special situations)
  4. Cross-sectionally ranks remaining tickers by PSS and returns a
     percentile rank in [0, 100].

The returned DataFrame is the per-asof signal cross-section. B2's daily-
rebalance adapter (Option α, sub-leveraged α2 with ``N_FIXED=150`` per
``docs/research/paradigm14_pead_cost_model_audit_2026_05_14.md``) takes the
percentile ranks and produces ticker weights for ``BacktestReport`` ingestion.

Stores are injected as callables so tests stay network-free:

  - ``earnings_loader(ticker) -> list[AVEarningsAnnouncement]``
  - ``close_lookup(ticker, date) -> float | None``

Use ``alphalens_research.screeners.event_drift.av_earnings_ingestion.load_av_earnings``
as the production ``earnings_loader``; price store wiring is B2's concern.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, timedelta

import pandas as pd

from alphalens_research.screeners.event_drift.av_earnings_ingestion import AVEarningsAnnouncement

EarningsLoader = Callable[[str], list[AVEarningsAnnouncement]]
# ``CloseLookup(ticker, target_date) -> float | None`` MUST implement
# "last trading day on or before target_date" semantics. Callers MUST NOT
# return None for weekends/holidays — that would silently drop every
# Monday announcement when the scorer queries ``reported_date - 1
# calendar day`` for the PSS denominator. Returning None is reserved
# for genuinely missing price data (delisted ticker, pre-IPO date).
CloseLookup = Callable[[str, date], float | None]

# Per-paradigm-14 pre-reg `pead_v5_pss_2026_05_13.params_frozen.eligibility_filters`.
_DEFAULT_CLOSE_FLOOR = 5.0
_DEFAULT_ABS_PSS_CAP = 0.20

_OUTPUT_COLUMNS = ("ticker", "period_end", "reported_date", "report_time", "pss", "percentile_rank")


def _latest_event_in_cohort(
    events: Iterable[AVEarningsAnnouncement],
    *,
    asof: date,
    cohort_lower_exclusive: date,
) -> AVEarningsAnnouncement | None:
    """Pick the most recent event with ``reported_date`` in the cohort window.

    Returns None if no event qualifies. Tracks the latest match explicitly
    rather than trusting input sort order — a misbehaving loader cannot
    break PIT semantics. O(n) per call; for full-audit scale (~500 tickers
    × ~3000 asofs × ~100 events/ticker) the comparison count is ~1.5e8,
    still well under audit wall-time budget. If profiling later shows this
    as a bottleneck, B2 can pre-sort each ticker's events once and switch
    to reverse-scan early-return here.
    """
    latest: AVEarningsAnnouncement | None = None
    for e in events:
        if e.reported_date <= cohort_lower_exclusive or e.reported_date > asof:
            continue
        if latest is None or e.reported_date > latest.reported_date:
            latest = e
    return latest


def pss_rank(
    *,
    asof: date,
    universe: list[str],
    earnings_loader: EarningsLoader,
    close_lookup: CloseLookup,
    cohort_window_days: int = 45,
    close_floor: float = _DEFAULT_CLOSE_FLOOR,
    abs_pss_cap: float = _DEFAULT_ABS_PSS_CAP,
) -> pd.DataFrame:
    """Cross-sectional PSS rank at ``asof``.

    Returns a DataFrame with columns ``(ticker, period_end, reported_date,
    report_time, pss, percentile_rank)``. ``percentile_rank`` is in ``[0, 100]``
    with 100 representing the strongest positive surprise in the cohort.
    Empty universe / empty cohort returns a well-formed empty DataFrame.
    """
    cohort_lower_exclusive = asof - timedelta(days=cohort_window_days)

    rows: list[dict] = []
    for ticker in universe:
        events = earnings_loader(ticker)
        latest = _latest_event_in_cohort(
            events, asof=asof, cohort_lower_exclusive=cohort_lower_exclusive
        )
        if latest is None:
            continue

        prior_close = close_lookup(ticker, latest.reported_date - timedelta(days=1))
        if prior_close is None or prior_close < close_floor:
            continue

        pss = (latest.reported_eps - latest.estimated_eps) / prior_close
        if abs(pss) >= abs_pss_cap:
            continue

        rows.append(
            {
                "ticker": latest.ticker,
                "period_end": latest.period_end,
                "reported_date": latest.reported_date,
                "report_time": latest.report_time,
                "pss": float(pss),
                "percentile_rank": float("nan"),  # filled below
            }
        )

    if not rows:
        # Explicit column-to-dtype mapping (vs substring-match heuristic) so
        # downstream type checks survive future column renames/additions.
        empty_dtypes = {"pss": float, "percentile_rank": float}
        return pd.DataFrame(
            {c: pd.Series(dtype=empty_dtypes.get(c, "object")) for c in _OUTPUT_COLUMNS}
        )

    df = pd.DataFrame(rows, columns=list(_OUTPUT_COLUMNS))
    # `rank(pct=True)` returns [0, 1]; scale to [0, 100] for downstream legibility.
    df["percentile_rank"] = df["pss"].rank(pct=True, method="average") * 100.0
    return df
