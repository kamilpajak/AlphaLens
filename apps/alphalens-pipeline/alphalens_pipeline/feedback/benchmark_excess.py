"""Benchmark-excess enrichment for the population-ladder parquet store.

Why this lives in the pipeline (NOT in the Django ingest)
---------------------------------------------------------
The market-behavior edge dashboard headline metric is the **benchmark-relative**
move (memo §3.1): ``market_excess_return = forward_return − benchmark_window_return``.
Both legs must be RAW close-to-close returns over the SAME arrival→exit window so
the subtraction is dimensionally correct (the candidate's ``realized_r`` is
risk-normalised and is NOT comparable to a raw index return — see the memo §3.1
and the discovery's "CRITICAL UNIT RESOLUTION").

The benchmark leg needs a market-index price fetch (Polygon) + the exchange
calendar to window it. Those both live in ``alphalens_pipeline``. The Django
``rebuild_*_cache`` commands run in the SLIM Django image, which deliberately
does NOT install ``alphalens_pipeline`` (the prod incident 2026-06-01: a
top-level ``alphalens_pipeline`` import broke ``collectstatic`` / the image
build). So the benchmark return is computed HERE, in the pipeline container that
already has Polygon + the calendar, and written onto the population-ladder
parquet as two extra columns. Django ingest then just READS them (and stores
``None`` for any older parquet that predates the columns, exactly as the briefs
ingest tolerates missing columns).

What it computes
----------------
For every row that carries a non-null ``forward_return`` and a recoverable
``[arrival_session, exit_session]`` window:

* ``benchmark_window_return`` — the market index (SPY) raw close-to-close return
  over the SAME window, computed with the SAME arrival-window-VWAP reference
  anchor and horizon-end last-close as ``forward_return`` (see
  :func:`alphalens_pipeline.feedback.ladder_replay._forward_return`).
* ``market_excess_return`` — ``forward_return − benchmark_window_return``.

Rows whose window is not recoverable, or whose benchmark fetch returns no bars,
get ``None`` for both columns (never a fudged value — memo §4: "do NOT silently
fudge"). ``forward_return`` itself is left untouched as the gross/raw leg.

Telemetry only; this reads briefs-independent price data + the existing parquet
store, never any click ledger.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from alphalens_pipeline.feedback.bar_window import ARRIVAL_VWAP_WINDOW_MIN, _window_vwap
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    session_on_or_after,
    session_open_utc,
)

logger = logging.getLogger(__name__)

# Default market index for the v1 benchmark leg (memo R3: "market index v1
# (SPY/IWM same-window)"). SPY is the broad-market proxy; sector/factor-neutral
# excess is deferred to Phase 2. A single constant keeps the metric homogeneous
# across rows and easy to retune.
DEFAULT_BENCHMARK_TICKER = "SPY"

# Minutes of the horizon-end session to include so the benchmark window covers
# the full final session (open → close, half-days too). Mirrors the monitor's
# ``_HORIZON_SESSION_SPAN_MIN`` so the benchmark window matches the candidate's.
_HORIZON_SESSION_SPAN_MIN = 480

# The two columns this module writes. Listed once so the ingest side and the
# carry-forward back-fill can reference the same names.
BENCHMARK_COLUMNS = ("benchmark_window_return", "market_excess_return")

# A (ticker, window start, window end) → list of Polygon agg bars. Same shape as
# ``bar_window.BarFetch`` so the production default + test stubs are shared.
BarFetch = Callable[[str, dt.datetime, dt.datetime], Sequence[dict[str, Any]]]


def _default_bar_fetch(
    ticker: str, start: dt.datetime, end: dt.datetime
) -> Sequence[dict[str, Any]]:
    """Production bar source: the canonical Polygon client minute aggregates."""
    from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client

    return get_default_polygon_client().get_agg_range(ticker=ticker, start=start, end=end)


def _recover_exit_session(row: dict[str, Any], *, last_closed_session: dt.date) -> dt.date | None:
    """Recover the exit session for a row's benchmark window.

    Terminal rows carry ``matured_at`` (the session the position resolved).
    Ongoing rows have ``matured_at = None``; their window runs to the last
    closed session (the same horizon end the candidate's ``forward_return``
    spans). Returns ``None`` when no usable date can be recovered.
    """
    raw = row.get("matured_at")
    parsed = _as_date(raw)
    if parsed is not None:
        return parsed
    return last_closed_session


def _as_date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().date()
        except (ValueError, TypeError):
            return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _benchmark_window_return(
    bars: Sequence[dict[str, Any]],
    *,
    arrival_open: dt.datetime,
) -> float | None:
    """Raw close-to-close benchmark return over ``bars``.

    Uses the SAME anchor convention as the candidate's ``forward_return``: the
    reference is the arrival opening-window VWAP (first ``ARRIVAL_VWAP_WINDOW_MIN``
    minutes), the numerator is the last bar's close. Returns ``None`` when the
    window has no bars or a zero reference.
    """
    if not bars:
        return None
    arrival_window_end = arrival_open + dt.timedelta(minutes=ARRIVAL_VWAP_WINDOW_MIN)
    reference = _window_vwap(bars, arrival_open, arrival_window_end)
    if reference is None or reference == 0:
        return None
    last_close = bars[-1].get("c")
    if last_close is None:
        return None
    return (float(last_close) - reference) / reference


def compute_market_excess_for_row(
    row: dict[str, Any],
    *,
    bar_fetch: BarFetch,
    last_closed_session: dt.date,
    benchmark_ticker: str = DEFAULT_BENCHMARK_TICKER,
    exchange: str = DEFAULT_EXCHANGE,
) -> tuple[float | None, float | None]:
    """``(benchmark_window_return, market_excess_return)`` for one store row.

    Returns ``(None, None)`` when the candidate has no ``forward_return``, the
    window is not recoverable, or the benchmark fetch yields no bars — never a
    fudged value.
    """
    forward_return = row.get("forward_return")
    if forward_return is None or (isinstance(forward_return, float) and pd.isna(forward_return)):
        return None, None

    brief_date = _as_date(row.get("brief_date"))
    if brief_date is None:
        return None, None
    exit_session = _recover_exit_session(row, last_closed_session=last_closed_session)
    if exit_session is None:
        return None, None

    arrival_session = session_on_or_after(brief_date, exchange)
    if exit_session < arrival_session:
        # A degenerate window (exit before arrival) — not recoverable.
        return None, None

    arrival_open = session_open_utc(arrival_session, exchange)
    horizon_end = session_open_utc(exit_session, exchange) + dt.timedelta(
        minutes=_HORIZON_SESSION_SPAN_MIN
    )
    try:
        bars = list(bar_fetch(benchmark_ticker, arrival_open, horizon_end))
    except Exception as exc:
        logger.warning(
            "benchmark-excess: fetch failed for %s window [%s, %s] — %s; leaving None.",
            benchmark_ticker,
            arrival_session.isoformat(),
            exit_session.isoformat(),
            exc,
        )
        return None, None

    benchmark_return = _benchmark_window_return(bars, arrival_open=arrival_open)
    if benchmark_return is None:
        return None, None
    excess = float(forward_return) - benchmark_return
    return benchmark_return, excess


def enrich_store_with_benchmark_excess(
    store_dir: Path | str,
    *,
    bar_fetch: BarFetch | None = None,
    now: dt.datetime | None = None,
    benchmark_ticker: str = DEFAULT_BENCHMARK_TICKER,
    exchange: str = DEFAULT_EXCHANGE,
) -> int:
    """Add / refresh the benchmark-excess columns on every store parquet.

    Reads each ``YYYY-MM-DD.parquet`` in ``store_dir``, computes the two
    benchmark columns per row, and rewrites the frame atomically. Returns the
    number of rows that got a non-null ``market_excess_return``.

    The benchmark window per (arrival, exit) is fetched once per distinct window
    via a small in-run cache so repeated tickers on the same date pay a single
    Polygon call for the shared index window.
    """
    from alphalens_pipeline.paper.calendar import previous_trading_day

    store = Path(store_dir)
    if not store.exists():
        return 0
    fetch = bar_fetch or _default_bar_fetch
    now = now or dt.datetime.now(dt.UTC)
    last_closed_session = previous_trading_day(now.date(), exchange)

    # (arrival_session, exit_session) -> (benchmark_return) cache. The index
    # window is identical for every candidate sharing the same arrival+exit, so
    # one fetch serves all of them.
    window_cache: dict[tuple[dt.date, dt.date], float | None] = {}
    n_enriched = 0

    for path in sorted(store.glob("*.parquet")):
        try:
            df = pd.read_parquet(path)
        except (OSError, ValueError) as exc:
            logger.warning("benchmark-excess: bad store parquet %s — %s; skipping.", path, exc)
            continue

        bench_col: list[float | None] = []
        excess_col: list[float | None] = []
        for _, row in df.iterrows():
            row_d = dict(row)
            bench, excess = _row_excess_cached(
                row_d,
                fetch=fetch,
                last_closed_session=last_closed_session,
                benchmark_ticker=benchmark_ticker,
                exchange=exchange,
                window_cache=window_cache,
            )
            bench_col.append(bench)
            excess_col.append(excess)
            if excess is not None:
                n_enriched += 1

        df["benchmark_window_return"] = bench_col
        df["market_excess_return"] = excess_col
        _write_atomic(path, df)

    return n_enriched


def _row_excess_cached(
    row: dict[str, Any],
    *,
    fetch: BarFetch,
    last_closed_session: dt.date,
    benchmark_ticker: str,
    exchange: str,
    window_cache: dict[tuple[dt.date, dt.date], float | None],
) -> tuple[float | None, float | None]:
    """``compute_market_excess_for_row`` with a per-window benchmark-return cache."""
    forward_return = row.get("forward_return")
    if forward_return is None or (isinstance(forward_return, float) and pd.isna(forward_return)):
        return None, None
    brief_date = _as_date(row.get("brief_date"))
    if brief_date is None:
        return None, None
    exit_session = _recover_exit_session(row, last_closed_session=last_closed_session)
    if exit_session is None:
        return None, None
    arrival_session = session_on_or_after(brief_date, exchange)
    if exit_session < arrival_session:
        return None, None

    key = (arrival_session, exit_session)
    if key in window_cache:
        benchmark_return = window_cache[key]
    else:
        arrival_open = session_open_utc(arrival_session, exchange)
        horizon_end = session_open_utc(exit_session, exchange) + dt.timedelta(
            minutes=_HORIZON_SESSION_SPAN_MIN
        )
        try:
            bars = list(fetch(benchmark_ticker, arrival_open, horizon_end))
        except Exception as exc:
            logger.warning(
                "benchmark-excess: fetch failed for %s window [%s, %s] — %s; leaving None.",
                benchmark_ticker,
                arrival_session.isoformat(),
                exit_session.isoformat(),
                exc,
            )
            benchmark_return = None
        else:
            benchmark_return = _benchmark_window_return(bars, arrival_open=arrival_open)
        window_cache[key] = benchmark_return

    if benchmark_return is None:
        return None, None
    return benchmark_return, float(forward_return) - benchmark_return


def _write_atomic(path: Path, df: pd.DataFrame) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, path)


__all__ = [
    "BENCHMARK_COLUMNS",
    "DEFAULT_BENCHMARK_TICKER",
    "compute_market_excess_for_row",
    "enrich_store_with_benchmark_excess",
]
