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

* ``benchmark_window_return`` — the market index (SPY) raw return over the SAME
  window as ``forward_return``, with the SAME two prints (#1445): the reference
  is the arrival-session opening-window VWAP (first ``ARRIVAL_VWAP_WINDOW_MIN``
  minutes of minute bars, the candidate's own anchor), and the exit print is the
  OFFICIAL close of the exit session read from the monitor's grouped-daily cache
  (``population_ladders/grouped/<session>.parquet``, disk-first, one grouped
  call per session the cache lacks) — the same file and field the monitor uses
  for a terminal row's ``forward_return`` since #1444. The minute fetch therefore
  covers only the 30-minute arrival window, once per (ticker, arrival session)
  per run, never the whole holding window. Until #1445 the exit print was the
  last available minute bar, an after-hours print up to 17:30 ET, so the two
  legs ended at different prints of the same day.
* ``benchmark_window_exit`` — the ISO date of the exit session the pair was
  computed over. Reuse-first (:func:`_has_consistent_stored_pair`) accepts a
  stored pair only while this still equals the row's ``matured_at``: the
  2026-09-11 rebuild computed every window to the rebuild night, and once
  ``matured_at`` was repaired the pairs stayed arithmetically consistent and were
  reused forever (#1444). Consistency alone cannot see a moved window.
* ``benchmark_leg_version`` — the convention the pair was computed under
  (``BENCHMARK_LEG_VERSION``). Reuse-first also requires equality, so a change of
  convention recomputes the whole store through the ordinary nightly gate with
  no operator step; the sector pass has the same in ``outcome_benchmark_version``.
* ``market_excess_return`` — ``forward_return − benchmark_window_return``.

Rows whose window is not recoverable, whose anchor fetch returns no bars, or
whose exit session has no official close on record get ``None`` for both
columns (never a fudged value — memo §4: "do NOT silently fudge"; a missing
close is retried next run, never replaced by a minute bar).
``forward_return`` itself is left untouched as the gross/raw leg. A
``SPLIT_INVALIDATED`` quarantine (:func:`row_is_quarantined`) gets no pair at
all, and a pair it once carried is nulled rather than reused (#1452): its
``forward_return`` is raw replay telemetry of what tripped the corporate-action
guard, not an outcome, so an excess over it would be a meaningless number
settled under the pre-registered poolability key.

Telemetry only; this reads briefs-independent price data + the existing parquet
store, never any click ledger.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeGuard

import pandas as pd

from alphalens_pipeline.feedback.bar_window import ARRIVAL_VWAP_WINDOW_MIN, _window_vwap
from alphalens_pipeline.feedback.corporate_actions import SPLIT_INVALIDATED_CLASSIFICATION
from alphalens_pipeline.feedback.ladder_config import ladder_arrival_session
from alphalens_pipeline.feedback.population_ladder_monitor import (
    GroupedFetch,
    _default_grouped_fetch,
    _grouped_close,
    _prefetch_grouped_daily,
)
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    previous_trading_day,
    session_open_utc,
)

logger = logging.getLogger(__name__)

# Default market index for the v1 benchmark leg (memo R3: "market index v1
# (SPY/IWM same-window)"). SPY is the broad-market proxy; sector/factor-neutral
# excess is deferred to Phase 2. A single constant keeps the metric homogeneous
# across rows and easy to retune.
DEFAULT_BENCHMARK_TICKER = "SPY"

# The leg convention a stored pair was computed under; a pair carrying another
# value (or none) is a gap and is recomputed. v2 = arrival VWAP anchor + OFFICIAL
# close of the exit session (#1445); v1 (never stamped) was the last minute bar.
BENCHMARK_LEG_VERSION = "spy-v2-official-close"

# The columns this module writes. Listed once so the ingest side and the
# carry-forward back-fill can reference the same names.
BENCHMARK_COLUMNS = (
    "benchmark_window_return",
    "market_excess_return",
    "benchmark_window_exit",
    "benchmark_leg_version",
)

# A (ticker, window start, window end) → list of Polygon agg bars. Same shape as
# ``bar_window.BarFetch`` so the production default + test stubs are shared.
BarFetch = Callable[[str, dt.datetime, dt.datetime], Sequence[dict[str, Any]]]

# (ticker, session) → the OFFICIAL close of that session, or None when the
# session has no close on record for the ticker. Built per run from the
# prefetched grouped-daily maps (:func:`_exit_close_lookup`).
ExitCloseOf = Callable[[str, dt.date], float | None]

# (ticker, arrival session) → the arrival opening-window VWAP reference, or None
# when the anchor could not be computed this run (cached so it is not retried
# within the run; retried next run). The exit close is free (on disk), so the
# anchor is the only thing worth caching.
AnchorCache = dict[tuple[str, dt.date], float | None]


def _default_bar_fetch(
    ticker: str, start: dt.datetime, end: dt.datetime
) -> Sequence[dict[str, Any]]:
    """Production bar source: the canonical Polygon client minute aggregates."""
    from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client

    return get_default_polygon_client().get_agg_range(ticker=ticker, start=start, end=end)


def _recover_exit_session(row: dict[str, Any], *, last_closed_session: dt.date) -> dt.date | None:
    """Recover the exit session for a row's benchmark window.

    Terminal rows carry ``matured_at`` — since #1442 the session the decision
    ENDED (last crossing, or the entry-expiry session for a NO_FILL), so the
    benchmark leg spans arrival → exit, and since #1444 the candidate leg
    (``forward_return``) ends on the same session's official close, on a rebuild
    as on an incremental night. Ongoing rows have ``matured_at = None``; their
    window runs to the last closed session. Returns ``None`` when no usable date
    can be recovered.
    """
    raw = row.get("matured_at")
    parsed = _as_date(raw)
    if parsed is not None:
        return parsed
    return last_closed_session


def _as_date(value: Any) -> dt.date | None:
    if value is None or value is pd.NaT:
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


def _arrival_reference(
    bars: Sequence[dict[str, Any]], *, arrival_open: dt.datetime
) -> float | None:
    """The benchmark's anchor: the arrival opening-window VWAP over ``bars``
    (first ``ARRIVAL_VWAP_WINDOW_MIN`` minutes, half-open) — the SAME reference
    convention as the candidate's ``forward_return``. ``None`` when no bar falls
    in the window or the VWAP is not a positive number."""
    if not bars:
        return None
    arrival_window_end = arrival_open + dt.timedelta(minutes=ARRIVAL_VWAP_WINDOW_MIN)
    reference = _window_vwap(bars, arrival_open, arrival_window_end)
    if reference is None or reference <= 0:
        return None
    return reference


def _window_return(reference: float | None, exit_close: float | None) -> float | None:
    """``(exit_close − reference) / reference``; ``None`` unless both are positive
    numbers (a zero close from the grouped cache is ``0.0``, real and wrong)."""
    if not _is_real(reference) or not _is_real(exit_close):
        return None
    if reference <= 0 or exit_close <= 0:
        return None
    return (float(exit_close) - float(reference)) / float(reference)


def _anchor_from_pair(window: float | None, exit_close: float | None) -> float | None:
    """Recover the reference a settled pair was computed against
    (``close / (1 + window)``), so a reused row can seed the anchor cache for a
    gap sibling with the same arrival. ``None`` unless the arithmetic is safe."""
    if not _is_real(window) or not _is_real(exit_close):
        return None
    if exit_close <= 0 or 1.0 + float(window) <= 0:
        return None
    return float(exit_close) / (1.0 + float(window))


def _fetch_arrival_reference(
    fetch: BarFetch, ticker: str, arrival_session: dt.date, exchange: str
) -> float | None:
    """One minute fetch over the arrival VWAP window only, reduced to the anchor."""
    arrival_open = session_open_utc(arrival_session, exchange)
    window_end = arrival_open + dt.timedelta(minutes=ARRIVAL_VWAP_WINDOW_MIN)
    try:
        bars = list(fetch(ticker, arrival_open, window_end))
    except Exception as exc:
        logger.warning(
            "benchmark-excess: anchor fetch failed for %s on %s — %s; leaving None.",
            ticker,
            arrival_session.isoformat(),
            exc,
        )
        return None
    return _arrival_reference(bars, arrival_open=arrival_open)


def _official_close(exit_close_of: ExitCloseOf, ticker: str, session: dt.date) -> float | None:
    close = exit_close_of(ticker, session)
    if close is None:
        logger.warning(
            "benchmark-excess: no official close for %s on %s; leaving None (retried next run).",
            ticker,
            session.isoformat(),
        )
    return close


def _window_bounds(
    row: dict[str, Any], *, last_closed_session: dt.date, exchange: str
) -> tuple[dt.date, dt.date] | None:
    """``(arrival_session, exit_session)`` for a row with a real ``forward_return``
    and a recoverable, non-degenerate window; ``None`` otherwise."""
    forward_return = row.get("forward_return")
    if not _is_real(forward_return):
        return None
    brief_date = _as_date(row.get("brief_date"))
    if brief_date is None:
        return None
    exit_session = _recover_exit_session(row, last_closed_session=last_closed_session)
    if exit_session is None:
        return None
    arrival_session = ladder_arrival_session(brief_date, exchange)
    if exit_session < arrival_session:
        # A degenerate window (exit before arrival) — not recoverable.
        return None
    return arrival_session, exit_session


def compute_market_excess_for_row(
    row: dict[str, Any],
    *,
    bar_fetch: BarFetch,
    exit_close_of: ExitCloseOf,
    last_closed_session: dt.date,
    benchmark_ticker: str = DEFAULT_BENCHMARK_TICKER,
    exchange: str = DEFAULT_EXCHANGE,
) -> tuple[float | None, float | None]:
    """``(benchmark_window_return, market_excess_return)`` for one store row.

    The anchor is one minute fetch over the arrival VWAP window; the exit print
    is ``exit_close_of(benchmark_ticker, exit_session)``, the official close of
    the exit session. Returns ``(None, None)`` when the candidate has no
    ``forward_return``, the window is not recoverable, the anchor fetch yields no
    bars, or the exit session has no official close — never a fudged value.
    """
    bounds = _window_bounds(row, last_closed_session=last_closed_session, exchange=exchange)
    if bounds is None:
        return None, None
    arrival_session, exit_session = bounds
    reference = _fetch_arrival_reference(bar_fetch, benchmark_ticker, arrival_session, exchange)
    if reference is None:
        return None, None
    benchmark_return = _window_return(
        reference, _official_close(exit_close_of, benchmark_ticker, exit_session)
    )
    if benchmark_return is None:
        return None, None
    return benchmark_return, float(row["forward_return"]) - benchmark_return


def _exit_close_lookup(
    store_dir: Path,
    sessions: set[dt.date],
    grouped_fetch: GroupedFetch,
    grouped_memo: dict[dt.date, dict[str, dict[str, Any]] | None],
    exchange: str,
) -> ExitCloseOf:
    """Prefetch the grouped-daily maps for ``sessions`` (disk-first, one grouped
    call per session the cache lacks, memoised in ``grouped_memo`` across files
    so a session is read once per run) and return the official-close lookup."""
    missing = sorted(s for s in sessions if s not in grouped_memo)
    if missing:
        grouped_memo.update(_prefetch_grouped_daily(store_dir, missing, grouped_fetch, exchange))

    def _close(ticker: str, session: dt.date) -> float | None:
        return _grouped_close(grouped_memo.get(session), ticker)

    return _close


def _frame_exit_sessions(
    df: pd.DataFrame, *, last_closed_session: dt.date, exchange: str
) -> set[dt.date]:
    """The exit sessions the rows of ``df`` will read an official close for."""
    sessions: set[dt.date] = set()
    for _, row in df.iterrows():
        bounds = _window_bounds(
            dict(row), last_closed_session=last_closed_session, exchange=exchange
        )
        if bounds is not None:
            sessions.add(bounds[1])
    return sessions


def _enrich_frame_rows(
    df: pd.DataFrame,
    *,
    fetch: BarFetch,
    last_closed_session: dt.date,
    benchmark_ticker: str,
    exchange: str,
    anchor_cache: AnchorCache,
    exit_close_of: ExitCloseOf,
    deadline: Any,
) -> tuple[list[float | None], list[float | None], list[str | None], int, bool, int, int]:
    """Compute the two benchmark columns for one frame.

    Reuse-first: a TERMINAL row with a consistent stored ``(benchmark, excess)``
    pair is settled — its window is frozen, so the stored pair is reused
    verbatim with NO fetch. Only gaps (missing/inconsistent pair) and ongoing
    rows pay a fetch. This is what lets the shared wall-clock budget reach the
    old unbenchmarked tail instead of being spent re-fetching already-settled
    rows every run.

    Returns ``(bench_col, excess_col, exit_col, n_enriched, stopped_early,
    n_reused, n_fetched)``. When the deadline trips mid-frame the partial columns are
    returned with ``stopped_early=True`` so the caller can leave the parquet
    untouched; rows already processed still count toward ``n_enriched``
    (matching the pre-refactor behaviour where the counter incremented before
    the break).
    """
    bench_col: list[float | None] = []
    excess_col: list[float | None] = []
    exit_col: list[str | None] = []
    n_enriched = 0
    n_reused = 0
    n_fetched = 0
    for _, row in df.iterrows():
        if deadline is not None and deadline.should_stop():
            return bench_col, excess_col, exit_col, n_enriched, True, n_reused, n_fetched
        if row_is_quarantined(row):
            # Before the reuse check on purpose: a quarantine's stored pair can
            # be settled and consistent (the production MQ row was), and the
            # reuse branch would carry it for ever. No pair, no stamp, no fetch;
            # counted under n_fetched so the file is rewritten (nulling a pair
            # that is on disk) — see the n_fetched note below.
            bench_col.append(None)
            excess_col.append(None)
            exit_col.append(None)
            n_fetched += 1
            continue
        exit_session = _recover_exit_session(dict(row), last_closed_session=last_closed_session)
        if _has_consistent_stored_pair(row):
            bench = float(row["benchmark_window_return"])
            excess = float(row["market_excess_return"])
            bench_col.append(bench)
            excess_col.append(excess)
            # Carry the window stamp: the predicate just proved it equals
            # matured_at. Dropping it here would turn every reused row into a
            # gap on the next run and refetch the store forever.
            exit_col.append(exit_session.isoformat() if exit_session is not None else None)
            n_enriched += 1
            n_reused += 1
            # Seed the anchor cache so a GAP sibling with the same arrival is
            # served free instead of paying its own fetch (M1).
            _seed_anchor_cache_from_reused(
                row,
                anchor_cache,
                exit_close_of=exit_close_of,
                last_closed_session=last_closed_session,
                benchmark_ticker=benchmark_ticker,
                exchange=exchange,
            )
            continue
        bench, excess = _row_excess_cached(
            dict(row),
            fetch=fetch,
            last_closed_session=last_closed_session,
            benchmark_ticker=benchmark_ticker,
            exchange=exchange,
            anchor_cache=anchor_cache,
            exit_close_of=exit_close_of,
        )
        bench_col.append(bench)
        excess_col.append(excess)
        matured = _as_date(row.get("matured_at"))
        exit_col.append(matured.isoformat() if bench is not None and matured is not None else None)
        # n_fetched counts rows that did NOT take the reuse branch: rows that
        # entered the fetch branch (including rows short-circuited by a missing
        # forward_return before any network call) and SPLIT_INVALIDATED
        # quarantines nulled above — NOT strictly rows that issued a Polygon
        # call. It is the "did this frame change?" signal the M3 skip-write
        # decision below reads. A file whose only unresolved rows are ongoing /
        # no-forward-return / quarantined is still rewritten (content unchanged,
        # mtime bumps) — acceptable, since it never causes a needed write to be
        # wrongly skipped.
        n_fetched += 1
        if excess is not None:
            n_enriched += 1
    return bench_col, excess_col, exit_col, n_enriched, False, n_reused, n_fetched


def _is_real(value: Any) -> TypeGuard[float]:
    """True for a concrete number — not None and not NaN. Guards a transient None
    from clobbering an already-computed benchmark on the whole-store rewrite."""
    return value is not None and not (isinstance(value, float) and pd.isna(value))


def _has_consistent_stored_pair(row: pd.Series) -> bool:
    """True when the row is TERMINAL and already carries a benchmark/excess pair
    consistent with its forward_return. A terminal window is fixed, so such a pair
    is settled and needs no refetch.

    The consistency check (not just the ``matured_at`` gate) matters because of
    the maturation-transition hazard: when an ongoing row matures, the monitor
    advances ``forward_return`` and stamps ``matured_at`` in the SAME rewrite,
    but a stale ``(benchmark, excess)`` pair computed against the OLD
    ``forward_return`` can be carried forward verbatim on that write. Such a
    pair would pass a ``matured_at``-only gate yet no longer satisfy
    ``excess == forward - benchmark``, so it must be recomputed rather than
    reused — this is what ``test_transient_none_drops_a_stale_pair_inconsistent_with_forward``
    guards. (Same predicate the removed _carry_forward_prev_pair used; it now
    lives only here.)

    The pair must also have been computed over THIS ``matured_at``
    (``benchmark_window_exit``, #1444): a pair whose window ended elsewhere can
    be arithmetically consistent and still wrong, and a pair without a recorded
    window is treated the same way — recomputed, never trusted. And it must
    carry the CURRENT leg convention (``benchmark_leg_version``, #1445): a pair
    computed with another exit print is consistent, correctly stamped, and still
    a different quantity."""
    if row.get("benchmark_leg_version") != BENCHMARK_LEG_VERSION:
        return False
    return stored_pair_is_settled(
        row,
        window_col="benchmark_window_return",
        excess_col="market_excess_return",
        exit_col="benchmark_window_exit",
    )


def row_is_quarantined(row: pd.Series | dict[str, Any]) -> bool:
    """True for a ``SPLIT_INVALIDATED`` quarantine (#1452), shared by the SPY and
    the sector passes.

    The monitor's ``_apply_split_invalidation`` nulls ``realized_r`` / ``open_r``
    so every R aggregate excludes the row, but KEEPS ``forward_return`` as raw
    telemetry of what tripped the corporate-action guard (the ladder levels were
    set on pre-action prices). An excess over that value is not an outcome; the
    passes give such a row no pair, no window stamp and no fetch, and never
    reuse a pair it once carried. Missing / ``None`` / NaN classifications are
    not quarantines.
    """
    return str(row.get("ladder_classification") or "") == SPLIT_INVALIDATED_CLASSIFICATION


def stored_pair_is_settled(
    row: pd.Series | dict[str, Any], *, window_col: str, excess_col: str, exit_col: str
) -> bool:
    """The column-parametric settled-pair predicate shared by the SPY and the
    sector passes (#1435): ``matured_at`` set, the pair real, ``excess ==
    forward_return - window`` within 1e-9, and the recorded window exit equal to
    ``matured_at``. A sector row additionally needs its ETF and map version to
    match — the sector pass checks those itself, because only it knows them."""
    get = row.get
    matured = _as_date(get("matured_at"))
    if matured is None:
        return False
    if _as_date(get(exit_col)) != matured:
        return False
    window = get(window_col)
    excess = get(excess_col)
    forward = get("forward_return")
    return (
        _is_real(window)
        and _is_real(excess)
        and _is_real(forward)
        and abs(float(excess) - (float(forward) - float(window))) < 1e-9
    )


def _seed_anchor_cache_from_reused(
    row: pd.Series,
    anchor_cache: AnchorCache,
    *,
    exit_close_of: ExitCloseOf,
    last_closed_session: dt.date,
    benchmark_ticker: str,
    exchange: str,
) -> None:
    """Best-effort: seed the anchor cache from a reused row (M1).

    A reused row carries a settled ``benchmark_window_return`` and its exit
    session's official close is on record, so the reference it was computed
    against is ``close / (1 + window)`` (exact in floating point for the stored
    values). Seeding lets a GAP sibling with the same arrival be served for free
    instead of paying its own anchor fetch. Skips silently when the window or the
    close cannot be recovered — a pure optimisation, never a correctness
    requirement; an anchor already in the cache is never overwritten.
    """
    bounds = _window_bounds(dict(row), last_closed_session=last_closed_session, exchange=exchange)
    if bounds is None:
        return
    arrival_session, exit_session = bounds
    key = (benchmark_ticker, arrival_session)
    if key in anchor_cache:
        return
    reference = _anchor_from_pair(
        row.get("benchmark_window_return"), exit_close_of(benchmark_ticker, exit_session)
    )
    if reference is not None:
        anchor_cache[key] = reference


def enrich_store_with_benchmark_excess(
    store_dir: Path | str,
    *,
    bar_fetch: BarFetch | None = None,
    now: dt.datetime | None = None,
    benchmark_ticker: str = DEFAULT_BENCHMARK_TICKER,
    exchange: str = DEFAULT_EXCHANGE,
    deadline: Any = None,
    grouped_fetch: GroupedFetch | None = None,
) -> int:
    """Add / refresh the benchmark-excess columns on every store parquet.

    Reads each ``YYYY-MM-DD.parquet`` in ``store_dir``, computes the benchmark
    columns per row, and rewrites the frame atomically. Returns the number of
    rows that got a non-null ``market_excess_return``.

    The anchor (arrival VWAP) is fetched once per distinct arrival session via a
    small in-run cache; the exit print is the official close read from the
    monitor's grouped-daily cache under ``store_dir/grouped`` (disk-first, one
    ``grouped_fetch`` call per session the cache lacks, memoised across files).

    When ``deadline`` is provided and ``deadline.should_stop()`` is True at the
    top of the per-row loop, the row loop breaks early. Rows left unprocessed
    keep their existing (None) values in the store — they are not marked as done
    and will be retried on the next run. ``deadline`` is typed ``Any`` (callers
    pass the monitor's ``_RunDeadline``).
    """
    store = Path(store_dir)
    if not store.exists():
        logger.info("benchmark-excess: enriched 0 (reused 0, fetched 0)")
        return 0
    fetch = bar_fetch or _default_bar_fetch
    grouped = grouped_fetch or _default_grouped_fetch
    now = now or dt.datetime.now(dt.UTC)
    last_closed_session = previous_trading_day(now.date(), exchange)

    anchor_cache: AnchorCache = {}
    grouped_memo: dict[dt.date, dict[str, dict[str, Any]] | None] = {}
    n_enriched = 0
    n_reused_total = 0
    n_fetched_total = 0

    # Newest first: under the shared run deadline a truncated sweep must heal the
    # recent, dashboard-visible dates before the deep history. ISO-named files sort
    # lexicographically by date, so reverse=True is newest-first.
    for path in sorted(store.glob("*.parquet"), reverse=True):
        try:
            df = pd.read_parquet(path)
        except (OSError, ValueError) as exc:
            logger.warning("benchmark-excess: bad store parquet %s — %s; skipping.", path, exc)
            continue

        exit_close_of = _exit_close_lookup(
            store,
            _frame_exit_sessions(df, last_closed_session=last_closed_session, exchange=exchange),
            grouped,
            grouped_memo,
            exchange,
        )
        bench_col, excess_col, exit_col, n_delta, stopped_early, n_reused, n_fetched = (
            _enrich_frame_rows(
                df,
                fetch=fetch,
                last_closed_session=last_closed_session,
                benchmark_ticker=benchmark_ticker,
                exchange=exchange,
                anchor_cache=anchor_cache,
                exit_close_of=exit_close_of,
                deadline=deadline,
            )
        )
        if stopped_early:
            # Deadline tripped mid-file: leave the parquet untouched so
            # unprocessed rows are retried on the next run. Break rather
            # than continue — every subsequent file would be opened only to
            # immediately skip (deadline latches), so skip the open entirely.
            # Do NOT count this file's partial rows: they were computed but
            # never persisted, so the log must not report them as enriched.
            break

        # Count only rows that are (or already are) persisted: a non-stopped file
        # is fully written below, and a reuse-only (n_fetched == 0) file keeps its
        # values on disk from a prior run — both are honestly "enriched".
        n_enriched += n_delta
        n_reused_total += n_reused
        n_fetched_total += n_fetched

        if n_fetched == 0:
            # Every row in this file was reused (M3) — nothing changed, so
            # skip the write. Avoids bumping the parquet mtime for no reason,
            # which matters for downstream mtime-gated caches (e.g. the Django
            # edge-mirror).
            continue

        df["benchmark_window_return"] = bench_col
        df["market_excess_return"] = excess_col
        df["benchmark_window_exit"] = exit_col
        df["benchmark_leg_version"] = [BENCHMARK_LEG_VERSION] * len(df)
        _write_atomic(path, df)

    logger.info(
        "benchmark-excess: enriched %d (reused %d, fetched %d)",
        n_enriched,
        n_reused_total,
        n_fetched_total,
    )
    return n_enriched


def _row_excess_cached(
    row: dict[str, Any],
    *,
    fetch: BarFetch,
    last_closed_session: dt.date,
    benchmark_ticker: str,
    exchange: str,
    anchor_cache: AnchorCache,
    exit_close_of: ExitCloseOf,
) -> tuple[float | None, float | None]:
    """``compute_market_excess_for_row`` with a per-(ticker, arrival) anchor cache.

    A failed anchor fetch caches ``None`` on purpose: one attempt per anchor per
    run; it is retried next run.
    """
    bounds = _window_bounds(row, last_closed_session=last_closed_session, exchange=exchange)
    if bounds is None:
        return None, None
    arrival_session, exit_session = bounds
    key = (benchmark_ticker, arrival_session)
    if key not in anchor_cache:
        anchor_cache[key] = _fetch_arrival_reference(
            fetch, benchmark_ticker, arrival_session, exchange
        )
    reference = anchor_cache[key]
    if reference is None:
        return None, None
    benchmark_return = _window_return(
        reference, _official_close(exit_close_of, benchmark_ticker, exit_session)
    )
    if benchmark_return is None:
        return None, None
    return benchmark_return, float(row["forward_return"]) - benchmark_return


def _write_atomic(path: Path, df: pd.DataFrame) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp)
    os.replace(tmp, path)


__all__ = [
    "BENCHMARK_COLUMNS",
    "BENCHMARK_LEG_VERSION",
    "DEFAULT_BENCHMARK_TICKER",
    "compute_market_excess_for_row",
    "enrich_store_with_benchmark_excess",
    "row_is_quarantined",
    "stored_pair_is_settled",
]
