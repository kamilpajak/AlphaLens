"""Broker-free ladder-replay nightly driver (the impure I/O layer).

This is the impure sibling of the pure :mod:`alphalens_pipeline.feedback.
ladder_replay` engine. It enumerates matured feedback DECISIONS (NEVER the paper
ledger), looks each ladder up from the brief parquet ``brief_trade_setup``,
fetches the intraday minute path via Polygon, replays it through the pure engine,
and stamps the gen-4 ladder-outcome columns onto every decision in the group.

Broker-free contract (design memo §5.1): enumeration is from the feedback
``decisions`` table, not the paper ledger. This module does NOT import or call
``fetch_plans_for_date`` / ``fetch_outcome_for_plan`` / ``compute_shadow_returns``
— it reuses ONLY the pure session / maturity / Polygon-fetch helpers from
``shadow_return`` (the same VWAP-window + horizon-session arithmetic the
shadow-return job uses), so the ``reference_close`` anchor stays consistent with
``shadow_return`` (the arrival opening-window VWAP), NOT the blended entry.

Click-orthogonality: the only ledger READ is :meth:`FeedbackStore.
iter_decisions_for_ladder`, which projects id / brief_date / ticker only — no
click column.

Resilience: a per-group failure (missing brief, bad setup, fetch error, no bars)
is logged and counted, never fatal — one bad ticker never aborts the sweep, and
the CLI wraps the whole call in a never-raises guard.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alphalens_feedback.store import FeedbackStore

from alphalens_pipeline.feedback.ladder_replay import LadderOutcome, replay_ladder
from alphalens_pipeline.feedback.shadow_return import (
    ARRIVAL_VWAP_WINDOW_MIN,
    DEFAULT_LOOKBACK_DAYS,
    HOLDING_HORIZON_TRADING_DAYS,
    _window_vwap,
)
from alphalens_pipeline.paper.brief_loader import load_brief
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    session_on_or_after,
    session_open_utc,
)

logger = logging.getLogger(__name__)

# A (ticker, window start, window end) → list of Polygon agg bars. Same shape as
# ``shadow_return.BarFetch`` so the production default + test stubs are shared.
BarFetch = Callable[[str, dt.datetime, dt.datetime], Sequence[dict[str, Any]]]

# Minutes of the horizon-end session to include so the replay path covers the
# full hold horizon (open of arrival session → close of the horizon session).
# XNYS regular session is 6.5h; 480 min spans it with margin (half-days too).
_HORIZON_SESSION_SPAN_MIN = 480


@dataclass(frozen=True)
class LadderBackfillReport:
    """Summary of one ladder-replay sweep over a single brief date."""

    brief_date: dt.date
    matured: bool  # False => horizon not yet closed; nothing replayed
    groups: int  # unique (brief_date, ticker) groups considered
    stamped: int  # decision rows stamped (>= groups when multi-theme)
    skipped_unmatured: int  # decisions skipped because the date is not matured
    no_structure: int  # groups whose setup parsed to NO_STRUCTURE
    no_data: int  # groups with a missing brief / no bars / fetch error


def _default_bar_fetch(
    ticker: str, start: dt.datetime, end: dt.datetime
) -> Sequence[dict[str, Any]]:
    """Production bar source: the canonical Polygon client minute aggregates."""
    from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client

    return get_default_polygon_client().get_agg_range(ticker=ticker, start=start, end=end)


def replay_ladder_decisions_window(
    feedback_path: Path,
    briefs_dir: Path,
    *,
    end_date: dt.date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    bar_fetch: BarFetch | None = None,
    now: dt.datetime | None = None,
    exchange: str = DEFAULT_EXCHANGE,
) -> list[LadderBackfillReport]:
    """Replay the ladder for every matured, not-yet-replayed feedback decision.

    Sweeps decisions whose ``brief_date >= end_date - lookback_days`` (the same
    date origin the shadow-return sweep uses) AND whose ``ladder_classification``
    is still NULL. Groups by ``(brief_date, ticker)``, replays ONCE per group,
    and stamps every member decision id. Per-date maturity is gated exactly like
    the shadow-return job: the horizon session must be strictly in the past
    (Polygon serves only closed sessions).

    Returns one :class:`LadderBackfillReport` per brief date touched (newest
    first), so the caller can print an aggregate.
    """
    now = now or dt.datetime.now(dt.UTC)
    end = end_date or now.date()
    fetch = bar_fetch or _default_bar_fetch
    lookback_start = end - dt.timedelta(days=lookback_days)

    with FeedbackStore.open(Path(feedback_path)) as fb:
        rows = fb.iter_decisions_for_ladder(lookback_start=lookback_start)

    # Group decision ids by (brief_date, ticker). Order preserved newest-first
    # by the store query; group iteration order does not affect correctness.
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for decision_id, brief_date_str, ticker in rows:
        groups[(brief_date_str, ticker.upper())].append(decision_id)

    # Bucket groups by brief date so the maturity gate + per-date report mirror
    # the shadow-return structure.
    by_date: dict[dt.date, list[tuple[str, list[str]]]] = defaultdict(list)
    for (brief_date_str, ticker), ids in groups.items():
        by_date[dt.date.fromisoformat(brief_date_str)].append((ticker, ids))

    reports: list[LadderBackfillReport] = []
    with FeedbackStore.open(Path(feedback_path)) as fb:
        for brief_date in sorted(by_date, reverse=True):  # newest first
            reports.append(
                _replay_one_date(
                    fb,
                    briefs_dir,
                    brief_date,
                    by_date[brief_date],
                    fetch=fetch,
                    now=now,
                    exchange=exchange,
                )
            )
    return reports


def _replay_one_date(
    fb: FeedbackStore,
    briefs_dir: Path,
    brief_date: dt.date,
    ticker_groups: list[tuple[str, list[str]]],
    *,
    fetch: BarFetch,
    now: dt.datetime,
    exchange: str,
) -> LadderBackfillReport:
    """Maturity-gate, then replay + stamp every group on one brief date."""
    n_groups = len(ticker_groups)
    n_decisions = sum(len(ids) for _t, ids in ticker_groups)

    # Maturity gate: the horizon session must be strictly in the past.
    horizon_session = advance_trading_sessions(brief_date, HOLDING_HORIZON_TRADING_DAYS, exchange)
    if horizon_session >= now.date():
        logger.info(
            "ladder-replay: horizon session %s for brief_date %s not matured (>= today %s) — skipping.",
            horizon_session.isoformat(),
            brief_date.isoformat(),
            now.date().isoformat(),
        )
        return LadderBackfillReport(
            brief_date=brief_date,
            matured=False,
            groups=n_groups,
            stamped=0,
            skipped_unmatured=n_decisions,
            no_structure=0,
            no_data=0,
        )

    # Anchor windows (same arithmetic as shadow_return). reference_close = the
    # arrival opening-window VWAP, consistent with shadow_return's anchor.
    arrival_session = session_on_or_after(brief_date, exchange)
    arrival_start = session_open_utc(arrival_session, exchange)
    arrival_end = arrival_start + dt.timedelta(minutes=ARRIVAL_VWAP_WINDOW_MIN)
    horizon_start = session_open_utc(horizon_session, exchange)
    horizon_end = horizon_start + dt.timedelta(minutes=_HORIZON_SESSION_SPAN_MIN)

    # Load + index the brief once per date.
    setups = _load_setups(briefs_dir, brief_date)

    stamped = 0
    no_structure = 0
    no_data = 0
    for ticker, ids in ticker_groups:
        # zen MEDIUM: a missing brief or a transient fetch failure / no-bars is
        # RETRYABLE — leave ladder_classification NULL so the next nightly sweep
        # re-attempts it within the lookback window (bounded; ages out
        # naturally). We must NOT stamp a terminal NO_DATA, because the
        # iter_decisions_for_ladder NULL gate would then skip the row forever
        # and a single Polygon 429 / a not-yet-built brief would permanently
        # abandon the decision (the SEC-ingest cache-poisoning class of bug).
        if setups is None:
            no_data += 1  # whole-date brief gap: retry next sweep, do NOT stamp
            continue
        setup = setups.get(ticker.upper())
        outcome = _replay_group(ticker, setup, fetch, arrival_start, arrival_end, horizon_end)
        if outcome is None:
            no_data += 1  # transient fetch error / no bars: retry, do NOT stamp
            continue
        if outcome.status == "NO_STRUCTURE":
            no_structure += 1
        for did in ids:
            _stamp(fb, did, outcome)
            stamped += 1

    return LadderBackfillReport(
        brief_date=brief_date,
        matured=True,
        groups=n_groups,
        stamped=stamped,
        skipped_unmatured=0,
        no_structure=no_structure,
        no_data=no_data,
    )


def _load_setups(briefs_dir: Path, brief_date: dt.date) -> dict[str, dict | None] | None:
    """Index ``ticker.upper() -> trade_setup`` for a brief date.

    Returns ``None`` when the brief parquet is missing / unreadable (the whole
    date's groups are then counted as no_data and left UNSTAMPED for a later
    retry — see the no_data handling in :func:`_replay_one_date`) — a missing
    brief is a data gap, not a crash.
    """
    try:
        candidates = load_brief(brief_date, briefs_dir)
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("ladder-replay: brief load failed for %s — %s", brief_date.isoformat(), exc)
        return None
    return {c.ticker.upper(): c.trade_setup for c in candidates}


def _replay_group(
    ticker: str,
    setup: dict | None,
    fetch: BarFetch,
    arrival_start: dt.datetime,
    arrival_end: dt.datetime,
    horizon_end: dt.datetime,
) -> LadderOutcome | None:
    """Fetch the path + replay one ticker. ``None`` on fetch error / no bars.

    A NO_STRUCTURE setup short-circuits to a NO_STRUCTURE outcome WITHOUT a fetch
    (no point pricing a candidate with no ladder).
    """
    if not setup or setup.get("status") != "OK":
        return replay_ladder(setup, [])  # NO_STRUCTURE outcome, no network call
    from alphalens_pipeline.data.alt_data.polygon_client import PolygonError

    try:
        bars = fetch(ticker, arrival_start, horizon_end)
    except (PolygonError, ValueError, KeyError, TypeError) as exc:
        logger.warning("ladder-replay: fetch failed for %s — skipping (%s)", ticker, exc)
        return None
    if not bars:
        logger.warning("ladder-replay: no bars for %s in the horizon window — skipping.", ticker)
        return None
    reference_close = _window_vwap(bars, arrival_start, arrival_end)
    return replay_ladder(setup, bars, reference_close=reference_close)


def _stamp(fb: FeedbackStore, decision_id: str, outcome: LadderOutcome) -> None:
    """Write the gen-4 ladder-outcome columns for one decision id.

    Only called for a real engine outcome (``OK`` or ``NO_STRUCTURE`` — the
    latter is terminal: a candidate with no ladder will never gain one). For a
    NO_STRUCTURE outcome the stored ``ladder_classification`` is the STATUS, not
    the inner ``NO_FILL`` default, so a non-NULL classification always means "the
    date was processed with data" — which the ``iter_decisions_for_ladder`` NULL
    gate relies on. Transient/retryable gaps (missing brief, fetch error, no
    bars) are deliberately NOT stamped here (see :func:`_replay_one_date`).
    """
    classification = outcome.classification if outcome.status == "OK" else outcome.status
    fb.stamp_ladder_outcome(
        decision_id,
        sequence_str=outcome.sequence_str(),
        mfe=outcome.mfe,
        mae=outcome.mae,
        forward_return=outcome.forward_return,
        ladder_classification=classification,
        blended_entry=outcome.blended_entry,
        realized_r=outcome.realized_r,
        horizon_open=str(outcome.horizon_open),
        ambiguous_bars=outcome.ambiguous_bars,
        ratchet_realized_r=outcome.ratchet_realized_r,
    )


__all__ = [
    "BarFetch",
    "LadderBackfillReport",
    "replay_ladder_decisions_window",
]
