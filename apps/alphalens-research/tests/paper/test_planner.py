"""End-to-end planner tests with injected dependencies.

Builds candidate briefs in-memory (skipping parquet I/O — that is covered by
``test_brief_loader.py``) and exercises the planner against a fresh tmp
SQLite ledger per test. The :class:`PositionChecker` + :class:`EquityProvider`
Protocols are stubbed so the planner runs without Alpaca creds.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.paper.brief_loader import CandidateBrief
from alphalens_pipeline.paper.ledger import (
    fetch_plans_for_date,
    fetch_shadow_for_date,
    open_ledger,
)
from alphalens_pipeline.paper.planner import plan_for_date


def _setup_dict(*, status="OK", suggested=5.0, disaster_stop=80.0, tier_alloc=(100.0,)) -> dict:
    return {
        "schema_version": "1.0.0",
        "status": status,
        "asof_close": 100.0,
        "atr": 1.5,
        "disaster_stop": disaster_stop,
        "suggested_size_pct": suggested,
        "order_ttl_days": 10,
        "entry_tiers": [
            {"limit": 100.0, "alloc_pct": alloc, "atr_distance": 0.0, "tag": f"t{i}"}
            for i, alloc in enumerate(tier_alloc)
        ],
        "tp_tranches": [{"target": 110.0, "tranche_pct": 100.0, "r_multiple": 1.0, "tag": "tp"}],
    }


def _make_candidate(
    *,
    ticker="NVDA",
    theme="ai",
    verified=True,
    setup=None,
    brief_date=dt.date(2026, 5, 28),
) -> CandidateBrief:
    setup = _setup_dict() if setup is _SENTINEL else setup
    suggested = (
        float(setup["suggested_size_pct"])
        if setup is not None and setup.get("suggested_size_pct") is not None
        else None
    )
    return CandidateBrief(
        brief_date=brief_date,
        ticker=ticker,
        theme=theme,
        verified=verified,
        suggested_size_pct=suggested,
        trade_setup=setup,
        n_gates_passed=4,
        n_gates_failed=0,
        layer4_weighted_score=15.0,
    )


_SENTINEL = object()


class _FixedEquity:
    def __init__(self, equity: float) -> None:
        self._equity = equity

    def get_paper_equity(self) -> float:
        return self._equity


class _SetPositionChecker:
    """Reports a position as open iff the ticker is in the provided set."""

    def __init__(self, open_set: set[str]) -> None:
        self._open = open_set

    def has_open_position(self, symbol: str) -> bool:
        return symbol in self._open


class TestHappyPath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.ledger = self.tmpdir / "ledger.db"

    def tearDown(self):
        self._tmp.cleanup()

    def test_verified_candidate_with_ok_setup_plans(self):
        candidate = _make_candidate(setup=_setup_dict())

        report = plan_for_date(
            brief_date=candidate.brief_date,
            briefs_dir=self.tmpdir,  # unused — candidates provided
            ledger_path=self.ledger,
            candidates=[candidate],
            position_checker=_SetPositionChecker(set()),
            equity_provider=_FixedEquity(1_000_000.0),
        )

        self.assertEqual(report.n_planned, 1)
        self.assertEqual(report.n_shadowed, 0)
        self.assertEqual(report.paper_equity, 1_000_000.0)

        with open_ledger(self.ledger) as conn:
            plans = fetch_plans_for_date(conn, candidate.brief_date)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["ticker"], "NVDA")
        self.assertEqual(plans[0]["status"], "PLANNED")
        # v2 sizing: one candidate at suggested 5%, equity $1M:
        #   aggregate = 0.05 × 1M = $50k
        #   daily_target = STEADY_STATE_GROSS_FRAC × 1M / EXPECTED_AVG_HOLD_DAYS
        #   scale_factor = daily_target / aggregate
        from alphalens_pipeline.paper.constants import (
            EXPECTED_AVG_HOLD_DAYS,
            STEADY_STATE_GROSS_FRAC,
        )

        expected_scale = (STEADY_STATE_GROSS_FRAC * 1_000_000.0 / EXPECTED_AVG_HOLD_DAYS) / (
            0.05 * 1_000_000.0
        )
        self.assertAlmostEqual(plans[0]["suggested_size_pct"], 5.0, places=8)
        self.assertAlmostEqual(plans[0]["scale_factor"], expected_scale, places=8)
        self.assertAlmostEqual(plans[0]["final_size_pct"], 5.0 * expected_scale, places=8)


class TestShadowReasons(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.ledger = self.tmpdir / "ledger.db"
        self.d = dt.date(2026, 5, 28)

    def tearDown(self):
        self._tmp.cleanup()

    def _plan_with(self, candidates, *, open_set=None):
        return plan_for_date(
            brief_date=self.d,
            briefs_dir=self.tmpdir,
            ledger_path=self.ledger,
            candidates=candidates,
            position_checker=_SetPositionChecker(open_set or set()),
            equity_provider=_FixedEquity(1_000_000.0),
        )

    def _shadow_reasons(self):
        with open_ledger(self.ledger) as conn:
            return [row["reason"] for row in fetch_shadow_for_date(conn, self.d)]

    def test_not_verified_candidate_shadowed(self):
        c = _make_candidate(ticker="X", verified=False, setup=_setup_dict())
        report = self._plan_with([c])
        self.assertEqual(report.n_shadowed, 1)
        self.assertIn("not_verified", self._shadow_reasons())

    def test_missing_trade_setup_shadowed(self):
        c = _make_candidate(ticker="X", verified=True, setup=None)
        self._plan_with([c])
        self.assertIn("no_trade_setup", self._shadow_reasons())

    def test_no_structure_setup_shadowed_with_reason(self):
        c = _make_candidate(ticker="X", setup=_setup_dict(status="NO_STRUCTURE"))
        self._plan_with([c])
        self.assertIn("unplannable_setup", self._shadow_reasons())

    def test_same_ticker_open_skips_and_shadows(self):
        c = _make_candidate(ticker="NVDA", setup=_setup_dict())
        self._plan_with([c], open_set={"NVDA"})
        self.assertIn("same_ticker_open", self._shadow_reasons())

    def test_duplicate_ticker_within_same_brief_shadowed_cleanly(self):
        """Real-world case: same ticker appears under TWO different themes in
        one day's brief (e.g. NVDA in 'ai-infra' AND 'datacenter-buildout').
        The ``UNIQUE(brief_date, ticker)`` constraint would crash the run
        with IntegrityError on the second occurrence — instead the second
        one must shadow-log cleanly so the rest of the brief still planss."""
        nvda_ai = _make_candidate(ticker="NVDA", theme="ai-infra", setup=_setup_dict())
        nvda_dc = _make_candidate(ticker="NVDA", theme="datacenter-buildout", setup=_setup_dict())
        third = _make_candidate(ticker="AVGO", theme="ai-infra", setup=_setup_dict())

        report = self._plan_with([nvda_ai, nvda_dc, third])

        # First NVDA + AVGO planned; second NVDA shadowed cleanly.
        self.assertEqual(report.n_planned, 2)
        self.assertEqual(report.n_shadowed, 1)
        self.assertIn("duplicate_ticker_in_brief", self._shadow_reasons())

        # Order matters: only the FIRST occurrence got a PLANNED row.
        outcomes_by_index = list(report.outcomes)
        self.assertEqual(outcomes_by_index[0].status, "PLANNED")  # NVDA / ai-infra
        self.assertEqual(outcomes_by_index[1].status, "SHADOWED")  # NVDA / datacenter
        self.assertEqual(outcomes_by_index[1].reason, "duplicate_ticker_in_brief")
        self.assertEqual(outcomes_by_index[2].status, "PLANNED")  # AVGO

    def test_duplicate_ticker_when_first_was_shadowed_still_plans_next_if_unique(self):
        """A duplicate must not be counted as 'already planned' if the FIRST
        occurrence shadowed (e.g. not_verified). Otherwise we'd lose the
        second occurrence to dedup when there's nothing to dedup against."""
        # First NVDA: not verified → shadow
        nvda_bad = _make_candidate(
            ticker="NVDA", theme="ai-infra", verified=False, setup=_setup_dict()
        )
        # Second NVDA: verified + plannable → should PLAN (first was shadowed,
        # not planned, so set membership skips it)
        nvda_good = _make_candidate(
            ticker="NVDA", theme="datacenter-buildout", verified=True, setup=_setup_dict()
        )

        report = self._plan_with([nvda_bad, nvda_good])
        self.assertEqual(report.n_planned, 1)
        self.assertEqual(report.n_shadowed, 1)
        self.assertIn("not_verified", self._shadow_reasons())
        self.assertNotIn("duplicate_ticker_in_brief", self._shadow_reasons())

    def test_gross_cap_block(self):
        """Gross cap binds when cumulative planned gross would exceed
        ``GROSS_SAFETY_FRAC × equity``. Forces the bind by monkey-patching
        the frac to a tiny value so two normal candidates exceed it together.

        v2 math: 2 candidates × suggested 5% on $1M equity. Aggregate
        uncapped = $100k; daily_target = 0.667 × $1M / 30 ≈ $22.2k;
        scale_factor = 22.2k / 100k = 0.222; per-cand final 1.11%, total
        notional $11.1k. Tier (limit=$100, alloc=100%): qty=111, gross
        $11,100. Set GROSS_SAFETY_FRAC so the cap sits between 1× and 2×
        per-cand gross ($11.1k < cap < $22.2k).
        """
        from alphalens_pipeline.paper import planner

        c1 = _make_candidate(ticker="A", setup=_setup_dict())
        c2 = _make_candidate(ticker="B", setup=_setup_dict())

        original_frac = planner.GROSS_SAFETY_FRAC
        try:
            planner.GROSS_SAFETY_FRAC = 0.012  # cap = $12_000 at $1M equity
            report = plan_for_date(
                brief_date=self.d,
                briefs_dir=self.tmpdir,
                ledger_path=self.ledger,
                candidates=[c1, c2],
                position_checker=_SetPositionChecker(set()),
                equity_provider=_FixedEquity(1_000_000.0),
            )
        finally:
            planner.GROSS_SAFETY_FRAC = original_frac

        # Exactly one planned, one blocked.
        self.assertEqual(report.n_planned, 1)
        self.assertEqual(report.n_shadowed, 1)
        self.assertIn("gross_cap_block", self._shadow_reasons())


class TestForceRerun(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.ledger = self.tmpdir / "ledger.db"
        self.d = dt.date(2026, 5, 28)

    def tearDown(self):
        self._tmp.cleanup()

    def test_repeat_without_force_fails_on_unique_constraint(self):
        import sqlite3

        c = _make_candidate(setup=_setup_dict())

        # First run: success.
        plan_for_date(
            brief_date=self.d,
            briefs_dir=self.tmpdir,
            ledger_path=self.ledger,
            candidates=[c],
            position_checker=_SetPositionChecker(set()),
            equity_provider=_FixedEquity(1_000_000.0),
        )
        # Second run without --force: ledger insert hits UNIQUE constraint.
        with self.assertRaises(sqlite3.IntegrityError):
            plan_for_date(
                brief_date=self.d,
                briefs_dir=self.tmpdir,
                ledger_path=self.ledger,
                candidates=[c],
                position_checker=_SetPositionChecker(set()),
                equity_provider=_FixedEquity(1_000_000.0),
            )

    def test_force_purges_existing_rows_and_replans(self):
        c = _make_candidate(setup=_setup_dict())

        plan_for_date(
            brief_date=self.d,
            briefs_dir=self.tmpdir,
            ledger_path=self.ledger,
            candidates=[c],
            position_checker=_SetPositionChecker(set()),
            equity_provider=_FixedEquity(1_000_000.0),
        )
        report2 = plan_for_date(
            brief_date=self.d,
            briefs_dir=self.tmpdir,
            ledger_path=self.ledger,
            candidates=[c],
            position_checker=_SetPositionChecker(set()),
            equity_provider=_FixedEquity(1_000_000.0),
            force=True,
        )
        self.assertEqual(report2.n_planned, 1)


class TestMixedBrief(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.ledger = self.tmpdir / "ledger.db"
        self.d = dt.date(2026, 5, 28)

    def tearDown(self):
        self._tmp.cleanup()

    def test_one_verified_one_skipped_one_blocked(self):
        verified = _make_candidate(ticker="A", verified=True, setup=_setup_dict())
        skipped_unverified = _make_candidate(ticker="B", verified=False, setup=_setup_dict())
        skipped_dedup = _make_candidate(ticker="C", verified=True, setup=_setup_dict())

        report = plan_for_date(
            brief_date=self.d,
            briefs_dir=self.tmpdir,
            ledger_path=self.ledger,
            candidates=[verified, skipped_unverified, skipped_dedup],
            position_checker=_SetPositionChecker({"C"}),
            equity_provider=_FixedEquity(1_000_000.0),
        )
        self.assertEqual(report.n_planned, 1)
        self.assertEqual(report.n_shadowed, 2)
        outcomes = {o.ticker: o for o in report.outcomes}
        self.assertEqual(outcomes["A"].status, "PLANNED")
        self.assertEqual(outcomes["B"].reason, "not_verified")
        self.assertEqual(outcomes["C"].reason, "same_ticker_open")


if __name__ == "__main__":
    unittest.main()
