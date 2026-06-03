"""Tests for the broker-free ladder-replay nightly driver (impure I/O layer).

The driver enumerates matured feedback DECISIONS (never the paper ledger), looks
the ladder up from the brief parquet, fetches minute bars via an injected
``bar_fetch`` stub, replays, and stamps the gen-4 ladder-outcome columns. These
tests pin: the maturity gate (un-matured dates skip), matured stamping, the
NO_STRUCTURE path (stamps a classification without crashing), and that the module
imports NO paper-ledger symbol (broker-free contract).
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_feedback.store import Decision, FeedbackStore
from alphalens_pipeline.feedback.ladder_backfill import replay_ladder_decisions_window

UTC = dt.UTC

# A clean OK trade setup: single entry, single TP, disaster stop below.
_OK_SETUP = {
    "status": "OK",
    "disaster_stop": 95.0,
    "atr": 2.0,
    "entry_tiers": [{"limit": 100.0, "alloc_pct": 100.0}],
    "tp_tranches": [{"target": 110.0, "tranche_pct": 100.0}],
}

_NO_STRUCTURE_SETUP = {"status": "NO_STRUCTURE", "disaster_stop": None, "entry_tiers": []}


def _write_brief(briefs_dir: Path, brief_date: dt.date, rows: list[dict]) -> None:
    frame_rows = []
    for r in rows:
        frame_rows.append(
            {
                "ticker": r["ticker"],
                "theme": r.get("theme", "ai"),
                "brief_trade_setup": json.dumps(r["setup"]) if r["setup"] is not None else None,
            }
        )
    df = pd.DataFrame(frame_rows)
    df.to_parquet(briefs_dir / f"{brief_date.isoformat()}.parquet")


def _insert_decision(fb: FeedbackStore, brief_date: dt.date, ticker: str, theme: str = "ai") -> str:
    row_id, _ = fb.insert(
        Decision(
            brief_date=brief_date,
            ticker=ticker,
            theme=theme,
            surfaced_at=dt.datetime(
                brief_date.year, brief_date.month, brief_date.day, 6, 30, tzinfo=UTC
            ),
            action="interested",
            action_at=dt.datetime(
                brief_date.year, brief_date.month, brief_date.day, 8, 0, tzinfo=UTC
            ),
        )
    )
    return row_id


class TestLadderBackfill(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.feedback_path = self.root / "feedback.db"
        self.briefs_dir = self.root / "briefs"
        self.briefs_dir.mkdir()

    def tearDown(self):
        self._td.cleanup()

    def _bars_for_tp(self, ticker, start, end):
        """A path that fills the entry then hits TP. Bars span the window."""
        base = int(start.timestamp() * 1000)
        minute = 60_000
        return [
            {"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000.0},
            {"t": base + minute, "o": 100.0, "h": 111.0, "l": 100.0, "c": 110.0, "v": 1000.0},
        ]

    def test_matured_decision_is_stamped(self):
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 6, 1, 7, 0, tzinfo=UTC)  # well past the 5-session horizon
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])
        with FeedbackStore.open(self.feedback_path) as fb:
            decision_id = _insert_decision(fb, brief_date, "NVDA")

        reports = replay_ladder_decisions_window(
            self.feedback_path,
            self.briefs_dir,
            end_date=now.date(),
            lookback_days=40,
            bar_fetch=self._bars_for_tp,
            now=now,
        )

        with FeedbackStore.open(self.feedback_path) as fb:
            row = fb.conn.execute(
                "SELECT ladder_classification, realized_r, blended_entry, sequence_str "
                "FROM decisions WHERE id = ?",
                (decision_id,),
            ).fetchone()
        self.assertIsNotNone(row["ladder_classification"])
        self.assertEqual(row["ladder_classification"], "TP_FULL")
        self.assertAlmostEqual(row["blended_entry"], 100.0, places=3)
        self.assertTrue(any(r.stamped > 0 for r in reports))

    def test_unmatured_decision_is_skipped(self):
        # brief_date so recent that the 5-session horizon has not closed yet.
        now = dt.datetime(2026, 6, 1, 7, 0, tzinfo=UTC)
        brief_date = now.date()  # horizon ends in the future
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])
        with FeedbackStore.open(self.feedback_path) as fb:
            decision_id = _insert_decision(fb, brief_date, "NVDA")

        reports = replay_ladder_decisions_window(
            self.feedback_path,
            self.briefs_dir,
            end_date=now.date(),
            lookback_days=2,
            bar_fetch=self._bars_for_tp,
            now=now,
        )

        with FeedbackStore.open(self.feedback_path) as fb:
            row = fb.conn.execute(
                "SELECT ladder_classification FROM decisions WHERE id = ?",
                (decision_id,),
            ).fetchone()
        self.assertIsNone(row["ladder_classification"])  # not stamped
        self.assertTrue(any(r.skipped_unmatured > 0 for r in reports))

    def test_no_structure_stamps_classification_without_crash(self):
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 6, 1, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "XYZ", "setup": _NO_STRUCTURE_SETUP}])
        with FeedbackStore.open(self.feedback_path) as fb:
            decision_id = _insert_decision(fb, brief_date, "XYZ")

        replay_ladder_decisions_window(
            self.feedback_path,
            self.briefs_dir,
            end_date=now.date(),
            lookback_days=40,
            bar_fetch=self._bars_for_tp,
            now=now,
        )

        with FeedbackStore.open(self.feedback_path) as fb:
            row = fb.conn.execute(
                "SELECT ladder_classification FROM decisions WHERE id = ?",
                (decision_id,),
            ).fetchone()
        self.assertEqual(row["ladder_classification"], "NO_STRUCTURE")

    def test_group_replayed_once_stamps_all_member_decisions(self):
        # Two decisions on the SAME (brief_date, ticker) but different themes →
        # one replay, both stamped.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 6, 1, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])
        with FeedbackStore.open(self.feedback_path) as fb:
            id_a = _insert_decision(fb, brief_date, "NVDA", theme="ai")
            id_b = _insert_decision(fb, brief_date, "NVDA", theme="datacenter")

        replay_ladder_decisions_window(
            self.feedback_path,
            self.briefs_dir,
            end_date=now.date(),
            lookback_days=40,
            bar_fetch=self._bars_for_tp,
            now=now,
        )

        with FeedbackStore.open(self.feedback_path) as fb:
            for did in (id_a, id_b):
                row = fb.conn.execute(
                    "SELECT ladder_classification FROM decisions WHERE id = ?", (did,)
                ).fetchone()
                self.assertEqual(row["ladder_classification"], "TP_FULL")

    def test_missing_brief_left_unstamped_for_retry(self):
        # zen MEDIUM: a missing brief is RETRYABLE — counted as no_data, no crash,
        # and the row is left NULL (NOT stamped NO_DATA) so the next sweep retries
        # it once the brief appears, instead of permanently abandoning it.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 6, 1, 7, 0, tzinfo=UTC)
        with FeedbackStore.open(self.feedback_path) as fb:
            decision_id = _insert_decision(fb, brief_date, "GONE")
        reports = replay_ladder_decisions_window(
            self.feedback_path,
            self.briefs_dir,
            end_date=now.date(),
            lookback_days=40,
            bar_fetch=self._bars_for_tp,
            now=now,
        )
        self.assertTrue(any(r.no_data > 0 for r in reports))
        with FeedbackStore.open(self.feedback_path) as fb:
            row = fb.conn.execute(
                "SELECT ladder_classification FROM decisions WHERE id = ?",
                (decision_id,),
            ).fetchone()
            self.assertIsNone(row["ladder_classification"])  # retryable, not stamped
            # still surfaced by the NULL-gate query for the next sweep
            pending = fb.iter_decisions_for_ladder(lookback_start=dt.date(2026, 4, 1))
            self.assertIn(decision_id, {r[0] for r in pending})

    def test_transient_fetch_error_left_unstamped_for_retry(self):
        # A Polygon outage (no bars / exception) must NOT poison the NULL-gate:
        # leave the row unstamped so the bounded sweep window retries it.
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 6, 1, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])
        with FeedbackStore.open(self.feedback_path) as fb:
            decision_id = _insert_decision(fb, brief_date, "NVDA")

        def _empty_fetch(ticker, start, end):
            return []  # transient: no bars returned

        reports = replay_ladder_decisions_window(
            self.feedback_path,
            self.briefs_dir,
            end_date=now.date(),
            lookback_days=40,
            bar_fetch=_empty_fetch,
            now=now,
        )
        self.assertTrue(any(r.no_data > 0 for r in reports))
        with FeedbackStore.open(self.feedback_path) as fb:
            row = fb.conn.execute(
                "SELECT ladder_classification FROM decisions WHERE id = ?",
                (decision_id,),
            ).fetchone()
            self.assertIsNone(row["ladder_classification"])  # retryable, not stamped


class TestBrokerFree(unittest.TestCase):
    """The broker-free contract: the driver must not IMPORT the paper ledger or
    CALL its plan/outcome enumeration. Uses an AST walk (not a substring scan)
    so the module docstring may explain the contract by naming the symbols."""

    def _module_path(self) -> Path:
        return (
            Path(__file__).resolve().parents[2]
            / "alphalens-pipeline"
            / "alphalens_pipeline"
            / "feedback"
            / "ladder_backfill.py"
        )

    def test_module_does_not_import_paper_ledger(self):
        import ast

        tree = ast.parse(self._module_path().read_text(encoding="utf-8"))
        forbidden_modules = {"alphalens_pipeline.paper.ledger"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name, forbidden_modules)
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                self.assertNotIn(module, forbidden_modules)
                # `from alphalens_pipeline.paper import ledger`
                if module == "alphalens_pipeline.paper":
                    self.assertNotIn(
                        "ledger", {a.name for a in node.names}, "must not import the paper ledger"
                    )

    def test_module_does_not_call_broker_enumeration(self):
        import ast

        forbidden_calls = {
            "fetch_plans_for_date",
            "fetch_outcome_for_plan",
            "compute_shadow_returns",
        }
        tree = ast.parse(self._module_path().read_text(encoding="utf-8"))
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name:
                    called.add(name)
        self.assertEqual(called & forbidden_calls, set())


if __name__ == "__main__":
    unittest.main()
