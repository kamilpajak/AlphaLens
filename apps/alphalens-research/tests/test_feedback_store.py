"""Tests for the feedback ledger primitives.

Design memo: ``docs/research/feedback_ledger_design_2026_05_29.md``.
Schema decisions (LOCKED): 5-action enum, 2-level dismiss taxonomy
(4 categories × 3 reasons + other), UNIQUE(brief_date, ticker, theme),
VIX-only market regime.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.feedback import regime
from alphalens_pipeline.feedback.store import (
    DISMISS_TAXONOMY,
    Decision,
    DecisionValidationError,
    FeedbackStore,
)

UTC = dt.UTC


def _make_decision(**overrides) -> Decision:
    """Build a baseline `interested` Decision; overrides applied last."""
    defaults = {
        "brief_date": dt.date(2026, 5, 28),
        "ticker": "NVDA",
        "theme": "ai_infrastructure",
        "surfaced_at": dt.datetime(2026, 5, 28, 6, 30, tzinfo=UTC),
        "action": "interested",
        "action_at": dt.datetime(2026, 5, 28, 8, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Decision(**defaults)


class TestFeedbackStoreSchema(unittest.TestCase):
    """Schema bootstrap is idempotent and survives reopen."""

    def test_open_creates_schema_on_fresh_db(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "feedback.db"
            with FeedbackStore.open(path) as fb:
                # decisions table exists with the expected columns
                rows = list(fb.conn.execute("PRAGMA table_info(decisions)"))
                col_names = {r[1] for r in rows}
                self.assertIn("id", col_names)
                self.assertIn("brief_date", col_names)
                self.assertIn("ticker", col_names)
                self.assertIn("dismiss_category", col_names)
                self.assertIn("dismiss_reason", col_names)
                self.assertIn("position_size_usd", col_names)
                self.assertIn("market_regime_at_entry", col_names)

    def test_open_is_idempotent_on_existing_db(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "feedback.db"
            with FeedbackStore.open(path) as fb:
                fb.insert(_make_decision())
            # second open must not raise + must preserve data
            with FeedbackStore.open(path) as fb:
                rows = fb.list_by_brief_date(dt.date(2026, 5, 28))
                self.assertEqual(len(rows), 1)


class TestDecisionValidation(unittest.TestCase):
    """Pair-integrity and field-level rules enforced in __post_init__."""

    def test_action_must_be_in_enum(self):
        with self.assertRaises(DecisionValidationError):
            _make_decision(action="bookmark")

    def test_dismissed_requires_category_and_reason(self):
        with self.assertRaises(DecisionValidationError):
            _make_decision(action="dismissed")
        with self.assertRaises(DecisionValidationError):
            _make_decision(action="dismissed", dismiss_category="thesis_setup")
        # category + reason → OK
        d = _make_decision(
            action="dismissed",
            dismiss_category="thesis_setup",
            dismiss_reason="wrong_theme",
        )
        self.assertEqual(d.dismiss_category, "thesis_setup")

    def test_non_dismissed_must_not_have_dismiss_fields(self):
        with self.assertRaises(DecisionValidationError):
            _make_decision(action="interested", dismiss_category="thesis_setup")
        with self.assertRaises(DecisionValidationError):
            _make_decision(action="watching", dismiss_reason="wrong_theme")

    def test_dismiss_reason_must_match_category(self):
        # `wrong_theme` belongs to `thesis_setup`; pairing with `risk_quality` fails
        with self.assertRaises(DecisionValidationError):
            _make_decision(
                action="dismissed",
                dismiss_category="risk_quality",
                dismiss_reason="wrong_theme",
            )

    def test_dismiss_other_requires_note(self):
        with self.assertRaises(DecisionValidationError):
            _make_decision(action="dismissed", dismiss_category="other", dismiss_reason="other")
        # with note → OK
        d = _make_decision(
            action="dismissed",
            dismiss_category="other",
            dismiss_reason="other",
            dismiss_note="thinly traded but interesting",
        )
        self.assertEqual(d.dismiss_note, "thinly traded but interesting")

    def test_confidence_subjective_must_be_1_to_5_when_present(self):
        with self.assertRaises(DecisionValidationError):
            _make_decision(confidence_subjective=0)
        with self.assertRaises(DecisionValidationError):
            _make_decision(confidence_subjective=6)
        # None and 1..5 OK
        for v in (None, 1, 3, 5):
            _make_decision(confidence_subjective=v)

    def test_position_size_usd_only_for_live_traded(self):
        # interested/watching/dismissed/paper_traded → must be None
        with self.assertRaises(DecisionValidationError):
            _make_decision(action="interested", position_size_usd=10_000.0)
        # live_traded → allowed
        d = _make_decision(
            action="live_traded",
            position_size_usd=10_000.0,
            entry_price=145.50,
        )
        self.assertEqual(d.position_size_usd, 10_000.0)

    def test_taxonomy_constant_covers_all_locked_pairs(self):
        # Sanity check that DISMISS_TAXONOMY exposes exactly the locked structure.
        # If we add/remove a reason, this test must fail loudly so callers
        # (Django serializer, SPA dropdown) get a coordinated update.
        self.assertEqual(
            set(DISMISS_TAXONOMY.keys()),
            {"thesis_setup", "risk_quality", "portfolio_style", "other"},
        )
        self.assertEqual(
            DISMISS_TAXONOMY["thesis_setup"],
            ("wrong_theme", "too_expensive", "bad_setup"),
        )
        self.assertEqual(
            DISMISS_TAXONOMY["risk_quality"],
            ("business_management", "risk_jurisdiction", "dont_understand"),
        )
        self.assertEqual(
            DISMISS_TAXONOMY["portfolio_style"],
            ("already_have_exposure", "liquidity_too_low", "not_my_style"),
        )
        self.assertEqual(DISMISS_TAXONOMY["other"], ("other",))


class TestFeedbackStoreCRUD(unittest.TestCase):
    """End-to-end persistence; ephemeral SQLite per test."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "feedback.db"

    def tearDown(self):
        self._td.cleanup()

    def test_insert_returns_id_and_persists_round_trip(self):
        d = _make_decision()
        with FeedbackStore.open(self.path) as fb:
            row_id = fb.insert(d)
            fetched = fb.get(row_id)
            self.assertEqual(fetched.ticker, "NVDA")
            self.assertEqual(fetched.action, "interested")
            self.assertEqual(fetched.brief_date, dt.date(2026, 5, 28))

    def test_insert_idempotent_on_unique_key_overwrites_prior(self):
        # Same (brief_date, ticker, theme) → second insert replaces first.
        # Variant A means NVDA × "ai_infrastructure" is one decision; user
        # changing their mind from interested → dismissed must update, not
        # raise UniqueConstraint.
        d1 = _make_decision(action="interested")
        d2 = _make_decision(
            action="dismissed",
            dismiss_category="thesis_setup",
            dismiss_reason="too_expensive",
        )
        with FeedbackStore.open(self.path) as fb:
            fb.insert(d1)
            fb.insert(d2)
            rows = fb.list_by_brief_date(dt.date(2026, 5, 28))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].action, "dismissed")
            self.assertEqual(rows[0].dismiss_reason, "too_expensive")

    def test_variant_a_uniqueness_allows_same_ticker_under_different_themes(self):
        # NVDA under "ai_infrastructure" AND "gpu_shortage" same day = 2 rows.
        d1 = _make_decision(theme="ai_infrastructure", action="interested")
        d2 = _make_decision(
            theme="gpu_shortage",
            action="dismissed",
            dismiss_category="thesis_setup",
            dismiss_reason="wrong_theme",
        )
        with FeedbackStore.open(self.path) as fb:
            fb.insert(d1)
            fb.insert(d2)
            rows = fb.list_by_brief_date(dt.date(2026, 5, 28))
            self.assertEqual(len(rows), 2)
            themes = {r.theme for r in rows}
            self.assertEqual(themes, {"ai_infrastructure", "gpu_shortage"})

    def test_list_by_ticker_returns_history_across_briefs(self):
        d1 = _make_decision(brief_date=dt.date(2026, 5, 27))
        d2 = _make_decision(brief_date=dt.date(2026, 5, 28))
        d3 = _make_decision(ticker="AMD", brief_date=dt.date(2026, 5, 28))
        with FeedbackStore.open(self.path) as fb:
            fb.insert(d1)
            fb.insert(d2)
            fb.insert(d3)
            nvda = fb.list_by_ticker("NVDA")
            self.assertEqual(
                {r.brief_date for r in nvda}, {dt.date(2026, 5, 27), dt.date(2026, 5, 28)}
            )
            self.assertEqual([r.ticker for r in nvda], ["NVDA", "NVDA"])

    def test_delete_by_id_removes_row(self):
        d = _make_decision()
        with FeedbackStore.open(self.path) as fb:
            row_id = fb.insert(d)
            fb.delete(row_id)
            self.assertIsNone(fb.get(row_id))

    def test_delete_unknown_id_is_noop(self):
        with FeedbackStore.open(self.path) as fb:
            # No raise — idempotent undo from a stale SPA state
            fb.delete("00000000-0000-0000-0000-000000000000")


class TestMarketRegime(unittest.TestCase):
    """Pure VIX bucket classifier — no network."""

    def test_low_below_15(self):
        self.assertEqual(regime.classify_vix(10.0), "low")
        self.assertEqual(regime.classify_vix(14.99), "low")

    def test_mid_15_to_25(self):
        self.assertEqual(regime.classify_vix(15.0), "mid")
        self.assertEqual(regime.classify_vix(20.0), "mid")
        self.assertEqual(regime.classify_vix(24.99), "mid")

    def test_high_at_or_above_25(self):
        self.assertEqual(regime.classify_vix(25.0), "high")
        self.assertEqual(regime.classify_vix(35.0), "high")

    def test_classify_vix_handles_none_as_unknown(self):
        # If the caller couldn't fetch VIX (e.g. weekend, holiday, network
        # blip in the API insert path), regime stamp is `unknown` rather
        # than blowing up the POST. Better to lose 1 row of regime than 1
        # row of decision.
        self.assertEqual(regime.classify_vix(None), "unknown")


if __name__ == "__main__":
    unittest.main()
