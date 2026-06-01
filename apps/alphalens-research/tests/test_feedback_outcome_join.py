"""Tests for the Track A v2 feedback<->paper outcome-join.

The paper harness auto-submits every verified candidate independent of
any user click (decoupled), so a decision is linked to its paper plan
outcome POST-HOC by ``(brief_date, ticker, account)``. The join stamps
``fill_status`` / ``exit_kind`` / ``outcome_plan_id`` / ``outcome_computed_at``
onto the decision row. ``shadow_return`` + ``realized_pnl`` are left for
the deferred PR-3 (no minute-bar arrival-price source exists yet).

Design intent: ``docs/research/alphalens_ideal_shape_2026_05_29.md`` §4 +
§8 L3; issue #165 v2 scope comment.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.feedback.outcome_join import join_decision_outcomes
from alphalens_pipeline.feedback.store import Decision, FeedbackStore
from alphalens_pipeline.paper import ledger as paper_ledger

UTC = dt.UTC
_BRIEF_DATE = dt.date(2026, 5, 28)
_NOW = dt.datetime(2026, 6, 1, 21, 30, tzinfo=UTC)


def _seed_decision(path: Path, *, ticker: str = "NVDA", theme: str = "ai") -> str:
    with FeedbackStore.open(path) as fb:
        row_id, _ = fb.insert(
            Decision(
                brief_date=_BRIEF_DATE,
                ticker=ticker,
                theme=theme,
                surfaced_at=dt.datetime(2026, 5, 28, 6, 30, tzinfo=UTC),
                action="interested",
                action_at=dt.datetime(2026, 5, 28, 8, 0, tzinfo=UTC),
            )
        )
    return row_id


def _seed_plan(
    path: Path,
    *,
    ticker: str = "NVDA",
    theme: str = "ai",
    account: str = "test",
    exit_kind: str | None = "TP_HIT",
) -> int:
    """Seed a paper plan (+ optional plan_outcome). Returns plan_id."""
    with paper_ledger.open_ledger(path) as conn:
        plan = paper_ledger.insert_planned(
            conn,
            brief_date=_BRIEF_DATE,
            ticker=ticker,
            theme=theme,
            planned_at=dt.datetime(2026, 5, 28, 13, 5, tzinfo=UTC),
            suggested_size_pct=2.0,
            scale_factor=1.0,
            final_size_pct=2.0,
            paper_equity=100_000.0,
            total_notional=2_000.0,
            gross_notional=2_000.0,
            disaster_stop=90.0,
            order_ttl_days=2,
            tiers=[(0, 100.0, 20, 100.0, "entry")],
            tp_tranches=[(0, 120.0, 100.0, 2.0, "tp")],
            account=account,
        )
        if exit_kind is not None:
            paper_ledger.insert_plan_outcome(
                conn,
                plan_id=plan.plan_id,
                exit_kind=exit_kind,
                closed_at=dt.datetime(2026, 5, 30, 20, 0, tzinfo=UTC),
            )
        return plan.plan_id


class TestOutcomeJoin(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.fb_path = Path(self._td.name) / "feedback.db"
        self.ledger_path = Path(self._td.name) / "paper_ledger.db"

    def tearDown(self):
        self._td.cleanup()

    def _join(self, account: str = "test"):
        return join_decision_outcomes(
            self.fb_path, self.ledger_path, brief_date=_BRIEF_DATE, account=account, now=_NOW
        )

    def _fetch(self, row_id: str) -> Decision:
        with FeedbackStore.open(self.fb_path) as fb:
            return fb.get(row_id)

    def test_filled_plan_stamps_fill_status_and_exit_kind(self):
        row_id = _seed_decision(self.fb_path)
        plan_id = _seed_plan(self.ledger_path, exit_kind="TP_HIT")
        report = self._join()
        self.assertEqual(report.n_matched, 1)
        d = self._fetch(row_id)
        self.assertEqual(d.fill_status, "FILLED")
        self.assertEqual(d.exit_kind, "TP_HIT")
        self.assertEqual(d.outcome_plan_id, str(plan_id))
        self.assertEqual(d.outcome_computed_at, _NOW)
        # PR-1 defers return computation
        self.assertIsNone(d.shadow_return)
        self.assertIsNone(d.realized_pnl)

    def test_unfilled_plan_stamps_unfilled(self):
        # The §4 never-filled candidate (limit never reached) must be
        # captured, not dropped — that is the whole point of the join.
        row_id = _seed_decision(self.fb_path)
        _seed_plan(self.ledger_path, exit_kind="UNFILLED")
        self._join()
        self.assertEqual(self._fetch(row_id).fill_status, "UNFILLED")

    def test_partial_tp_stamps_partial(self):
        row_id = _seed_decision(self.fb_path)
        _seed_plan(self.ledger_path, exit_kind="PARTIAL_TP")
        self._join()
        d = self._fetch(row_id)
        self.assertEqual(d.fill_status, "PARTIAL")
        self.assertEqual(d.exit_kind, "PARTIAL_TP")

    def test_no_matching_plan_leaves_outcome_null(self):
        # Paper is decoupled — a clicked candidate may have no plan at all.
        row_id = _seed_decision(self.fb_path)
        report = self._join()
        self.assertEqual(report.n_matched, 0)
        self.assertEqual(report.n_plans, 0)
        d = self._fetch(row_id)
        self.assertIsNone(d.fill_status)
        self.assertIsNone(d.outcome_plan_id)

    def test_open_plan_without_outcome_leaves_null_then_stamps_when_matured(self):
        # Maturation path: a plan exists but hasn't closed yet (no
        # plan_outcomes row) -> decision stays NULL; once the reconciler
        # writes the outcome, a later join run stamps it.
        row_id = _seed_decision(self.fb_path)
        _seed_plan(self.ledger_path, exit_kind=None)  # plan only, no outcome
        self._join()
        self.assertIsNone(self._fetch(row_id).fill_status)
        # outcome matures
        with paper_ledger.open_ledger(self.ledger_path) as conn:
            plan = paper_ledger.fetch_plans_for_date(conn, _BRIEF_DATE, account="test")[0]
            paper_ledger.insert_plan_outcome(
                conn,
                plan_id=plan["plan_id"],
                exit_kind="SL_HIT",
                closed_at=dt.datetime(2026, 5, 31, 20, 0, tzinfo=UTC),
            )
        self._join()
        d = self._fetch(row_id)
        self.assertEqual(d.fill_status, "FILLED")
        self.assertEqual(d.exit_kind, "SL_HIT")

    def test_join_is_idempotent(self):
        row_id = _seed_decision(self.fb_path)
        _seed_plan(self.ledger_path, exit_kind="TP_HIT")
        self._join()
        first = self._fetch(row_id)
        self._join()  # re-run with the same fixed `now`
        second = self._fetch(row_id)
        self.assertEqual(first.fill_status, second.fill_status)
        self.assertEqual(first.exit_kind, second.exit_kind)
        self.assertEqual(first.outcome_plan_id, second.outcome_plan_id)
        self.assertEqual(first.outcome_computed_at, second.outcome_computed_at)
        # still exactly one decision row
        with FeedbackStore.open(self.fb_path) as fb:
            self.assertEqual(len(fb.list_by_brief_date(_BRIEF_DATE)), 1)

    def test_two_themes_same_ticker_both_get_same_outcome(self):
        # decisions are keyed per (brief_date, ticker, theme); plans are
        # per (brief_date, ticker, account) with NO theme. Same ticker under
        # two themes => two decisions, one plan. Both decisions must take the
        # same per-ticker-day outcome (a conscious grain choice).
        id_a = _seed_decision(self.fb_path, ticker="NVDA", theme="ai_infrastructure")
        id_b = _seed_decision(self.fb_path, ticker="NVDA", theme="gpu_shortage")
        _seed_plan(self.ledger_path, ticker="NVDA", exit_kind="TP_HIT")
        report = self._join()
        self.assertEqual(report.n_matched, 2)
        for row_id in (id_a, id_b):
            d = self._fetch(row_id)
            self.assertEqual(d.fill_status, "FILLED")
            self.assertEqual(d.exit_kind, "TP_HIT")

    def test_account_scoping_main_plan_not_joined_with_test(self):
        # Live VPS chain runs account='test'. A plan under 'main' must NOT
        # be joined when the sweep is scoped to 'test' — documents the
        # account-scoping trap as a conscious choice (no silent cross-join).
        row_id = _seed_decision(self.fb_path)
        _seed_plan(self.ledger_path, account="main", exit_kind="TP_HIT")
        report = self._join(account="test")
        self.assertEqual(report.n_plans, 0)
        self.assertIsNone(self._fetch(row_id).fill_status)

    def test_warns_when_decisions_exist_but_no_plans(self):
        # Misconfigured account / dead paper chain = all-NULL outcomes with
        # zero error. Surface a WARNING rather than failing silently.
        _seed_decision(self.fb_path)
        with self.assertLogs("alphalens_pipeline.feedback.outcome_join", level="WARNING") as cm:
            self._join(account="test")
        self.assertTrue(any("ZERO plans" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main()
