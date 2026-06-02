"""Join feedback decisions to their paper-trade plan outcomes (Track A v2).

The paper-trade harness auto-submits every verified brief candidate on its
own schedule (plan / submit / reconcile cron), INDEPENDENT of any user
Interested/Dismissed click — it never reads the feedback ledger. So a
decision and its paper outcome are parallel inputs that must be linked
POST-HOC. This module is that link.

Join key
--------
``(brief_date, ticker, account)``. The paper ``plans`` table is
``UNIQUE(brief_date, ticker, account)`` with NO ``theme`` column, whereas
decisions are ``UNIQUE(brief_date, ticker, theme)``. So the outcome grain is
per-ticker-day: if the same ticker is surfaced under two themes the same day,
BOTH decisions take the same plan outcome. ``account`` defaults to ``"test"``
because the live VPS paper chain runs on the Alpaca test account
(``alphalens paper plan/submit --use-test-account``).

What is stamped
---------------
``fill_status`` (the §4 FILLED / UNFILLED / PARTIAL distinction so
never-filled candidates are recorded, not dropped — fill-only learning is
adversely selected per Glosten/Linnainmaa), ``exit_kind`` (verbatim paper
disposition), ``outcome_plan_id`` (the joined ``plans.plan_id``) and
``outcome_computed_at`` (last-joined-at provenance). ``shadow_return`` +
``realized_return`` are deliberately left untouched here — they are filled by
the separate PR-3 ``shadow_return.compute_shadow_returns`` pass (minute-bar
arrival-price counterfactual), which this cheap fill-status join never reads.

Decoupled no-match is normal
----------------------------
A clicked candidate may have no paper plan at all, or a plan that has not yet
closed (no ``plan_outcomes`` row). Both cases leave the decision's outcome
columns NULL and are NOT errors. But if a sweep finds decisions yet ZERO
plans for the requested account, that usually means a misconfigured account
(or a dead paper chain) producing silent all-NULL outcomes — so it logs a
WARNING rather than failing quietly.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from alphalens_feedback.store import FeedbackStore

from alphalens_pipeline.paper import ledger as paper_ledger

logger = logging.getLogger(__name__)

# Paper ``plan_outcomes.exit_kind`` -> feedback ``fill_status``. Every
# terminal exit other than UNFILLED implies the entry filled; PARTIAL_TP is
# a partial fill (some tranches hit). Public so the shadow-return pass shares
# the single mapping rather than coupling to a private name (zen pre-merge).
EXIT_KIND_TO_FILL_STATUS: dict[str, str] = {
    "TP_HIT": "FILLED",
    "SL_HIT": "FILLED",
    "TIME_STOP_HIT": "FILLED",
    "PARTIAL_TP": "PARTIAL",
    "UNFILLED": "UNFILLED",
}


@dataclass(frozen=True)
class JoinReport:
    """Summary of one outcome-join sweep over a single brief date."""

    brief_date: dt.date
    account: str
    n_decisions: int
    n_matched: int  # decisions stamped with a matured outcome
    n_unmatched: int  # decisions left NULL (no plan, or plan not yet closed)
    n_plans: int  # plans found for (brief_date, account)


def join_decision_outcomes(
    feedback_path: Path,
    ledger_path: Path,
    *,
    brief_date: dt.date,
    account: str = "test",
    now: dt.datetime | None = None,
) -> JoinReport:
    """Stamp paper outcomes onto every decision for ``brief_date``.

    Idempotent and re-runnable: stamping is a targeted UPDATE, so a re-run
    with the same matured outcomes is a no-op (modulo ``outcome_computed_at``,
    which tracks last-joined-at). The two stores are opened sequentially (the
    ledger first, then the feedback store) — never nested — so the advisory
    file locks do not deadlock.
    """
    now = now or dt.datetime.now(dt.UTC)

    # 1. Read paper plans + their (optional) matured outcomes, keyed by ticker.
    ticker_to_outcome: dict[str, tuple[int, str]] = {}
    with paper_ledger.open_ledger(Path(ledger_path)) as conn:
        plans = paper_ledger.fetch_plans_for_date(conn, brief_date, account=account)
        n_plans = len(plans)
        for plan in plans:
            outcome = paper_ledger.fetch_outcome_for_plan(conn, plan["plan_id"])
            if outcome is None:
                continue  # plan still open — leave the decision NULL for now
            # Normalise ticker case: decisions are stored uppercase (Django
            # forces ``.upper()`` on POST) but the planner persists the ticker
            # verbatim, so match case-insensitively to avoid a silent miss.
            ticker_to_outcome[plan["ticker"].upper()] = (plan["plan_id"], outcome["exit_kind"])

    # 2. Stamp each decision whose ticker has a matured outcome.
    n_matched = 0
    with FeedbackStore.open(Path(feedback_path)) as fb:
        decisions = fb.list_by_brief_date(brief_date)
        n_decisions = len(decisions)
        # TODO(#388, NON-OPTIONAL before a 2nd broker or n>=50 decisions):
        # the feedback ledger has no `platform` column yet. A broker switch is
        # an EXECUTION-REGIME break — realized_return / fill-rate must NEVER be
        # pooled across platforms (see
        # docs/research/feedback_ledger_counterfactual_design_2026_06_02.md).
        # Wire a platform column + stratify execution_modes by it when the 2nd
        # paper platform lands OR n reaches 50. Plans-side platform (this PR)
        # is sufficient until then because feedback.db is empty + execution_modes
        # is INERT below n=50.
        for decision in decisions:
            match = ticker_to_outcome.get(decision.ticker.upper())
            if match is None:
                continue
            plan_id, exit_kind = match
            fill_status = EXIT_KIND_TO_FILL_STATUS.get(exit_kind)
            if fill_status is None:
                # Defensive against a future paper-reconciler exit kind not yet
                # mapped here: skip + warn rather than KeyError-halt mid-sweep
                # (which would leave the day partially joined).
                logger.warning(
                    "outcome-join: unmapped exit_kind=%r (plan_id=%s) — "
                    "skipping decision %s; extend EXIT_KIND_TO_FILL_STATUS.",
                    exit_kind,
                    plan_id,
                    decision.id,
                )
                continue
            fb.stamp_outcome(
                decision.id,
                fill_status=fill_status,
                exit_kind=exit_kind,
                outcome_plan_id=str(plan_id),
                outcome_computed_at=now,
            )
            n_matched += 1

    if n_decisions and n_plans == 0:
        logger.warning(
            "outcome-join: %d decision(s) for %s but ZERO plans on account=%r "
            "(wrong account? the live chain uses 'test') — all outcomes left NULL.",
            n_decisions,
            brief_date.isoformat(),
            account,
        )

    return JoinReport(
        brief_date=brief_date,
        account=account,
        n_decisions=n_decisions,
        n_matched=n_matched,
        n_unmatched=n_decisions - n_matched,
        n_plans=n_plans,
    )


__all__ = ["EXIT_KIND_TO_FILL_STATUS", "JoinReport", "join_decision_outcomes"]
