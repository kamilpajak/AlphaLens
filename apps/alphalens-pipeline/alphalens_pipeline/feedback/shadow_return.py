"""Compute the arrival-price shadow return for feedback decisions (Track A v2 PR-3).

What this answers
-----------------
For every brief candidate the paper harness shadow-trades — INCLUDING ones the
user never clicked and ones that never filled — what was the counterfactual
return of simply buying at the next session's open and holding a fixed horizon?
That is ``shadow_return``: an implementation-shortfall arrival benchmark
(Perold / Almgren-Chriss), NOT an oracle replay of the realised path.

Metric (locked)
---------------
``shadow_return = (horizon_vwap − arrival_vwap) / arrival_vwap`` (a decimal
fraction), where

* ``arrival_vwap`` = volume-weighted close over the first
  ``ARRIVAL_VWAP_WINDOW_MIN`` minutes of the FIRST XNYS session on-or-after
  ``brief_date`` (09:30–10:00 ET by default);
* ``horizon_vwap`` = the same opening window ``HOLDING_HORIZON_TRADING_DAYS``
  sessions later (weekends + holidays skipped via the exchange calendar).

Computed regardless of fill status. ``realized_return`` (the realised leg,
``(blended_exit − blended_entry) / blended_entry`` from the paper ledger) is a
SEPARATE column, defined only for FILLED outcomes — kept unit-consistent with
``shadow_return`` (both fractions) so the §6 execution gap
``realized_return − shadow_return`` is a fraction-minus-fraction.

Data-availability limitation (stated verbatim per the design)
-------------------------------------------------------------
The ideal anchor would be the VWAP of the first minutes after the candidate was
SURFACED, but the ledger carries no per-candidate intraday surfacing instant
(``plans.planned_at`` is the 13:05 UTC batch-cron time, identical for the whole
day; ``brief_date`` is a calendar day). So this anchors to the next-session
OPENING window — strictly weaker than true intraday arrival, but the strongest
anchor the ledger supports, and still point-in-time-honest (it never reads the
realised exit, so no look-ahead).

Maturity precondition (HARD)
----------------------------
The horizon window must have fully closed in the PAST. Two reasons: (1) a
forward N-session return is undefined until N sessions elapse; (2) Polygon's
free / Basic plan serves only past-session minute aggregates, not the current
session. If the horizon is not yet matured the whole run is skipped with a loud
WARNING and ``ShadowReturnReport.matured = False`` — never a silent all-NULL
"looks healthy" pass.

Resilience
----------
A per-ticker fetch failure (rate-limit, auth, malformed payload) or a window
with no bars is logged and SKIPPED — one bad ticker never aborts the sweep. An
implausible move (``|shadow_return| > IMPLAUSIBLE_RETURN_THRESHOLD``) is treated
as a likely corporate-action artifact (bars are ``adjusted=false``) and skipped
rather than stamping a corrupted number that would pollute later re-weighting.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alphalens_pipeline.feedback.outcome_join import _EXIT_KIND_TO_FILL_STATUS
from alphalens_pipeline.feedback.store import FeedbackStore
from alphalens_pipeline.paper import ledger as paper_ledger
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    session_on_or_after,
    session_open_utc,
)

logger = logging.getLogger(__name__)

# Opening window (minutes from the session open) over which the arrival /
# horizon VWAP is taken. 30 min damps opening-auction noise vs the single open
# print; cheap to retune (one constant).
ARRIVAL_VWAP_WINDOW_MIN = 30

# Holding horizon, in trading sessions, between the arrival anchor and the
# exit anchor. A single global constant keeps the metric homogeneous across
# rows; a per-plan ``order_ttl_days`` variant is a possible refinement but
# would make the cross-row comparison heterogeneous.
HOLDING_HORIZON_TRADING_DAYS = 5

# Above this absolute move the 5-session window almost certainly spans a split
# / special dividend (bars are adjusted=false) rather than a real return — skip
# and flag rather than stamp a corrupted value.
IMPLAUSIBLE_RETURN_THRESHOLD = 0.60

# A bar (dict) → ticker, window start, window end → list of Polygon agg bars.
BarFetch = Callable[[str, dt.datetime, dt.datetime], Sequence[dict[str, Any]]]


@dataclass(frozen=True)
class ShadowReturnReport:
    """Summary of one shadow-return sweep over a single brief date."""

    brief_date: dt.date
    account: str
    matured: bool  # False => horizon not yet closed; nothing computed
    n_outcomes: int  # matured plan outcomes considered
    n_priced: int  # decisions stamped with a shadow_return
    n_skipped: int  # fetch error / unmapped exit_kind / implausible move
    n_no_bars: int  # window had no bars (illiquid / no market data)


def _window_vwap(
    bars: Sequence[dict[str, Any]],
    start: dt.datetime,
    end: dt.datetime,
) -> float | None:
    """Volume-weighted close over bars whose start ``t`` is in ``[start, end)``.

    Returns ``None`` when no bar falls in the window. Degrades to the simple
    mean of closes when total volume is zero (an all-zero-volume thin-name
    window) so a VWAP is still produced rather than a divide-by-zero.
    """
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    pairs: list[tuple[float, float]] = []
    for bar in bars:
        t = bar.get("t")
        close = bar.get("c")
        if t is None or close is None or not (start_ms <= t < end_ms):
            continue
        pairs.append((float(close), float(bar.get("v") or 0.0)))
    if not pairs:
        return None
    total_vol = sum(v for _, v in pairs)
    if total_vol == 0:
        return sum(c for c, _ in pairs) / len(pairs)
    return sum(c * v for c, v in pairs) / total_vol


def _realized_return(outcome: Any, fill_status: str) -> float | None:
    """Realised return fraction from the paper ledger blended prices.

    Defined only for FILLED outcomes with both blended prices present; NULL for
    UNFILLED / PARTIAL-with-missing-price (so it is never confused with a real
    0.0 return).
    """
    if fill_status != "FILLED":
        return None
    entry = outcome["blended_entry_price"]
    exit_ = outcome["blended_exit_price"]
    if entry is None or exit_ is None or entry == 0:
        return None
    return (exit_ - entry) / entry


def _default_bar_fetch(
    ticker: str, start: dt.datetime, end: dt.datetime
) -> Sequence[dict[str, Any]]:
    """Production bar source: the canonical Polygon client minute aggregates."""
    from alphalens_pipeline.data.alt_data.polygon_client import get_default_polygon_client

    return get_default_polygon_client().get_agg_range(ticker=ticker, start=start, end=end)


def compute_shadow_returns(
    feedback_path: Path,
    ledger_path: Path,
    *,
    brief_date: dt.date,
    account: str = "test",
    bar_fetch: BarFetch | None = None,
    now: dt.datetime | None = None,
    exchange: str = DEFAULT_EXCHANGE,
) -> ShadowReturnReport:
    """Stamp ``shadow_return`` (+ ``realized_return``) onto decisions for a date.

    Runs AFTER the cheap fill-status join (``join_decision_outcomes``); it also
    re-stamps fill_status/exit_kind (idempotent) so it is self-sufficient. The
    two stores are opened sequentially (ledger then feedback), never nested, so
    the advisory file locks do not deadlock. Polygon errors are imported lazily
    only when needed for the resilience ``except``.
    """
    from alphalens_pipeline.data.alt_data.polygon_client import PolygonError

    now = now or dt.datetime.now(dt.UTC)
    fetch = bar_fetch or _default_bar_fetch

    # Maturity guard: the horizon session must be strictly in the past (Polygon
    # Basic serves only closed sessions, and a forward return is undefined until
    # the horizon elapses). Skip the WHOLE run loudly rather than silently
    # stamping nothing.
    horizon_session = advance_trading_sessions(brief_date, HOLDING_HORIZON_TRADING_DAYS, exchange)
    if horizon_session >= now.date():
        logger.warning(
            "shadow-return: horizon session %s for brief_date %s has not matured "
            "(>= today %s) — Polygon serves only closed sessions; skipping the run "
            "(0 stamped). Re-run after the horizon closes.",
            horizon_session.isoformat(),
            brief_date.isoformat(),
            now.date().isoformat(),
        )
        return ShadowReturnReport(
            brief_date=brief_date,
            account=account,
            matured=False,
            n_outcomes=0,
            n_priced=0,
            n_skipped=0,
            n_no_bars=0,
        )

    arrival_session = session_on_or_after(brief_date, exchange)
    arrival_start = session_open_utc(arrival_session, exchange)
    arrival_end = arrival_start + dt.timedelta(minutes=ARRIVAL_VWAP_WINDOW_MIN)
    horizon_start = session_open_utc(horizon_session, exchange)
    horizon_end = horizon_start + dt.timedelta(minutes=ARRIVAL_VWAP_WINDOW_MIN)

    # 1. Gather matured outcomes keyed by ticker (ledger store).
    ticker_to_outcome: dict[str, tuple[int, Any, str]] = {}
    with paper_ledger.open_ledger(Path(ledger_path)) as conn:
        for plan in paper_ledger.fetch_plans_for_date(conn, brief_date, account=account):
            outcome = paper_ledger.fetch_outcome_for_plan(conn, plan["plan_id"])
            if outcome is None:
                continue  # plan still open — nothing to price yet
            fill_status = _EXIT_KIND_TO_FILL_STATUS.get(outcome["exit_kind"])
            if fill_status is None:
                logger.warning(
                    "shadow-return: unmapped exit_kind=%r (plan_id=%s) — skipping; "
                    "extend _EXIT_KIND_TO_FILL_STATUS.",
                    outcome["exit_kind"],
                    plan["plan_id"],
                )
                continue
            ticker_to_outcome[plan["ticker"].upper()] = (plan["plan_id"], outcome, fill_status)
    n_outcomes = len(ticker_to_outcome)

    # 2. Price each ticker (network leg). Per-ticker failures skip, never abort.
    priced: dict[str, tuple[float, float | None, int, str, str]] = {}
    n_skipped = 0
    n_no_bars = 0
    for ticker, (plan_id, outcome, fill_status) in ticker_to_outcome.items():
        try:
            arrival_bars = fetch(ticker, arrival_start, arrival_end)
            horizon_bars = fetch(ticker, horizon_start, horizon_end)
        except (PolygonError, ValueError, KeyError, TypeError) as exc:
            logger.warning("shadow-return: fetch failed for %s — skipping (%s)", ticker, exc)
            n_skipped += 1
            continue
        arrival_vwap = _window_vwap(arrival_bars, arrival_start, arrival_end)
        horizon_vwap = _window_vwap(horizon_bars, horizon_start, horizon_end)
        if arrival_vwap is None or horizon_vwap is None or arrival_vwap == 0:
            logger.warning(
                "shadow-return: no usable bars for %s in the arrival/horizon window — skipping.",
                ticker,
            )
            n_no_bars += 1
            continue
        shadow = (horizon_vwap - arrival_vwap) / arrival_vwap
        if abs(shadow) > IMPLAUSIBLE_RETURN_THRESHOLD:
            logger.warning(
                "shadow-return: implausible move %.3f for %s (likely a split/dividend in the "
                "window; bars are adjusted=false) — skipping rather than stamping.",
                shadow,
                ticker,
            )
            n_skipped += 1
            continue
        realized = _realized_return(outcome, fill_status)
        priced[ticker] = (shadow, realized, plan_id, outcome["exit_kind"], fill_status)

    # 3. Stamp each decision whose ticker was priced (feedback store).
    n_priced = 0
    with FeedbackStore.open(Path(feedback_path)) as fb:
        for decision in fb.list_by_brief_date(brief_date):
            match = priced.get(decision.ticker.upper())
            if match is None:
                continue
            shadow, realized, plan_id, exit_kind, fill_status = match
            fb.stamp_outcome(
                decision.id,
                fill_status=fill_status,
                exit_kind=exit_kind,
                outcome_plan_id=str(plan_id),
                outcome_computed_at=now,
                shadow_return=shadow,
                realized_return=realized,
            )
            n_priced += 1

    return ShadowReturnReport(
        brief_date=brief_date,
        account=account,
        matured=True,
        n_outcomes=n_outcomes,
        n_priced=n_priced,
        n_skipped=n_skipped,
        n_no_bars=n_no_bars,
    )


__all__ = [
    "ARRIVAL_VWAP_WINDOW_MIN",
    "HOLDING_HORIZON_TRADING_DAYS",
    "IMPLAUSIBLE_RETURN_THRESHOLD",
    "ShadowReturnReport",
    "compute_shadow_returns",
]
