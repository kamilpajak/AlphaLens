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

from alphalens_feedback import regime
from alphalens_feedback.store import (
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
            row_id, was_created = fb.insert(d)
            self.assertTrue(was_created)
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
            _, created_first = fb.insert(d1)
            self.assertTrue(created_first)
            _, created_second = fb.insert(d2)
            # Second insert hit the upsert path; was_created must be False so
            # the Django view returns 200 instead of 201 (zen #5).
            self.assertFalse(created_second)
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
            _, created_a = fb.insert(d1)
            _, created_b = fb.insert(d2)
            # Both NEW rows — different theme, so the unique-key check
            # in insert() returns no existing row for either.
            self.assertTrue(created_a)
            self.assertTrue(created_b)
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
            row_id, _ = fb.insert(d)
            fb.delete(row_id)
            self.assertIsNone(fb.get(row_id))

    def test_delete_unknown_id_is_noop(self):
        with FeedbackStore.open(self.path) as fb:
            # No raise — idempotent undo from a stale SPA state
            fb.delete("00000000-0000-0000-0000-000000000000")


# v1 schema (15 columns, no outcome columns) — used to fabricate a legacy
# database so the v1->v2 ALTER migration path is exercised, not just the
# fresh-DB CREATE path.
_V1_DECISIONS_DDL = """
    CREATE TABLE decisions (
        id TEXT PRIMARY KEY,
        brief_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        theme TEXT NOT NULL,
        surfaced_at TEXT NOT NULL,
        action TEXT NOT NULL,
        action_at TEXT NOT NULL,
        dismiss_category TEXT,
        dismiss_reason TEXT,
        dismiss_note TEXT,
        confidence_subjective INTEGER,
        paper_trade_plan_id TEXT,
        position_size_usd REAL,
        entry_price REAL,
        market_regime_at_entry TEXT,
        UNIQUE(brief_date, ticker, theme)
    )
"""

# gen-1 schema (PR-1: 21 columns incl. the original ``realized_pnl``) — used
# to exercise the gen-1 -> gen-2 column rename to ``realized_return``.
_GEN1_DECISIONS_DDL = """
    CREATE TABLE decisions (
        id TEXT PRIMARY KEY,
        brief_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        theme TEXT NOT NULL,
        surfaced_at TEXT NOT NULL,
        action TEXT NOT NULL,
        action_at TEXT NOT NULL,
        dismiss_category TEXT,
        dismiss_reason TEXT,
        dismiss_note TEXT,
        confidence_subjective INTEGER,
        paper_trade_plan_id TEXT,
        position_size_usd REAL,
        entry_price REAL,
        market_regime_at_entry TEXT,
        outcome_plan_id TEXT,
        fill_status TEXT,
        exit_kind TEXT,
        shadow_return REAL,
        realized_pnl REAL,
        outcome_computed_at TEXT,
        UNIQUE(brief_date, ticker, theme)
    );
"""

_OUTCOME_COLUMN_NAMES = {
    "outcome_plan_id",
    "fill_status",
    "exit_kind",
    "shadow_return",
    "realized_return",
    "outcome_computed_at",
}

# gen-2 schema (PR-3: 21 columns, ``realized_return`` named, NO v3 brief-metadata
# columns) — used to exercise the gen-2 -> gen-3 ADD COLUMN migration path.
_GEN2_DECISIONS_DDL = """
    CREATE TABLE decisions (
        id TEXT PRIMARY KEY,
        brief_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        theme TEXT NOT NULL,
        surfaced_at TEXT NOT NULL,
        action TEXT NOT NULL,
        action_at TEXT NOT NULL,
        dismiss_category TEXT,
        dismiss_reason TEXT,
        dismiss_note TEXT,
        confidence_subjective INTEGER,
        paper_trade_plan_id TEXT,
        position_size_usd REAL,
        entry_price REAL,
        market_regime_at_entry TEXT,
        outcome_plan_id TEXT,
        fill_status TEXT,
        exit_kind TEXT,
        shadow_return REAL,
        realized_return REAL,
        outcome_computed_at TEXT,
        UNIQUE(brief_date, ticker, theme)
    );
"""

_BRIEF_METADATA_COLUMN_NAMES = {
    "layer4_score",
    "rank_in_day",
    "cohort_size_in_day",
    "gate_verdict_json",
    "brief_model_used",
}


class TestOutcomeColumnsSchema(unittest.TestCase):
    """v2 outcome-join columns + the PRAGMA user_version ALTER migration."""

    def test_fresh_db_has_outcome_columns(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "feedback.db"
            with FeedbackStore.open(path) as fb:
                cols = {r[1] for r in fb.conn.execute("PRAGMA table_info(decisions)")}
                self.assertTrue(_OUTCOME_COLUMN_NAMES.issubset(cols))

    def test_fresh_db_sets_user_version(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "feedback.db"
            with FeedbackStore.open(path) as fb:
                user_version = fb.conn.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(user_version, 4)

    def test_fresh_db_open_twice_does_not_raise_duplicate_column(self):
        # Migration must be idempotent: a fresh DB already carries the
        # columns from the CREATE block AND has user_version=0 on first
        # open, so a naive `ALTER TABLE ADD COLUMN` would raise
        # "duplicate column name" on the second open. The table_info guard
        # must skip already-present columns.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "feedback.db"
            with FeedbackStore.open(path):
                pass
            with FeedbackStore.open(path) as fb:  # must not raise
                cols = {r[1] for r in fb.conn.execute("PRAGMA table_info(decisions)")}
                self.assertTrue(_OUTCOME_COLUMN_NAMES.issubset(cols))

    def test_legacy_v1_db_gets_columns_via_alter_and_bumps_user_version(self):
        # Hand-roll a v1 DB (15 columns, user_version=0) then open through
        # FeedbackStore. The ALTER block must add the 6 outcome columns and
        # set user_version to the current generation — pins the documented
        # legacy migration path.
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "feedback.db"
            raw = sqlite3.connect(str(path))
            raw.execute(_V1_DECISIONS_DDL)
            raw.execute(
                """INSERT INTO decisions(id, brief_date, ticker, theme, surfaced_at,
                                          action, action_at)
                   VALUES ('legacy-1', '2026-05-20', 'NVDA', 'ai',
                           '2026-05-20T06:30:00+00:00', 'interested',
                           '2026-05-20T08:00:00+00:00')"""
            )
            raw.commit()
            self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 0)
            raw.close()

            with FeedbackStore.open(path) as fb:
                cols = {r[1] for r in fb.conn.execute("PRAGMA table_info(decisions)")}
                self.assertTrue(_OUTCOME_COLUMN_NAMES.issubset(cols))
                self.assertEqual(fb.conn.execute("PRAGMA user_version").fetchone()[0], 4)
                # legacy row still readable, outcome fields default NULL
                legacy = fb.get("legacy-1")
                self.assertIsNotNone(legacy)
                self.assertIsNone(legacy.fill_status)
                self.assertIsNone(legacy.outcome_plan_id)

    def test_gen1_db_renames_realized_pnl_to_realized_return(self):
        # gen-1 (PR-1) shipped a ``realized_pnl`` column (dollars, never
        # populated). gen-2 (PR-3) renames it to ``realized_return`` (a decimal
        # fraction). The rename must fire on a gen-1 DB, drop the old name, keep
        # the new one, and NOT create a duplicate empty ``realized_return``.
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "feedback.db"
            raw = sqlite3.connect(str(path))
            raw.executescript(_GEN1_DECISIONS_DDL)
            raw.execute("PRAGMA user_version = 1")
            raw.execute(
                """INSERT INTO decisions(id, brief_date, ticker, theme, surfaced_at,
                                          action, action_at)
                   VALUES ('g1-1', '2026-05-20', 'NVDA', 'ai',
                           '2026-05-20T06:30:00+00:00', 'interested',
                           '2026-05-20T08:00:00+00:00')"""
            )
            raw.commit()
            raw.close()

            with FeedbackStore.open(path) as fb:
                cols = {r[1] for r in fb.conn.execute("PRAGMA table_info(decisions)")}
                self.assertIn("realized_return", cols)
                self.assertNotIn("realized_pnl", cols)
                self.assertEqual(fb.conn.execute("PRAGMA user_version").fetchone()[0], 4)
                # legacy row still reads, realized_return defaults NULL
                self.assertIsNone(fb.get("g1-1").realized_return)


def _normalized_table_info(conn) -> dict[str, tuple]:
    """Map each ``decisions`` column → (type, notnull, dflt_value, pk).

    Keyed by name (NOT cid) because the contract is an identical column SET +
    per-column type/nullability, not a positional order — the store reads/writes
    by name, so order is immaterial. CHECK / UNIQUE constraints are not in
    ``PRAGMA table_info`` and are intentionally out of scope here.
    """
    return {
        row[1]: (row[2], row[3], row[4], row[5])
        for row in conn.execute("PRAGMA table_info(decisions)")
    }


class TestMigrationStateConvergence(unittest.TestCase):
    """L2 contract (test-strategy Phase 2): a fresh-v2 DB and a DB upgraded
    from an older schema MUST converge to an identical ``decisions`` table.

    Pins the #351 seam — the ``ALTER TABLE`` migration path and the
    ``CREATE TABLE`` fresh path are two separate schema definitions that can
    drift. The existing ``TestOutcomeColumnsSchema`` checks each path in
    isolation; this asserts the two AGREE, with a positive control proving the
    convergence assertion bites.
    """

    def _open_fresh(self, td: str) -> dict[str, tuple]:
        path = Path(td) / "fresh.db"
        with FeedbackStore.open(path) as fb:
            return _normalized_table_info(fb.conn)

    def _open_upgraded(self, td: str, base_ddl: str, *, user_version: int) -> dict[str, tuple]:
        import sqlite3

        path = Path(td) / "upgraded.db"
        raw = sqlite3.connect(str(path))
        raw.executescript(base_ddl)
        raw.execute(f"PRAGMA user_version = {user_version}")
        raw.commit()
        raw.close()
        with FeedbackStore.open(path) as fb:
            return _normalized_table_info(fb.conn)

    def test_fresh_v2_and_upgraded_v1_table_info_identical(self):
        with tempfile.TemporaryDirectory() as td:
            fresh = self._open_fresh(td)
            upgraded = self._open_upgraded(td, _V1_DECISIONS_DDL, user_version=0)
        self.assertEqual(
            set(fresh), set(upgraded), "fresh-v2 and upgraded-v1 have different column SETS"
        )
        for col in fresh:
            self.assertEqual(
                fresh[col], upgraded[col], f"column {col!r} diverges (type/notnull/default/pk)"
            )

    def test_fresh_v2_and_upgraded_gen1_table_info_identical(self):
        # gen-1 carries ``realized_pnl``; the rename to ``realized_return`` must
        # bring it into line with the fresh column set (no leftover, no dupe).
        with tempfile.TemporaryDirectory() as td:
            fresh = self._open_fresh(td)
            upgraded = self._open_upgraded(td, _GEN1_DECISIONS_DDL, user_version=1)
        self.assertEqual(set(fresh), set(upgraded))
        self.assertNotIn("realized_pnl", upgraded)
        self.assertIn("realized_return", upgraded)
        for col in fresh:
            self.assertEqual(fresh[col], upgraded[col], f"column {col!r} diverges")

    def test_divergent_migration_is_caught(self):
        # positive control: simulate a broken migration that forgets one outcome
        # column (exit_kind). The convergence assertion above MUST be able to
        # flag this — otherwise it asserts nothing.
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            fresh = self._open_fresh(td)

            broken_path = Path(td) / "broken.db"
            raw = sqlite3.connect(str(broken_path))
            raw.executescript(_V1_DECISIONS_DDL)
            # Apply every outcome column EXCEPT exit_kind (the simulated bug).
            for name in _OUTCOME_COLUMN_NAMES - {"exit_kind"}:
                raw.execute(f"ALTER TABLE decisions ADD COLUMN {name} TEXT")
            raw.commit()
            broken = _normalized_table_info(raw)
            raw.close()

        self.assertIn("exit_kind", fresh)
        self.assertNotIn("exit_kind", broken)
        self.assertNotEqual(set(fresh), set(broken))


class TestBriefMetadataColumns(unittest.TestCase):
    """v3 click-time brief-metadata columns (Variant A): schema migration,
    CREATE/ALTER parity, round-trip, and click-time re-write on upsert.

    UNLIKE the v2 outcome columns these are stamped DIRECTLY from the Decision
    on every insert (not carried-forward), so an upsert must re-write — never
    NULL — them.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "feedback.db"

    def tearDown(self):
        self._td.cleanup()

    def test_gen2_db_gets_v3_columns_via_alter_and_bumps_user_version(self):
        # Hand-roll a gen-2 DB (21 cols, NO v3 brief-metadata, user_version=2)
        # then open through FeedbackStore. The gen 2 -> gen 3 ALTER loop must add
        # all 5 v3 columns and bump user_version to 3; a pre-existing row gains
        # them as NULL.
        import sqlite3

        raw = sqlite3.connect(str(self.path))
        raw.executescript(_GEN2_DECISIONS_DDL)
        raw.execute("PRAGMA user_version = 2")
        raw.execute(
            """INSERT INTO decisions(id, brief_date, ticker, theme, surfaced_at,
                                      action, action_at)
               VALUES ('g2-1', '2026-05-20', 'NVDA', 'ai',
                       '2026-05-20T06:30:00+00:00', 'interested',
                       '2026-05-20T08:00:00+00:00')"""
        )
        raw.commit()
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 2)
        raw.close()

        with FeedbackStore.open(self.path) as fb:
            cols = {r[1] for r in fb.conn.execute("PRAGMA table_info(decisions)")}
            self.assertTrue(_BRIEF_METADATA_COLUMN_NAMES.issubset(cols))
            self.assertEqual(fb.conn.execute("PRAGMA user_version").fetchone()[0], 4)
            # pre-existing row now carries the v3 columns as NULL
            legacy = fb.get("g2-1")
            self.assertIsNotNone(legacy)
            self.assertIsNone(legacy.layer4_score)
            self.assertIsNone(legacy.rank_in_day)
            self.assertIsNone(legacy.cohort_size_in_day)
            self.assertIsNone(legacy.gate_verdict_json)
            self.assertIsNone(legacy.brief_model_used)

    def test_fresh_db_and_upgraded_gen2_table_info_identical(self):
        # CREATE/ALTER parity: a fresh DB must have the IDENTICAL decisions
        # column set as a gen-2 DB migrated to gen-3.
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            fresh_path = Path(td) / "fresh.db"
            with FeedbackStore.open(fresh_path) as fb:
                fresh = _normalized_table_info(fb.conn)

            upgraded_path = Path(td) / "upgraded.db"
            raw = sqlite3.connect(str(upgraded_path))
            raw.executescript(_GEN2_DECISIONS_DDL)
            raw.execute("PRAGMA user_version = 2")
            raw.commit()
            raw.close()
            with FeedbackStore.open(upgraded_path) as fb:
                upgraded = _normalized_table_info(fb.conn)

        self.assertEqual(set(fresh), set(upgraded))
        self.assertTrue(_BRIEF_METADATA_COLUMN_NAMES.issubset(set(fresh)))
        for col in fresh:
            self.assertEqual(fresh[col], upgraded[col], f"column {col!r} diverges")

    def test_brief_metadata_round_trip(self):
        d = _make_decision(
            layer4_score=4,
            rank_in_day=2,
            cohort_size_in_day=12,
            gate_verdict_json='{"passed": ["liquidity"], "failed": [], "unknown": ["pead"]}',
            brief_model_used="deepseek-v4-pro",
        )
        with FeedbackStore.open(self.path) as fb:
            row_id, _ = fb.insert(d)
            fetched = fb.get(row_id)
            self.assertEqual(fetched.layer4_score, 4)
            self.assertEqual(fetched.rank_in_day, 2)
            self.assertEqual(fetched.cohort_size_in_day, 12)
            self.assertEqual(
                fetched.gate_verdict_json,
                '{"passed": ["liquidity"], "failed": [], "unknown": ["pead"]}',
            )
            self.assertEqual(fetched.brief_model_used, "deepseek-v4-pro")

    def test_upsert_rewrites_brief_metadata_not_carried_forward(self):
        # Click-time: re-inserting the same (brief_date, ticker, theme) with the
        # 5 fields set again must WRITE the new values, not preserve/NULL them.
        with FeedbackStore.open(self.path) as fb:
            row_id, _ = fb.insert(
                _make_decision(
                    action="interested",
                    layer4_score=3,
                    rank_in_day=5,
                    cohort_size_in_day=10,
                    gate_verdict_json='{"passed": [], "failed": [], "unknown": []}',
                    brief_model_used="deepseek-v4-flash",
                )
            )
            # user flips to dismissed; the SPA re-stamps the (possibly changed)
            # brief metadata on the same click.
            fb.insert(
                _make_decision(
                    action="dismissed",
                    dismiss_category="thesis_setup",
                    dismiss_reason="too_expensive",
                    layer4_score=4,
                    rank_in_day=1,
                    cohort_size_in_day=11,
                    gate_verdict_json='{"passed": ["liquidity"], "failed": [], "unknown": []}',
                    brief_model_used="deepseek-v4-pro",
                )
            )
            fetched = fb.get(row_id)
            self.assertEqual(fetched.action, "dismissed")
            # new values written, NOT lost / NULLed
            self.assertEqual(fetched.layer4_score, 4)
            self.assertEqual(fetched.rank_in_day, 1)
            self.assertEqual(fetched.cohort_size_in_day, 11)
            self.assertEqual(
                fetched.gate_verdict_json,
                '{"passed": ["liquidity"], "failed": [], "unknown": []}',
            )
            self.assertEqual(fetched.brief_model_used, "deepseek-v4-pro")


class TestOutcomeRoundTrip(unittest.TestCase):
    """Outcome fields default to None and survive a round-trip."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "feedback.db"

    def tearDown(self):
        self._td.cleanup()

    def test_outcome_fields_default_none_on_insert(self):
        with FeedbackStore.open(self.path) as fb:
            row_id, _ = fb.insert(_make_decision())
            fetched = fb.get(row_id)
            self.assertIsNone(fetched.outcome_plan_id)
            self.assertIsNone(fetched.fill_status)
            self.assertIsNone(fetched.exit_kind)
            self.assertIsNone(fetched.shadow_return)
            self.assertIsNone(fetched.realized_return)
            self.assertIsNone(fetched.outcome_computed_at)

    def test_stamp_outcome_persists_join_fields(self):
        with FeedbackStore.open(self.path) as fb:
            row_id, _ = fb.insert(_make_decision())
            stamped_at = dt.datetime(2026, 6, 1, 21, 30, tzinfo=UTC)
            fb.stamp_outcome(
                row_id,
                fill_status="FILLED",
                exit_kind="TP_HIT",
                outcome_plan_id="42",
                outcome_computed_at=stamped_at,
            )
            fetched = fb.get(row_id)
            self.assertEqual(fetched.fill_status, "FILLED")
            self.assertEqual(fetched.exit_kind, "TP_HIT")
            self.assertEqual(fetched.outcome_plan_id, "42")
            self.assertEqual(fetched.outcome_computed_at, stamped_at)
            # The fill-status pass leaves the return columns untouched (the
            # shadow pass fills them) — the sentinel default keeps them NULL.
            self.assertIsNone(fetched.shadow_return)
            self.assertIsNone(fetched.realized_return)

    def test_stamp_outcome_persists_shadow_and_realized_return(self):
        # The PR-3 shadow pass passes the return kwargs explicitly — they
        # round-trip as decimal fractions.
        with FeedbackStore.open(self.path) as fb:
            row_id, _ = fb.insert(_make_decision())
            fb.stamp_outcome(
                row_id,
                fill_status="FILLED",
                exit_kind="TP_HIT",
                outcome_plan_id="42",
                outcome_computed_at=dt.datetime(2026, 6, 1, 21, 30, tzinfo=UTC),
                shadow_return=0.042,
                realized_return=0.031,
            )
            fetched = fb.get(row_id)
            self.assertEqual(fetched.shadow_return, 0.042)
            self.assertEqual(fetched.realized_return, 0.031)

    def test_fill_status_pass_preserves_existing_shadow_return(self):
        # Two-pass safety: the nightly shadow pass stamps shadow_return, then a
        # later cheap fill-status re-run (omitting the return kwargs) must NOT
        # wipe it back to NULL. The sentinel default keeps it out of the SET.
        with FeedbackStore.open(self.path) as fb:
            row_id, _ = fb.insert(_make_decision())
            now = dt.datetime(2026, 6, 1, 21, 30, tzinfo=UTC)
            fb.stamp_outcome(
                row_id,
                fill_status="FILLED",
                exit_kind="TP_HIT",
                outcome_plan_id="42",
                outcome_computed_at=now,
                shadow_return=0.05,
                realized_return=0.04,
            )
            # cheap fill-status re-run — no shadow/realized kwargs
            fb.stamp_outcome(
                row_id,
                fill_status="FILLED",
                exit_kind="TP_HIT",
                outcome_plan_id="42",
                outcome_computed_at=now + dt.timedelta(hours=1),
            )
            fetched = fb.get(row_id)
            self.assertEqual(fetched.shadow_return, 0.05)
            self.assertEqual(fetched.realized_return, 0.04)

    def test_stamp_outcome_writes_explicit_none_for_unfilled(self):
        # An UNFILLED row has no realised price, so the shadow pass passes
        # realized_return=None explicitly — which IS written (distinct from
        # "omitted"). shadow_return for an unfilled row is still populated.
        with FeedbackStore.open(self.path) as fb:
            row_id, _ = fb.insert(_make_decision())
            fb.stamp_outcome(
                row_id,
                fill_status="UNFILLED",
                exit_kind="UNFILLED",
                outcome_plan_id="42",
                outcome_computed_at=dt.datetime(2026, 6, 1, 21, 30, tzinfo=UTC),
                shadow_return=-0.012,
                realized_return=None,
            )
            fetched = fb.get(row_id)
            self.assertEqual(fetched.shadow_return, -0.012)
            self.assertIsNone(fetched.realized_return)

    def test_stamp_outcome_does_not_revalidate_decision(self):
        # The write path is a targeted UPDATE, not a Decision rebuild, so a
        # legacy row that would fail tightened __post_init__ rules is still
        # stampable. Seed a dismissed/other row WITHOUT the required note via
        # the read-bypass path, then stamp it — must not raise.
        import sqlite3

        raw = sqlite3.connect(str(self.path))
        # ensure schema first by opening once
        raw.close()
        with FeedbackStore.open(self.path) as fb:
            fb.conn.execute(
                """INSERT INTO decisions(id, brief_date, ticker, theme, surfaced_at,
                                          action, action_at, dismiss_category, dismiss_reason)
                   VALUES ('odd-1', '2026-05-20', 'NVDA', 'ai',
                           '2026-05-20T06:30:00+00:00', 'dismissed',
                           '2026-05-20T08:00:00+00:00', 'other', 'other')"""
            )
            # No DecisionValidationError even though 'other' lacks a note.
            fb.stamp_outcome(
                "odd-1",
                fill_status="UNFILLED",
                exit_kind="UNFILLED",
                outcome_plan_id="7",
                outcome_computed_at=dt.datetime(2026, 6, 1, tzinfo=UTC),
            )
            fetched = fb.get("odd-1")
            self.assertEqual(fetched.fill_status, "UNFILLED")

    def test_upsert_preserves_existing_outcome_columns(self):
        # A user flipping interested -> dismissed re-POSTs through insert()
        # AFTER the join job stamped an outcome. The upsert must NOT wipe the
        # outcome columns back to NULL (they are job-set, never user-set).
        with FeedbackStore.open(self.path) as fb:
            row_id, _ = fb.insert(_make_decision(action="interested"))
            fb.stamp_outcome(
                row_id,
                fill_status="FILLED",
                exit_kind="TP_HIT",
                outcome_plan_id="99",
                outcome_computed_at=dt.datetime(2026, 6, 1, tzinfo=UTC),
            )
            # user flips to dismissed (same brief_date, ticker, theme)
            fb.insert(
                _make_decision(
                    action="dismissed",
                    dismiss_category="thesis_setup",
                    dismiss_reason="too_expensive",
                )
            )
            fetched = fb.get(row_id)
            self.assertEqual(fetched.action, "dismissed")
            # outcome survived the upsert
            self.assertEqual(fetched.fill_status, "FILLED")
            self.assertEqual(fetched.exit_kind, "TP_HIT")
            self.assertEqual(fetched.outcome_plan_id, "99")


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


class TestVixCacheReader(unittest.TestCase):
    """Hot-path VIX cache reader feeding classify_vix (Track A v2 PR-2).

    get_cached_vix does ONE local file read, zero network, and degrades to
    None (-> classify_vix returns "unknown") on ANY failure. Staleness is
    measured on ``fetched_at`` with a 96h ceiling.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "vix_regime_cache.json"

    def tearDown(self):
        self._td.cleanup()

    def _write(self, *, vix, fetched_at: dt.datetime, observation_date: str = "2026-05-29"):
        import json

        self.path.write_text(
            json.dumps(
                {
                    "observation_date": observation_date,
                    "vix": vix,
                    "fetched_at": fetched_at.isoformat(),
                    "series": "VIXCLS",
                }
            )
        )

    def test_returns_value_on_fresh_cache(self):
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        self._write(vix=18.5, fetched_at=now)
        self.assertEqual(regime.get_cached_vix(self.path, now=now), 18.5)

    def test_missing_file_returns_none(self):
        missing = Path(self._td.name) / "nope.json"
        self.assertIsNone(regime.get_cached_vix(missing))

    def test_stale_beyond_96h_returns_none(self):
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        self._write(vix=18.5, fetched_at=now - dt.timedelta(hours=97))
        self.assertIsNone(regime.get_cached_vix(self.path, now=now))

    def test_at_96h_boundary_returns_value(self):
        # Policy is age > 96h -> stale; exactly 96h is still fresh.
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        self._write(vix=18.5, fetched_at=now - dt.timedelta(hours=96))
        self.assertEqual(regime.get_cached_vix(self.path, now=now), 18.5)

    def test_within_96h_weekend_returns_value(self):
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        self._write(vix=21.0, fetched_at=now - dt.timedelta(hours=70))
        self.assertEqual(regime.get_cached_vix(self.path, now=now), 21.0)

    def test_fresh_fetched_at_old_observation_still_returns_value(self):
        # fetched_at is the SOLE freshness gate: a live refresher re-stamping
        # fetched_at every few hours proves liveness even if FRED's last
        # published observation is several days old (holiday week).
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        self._write(vix=19.0, fetched_at=now, observation_date="2026-05-22")
        self.assertEqual(regime.get_cached_vix(self.path, now=now), 19.0)

    def test_malformed_json_returns_none(self):
        self.path.write_text("not json {")
        self.assertIsNone(regime.get_cached_vix(self.path))

    def test_missing_fetched_at_key_returns_none(self):
        import json

        self.path.write_text(json.dumps({"vix": 18.5, "series": "VIXCLS"}))
        self.assertIsNone(regime.get_cached_vix(self.path))

    def test_non_numeric_vix_returns_none(self):
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        self._write(vix="not-a-number", fetched_at=now)
        self.assertIsNone(regime.get_cached_vix(self.path, now=now))

    def test_classify_vix_of_cached_value_buckets_correctly(self):
        now = dt.datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
        for vix, expected in ((12.0, "low"), (22.0, "mid"), (30.0, "high")):
            self._write(vix=vix, fetched_at=now)
            self.assertEqual(
                regime.classify_vix(regime.get_cached_vix(self.path, now=now)), expected
            )


class TestIterMaturedDecisions(unittest.TestCase):
    """Projection feeding the PR-4 execution-mode estimator.

    Two contracts that protect the ≥50 pooled gate from inflating below the true
    priced sample: maturity == ``shadow_return IS NOT NULL`` (not
    ``outcome_computed_at``), and dedup to one row per ``(brief_date, ticker)``.
    """

    def _stamp_priced(self, fb, decision_id, *, fill_status, shadow, realized):
        fb.stamp_outcome(
            decision_id,
            fill_status=fill_status,
            exit_kind="TP_HIT" if fill_status == "FILLED" else "UNFILLED",
            outcome_plan_id="p1",
            outcome_computed_at=dt.datetime(2026, 5, 30, 2, 0, tzinfo=UTC),
            shadow_return=shadow,
            realized_return=realized,
        )

    def test_unpriced_cheap_pass_row_is_excluded(self):
        # The CRITICAL fix: a row stamped by the cheap fill-status join only
        # (outcome_computed_at set, shadow_return still NULL) must NOT appear —
        # else it would count toward the 50-gate while carrying no signal.
        with tempfile.TemporaryDirectory() as td:
            with FeedbackStore.open(Path(td) / "feedback.db") as fb:
                row_id, _ = fb.insert(_make_decision())
                fb.stamp_outcome(
                    row_id,
                    fill_status="UNFILLED",
                    exit_kind="UNFILLED",
                    outcome_plan_id="p1",
                    outcome_computed_at=dt.datetime(2026, 5, 30, 2, 0, tzinfo=UTC),
                    # shadow_return omitted → stays NULL (cheap pass only)
                )
                self.assertEqual(fb.iter_matured_decisions(), [])

    def test_priced_row_is_included_with_projection(self):
        with tempfile.TemporaryDirectory() as td:
            with FeedbackStore.open(Path(td) / "feedback.db") as fb:
                row_id, _ = fb.insert(_make_decision(market_regime_at_entry="mid"))
                self._stamp_priced(fb, row_id, fill_status="FILLED", shadow=0.05, realized=0.03)
                self.assertEqual(fb.iter_matured_decisions(), [("mid", "FILLED", 0.05, 0.03)])

    def test_null_regime_coalesced_to_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            with FeedbackStore.open(Path(td) / "feedback.db") as fb:
                row_id, _ = fb.insert(_make_decision())  # no market_regime_at_entry
                self._stamp_priced(fb, row_id, fill_status="UNFILLED", shadow=0.08, realized=None)
                rows = fb.iter_matured_decisions()
                self.assertEqual(rows, [("unknown", "UNFILLED", 0.08, None)])

    def test_multi_theme_ticker_day_counts_once(self):
        # The CRITICAL dedup fix: the ticker-day outcome join stamps the SAME
        # outcome onto every same-day multi-theme decision, so the rows are
        # identical; counting decision rows would double-count one economic
        # outcome at the n≈50 scale.
        with tempfile.TemporaryDirectory() as td:
            with FeedbackStore.open(Path(td) / "feedback.db") as fb:
                id_a, _ = fb.insert(
                    _make_decision(theme="ai_infrastructure", market_regime_at_entry="mid")
                )
                id_b, _ = fb.insert(
                    _make_decision(theme="data_center", market_regime_at_entry="mid")
                )
                # Same plan outcome stamped onto both (same brief_date+ticker).
                self._stamp_priced(fb, id_a, fill_status="FILLED", shadow=0.05, realized=0.03)
                self._stamp_priced(fb, id_b, fill_status="FILLED", shadow=0.05, realized=0.03)
                rows = fb.iter_matured_decisions()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0], ("mid", "FILLED", 0.05, 0.03))


# gen-3 schema (v3 Variant A: 26 columns — gen-2 21 + 5 brief-metadata, NO
# ladder-replay columns) — used to exercise the gen-3 -> gen-4 ADD COLUMN path.
_GEN3_DECISIONS_DDL = """
    CREATE TABLE decisions (
        id TEXT PRIMARY KEY,
        brief_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        theme TEXT NOT NULL,
        surfaced_at TEXT NOT NULL,
        action TEXT NOT NULL,
        action_at TEXT NOT NULL,
        dismiss_category TEXT,
        dismiss_reason TEXT,
        dismiss_note TEXT,
        confidence_subjective INTEGER,
        paper_trade_plan_id TEXT,
        position_size_usd REAL,
        entry_price REAL,
        market_regime_at_entry TEXT,
        layer4_score INTEGER,
        rank_in_day INTEGER,
        cohort_size_in_day INTEGER,
        gate_verdict_json TEXT,
        brief_model_used TEXT,
        outcome_plan_id TEXT,
        fill_status TEXT,
        exit_kind TEXT,
        shadow_return REAL,
        realized_return REAL,
        outcome_computed_at TEXT,
        UNIQUE(brief_date, ticker, theme)
    );
"""

_LADDER_OUTCOME_COLUMN_NAMES = {
    "sequence_str",
    "mfe",
    "mae",
    "forward_return",
    "ladder_classification",
    "blended_entry",
    "realized_r",
    "horizon_open",
    "ambiguous_bars",
    "ratchet_realized_r",
}


def _seed_decision_row(conn, decision_id: str) -> None:
    conn.execute(
        """INSERT INTO decisions(id, brief_date, ticker, theme, surfaced_at,
                                  action, action_at)
           VALUES (?, '2026-05-20', 'NVDA', 'ai',
                   '2026-05-20T06:30:00+00:00', 'interested',
                   '2026-05-20T08:00:00+00:00')""",
        (decision_id,),
    )


class TestLadderOutcomeSchema(unittest.TestCase):
    """gen-4 ladder-replay outcome columns: fresh DDL, gen-3 -> gen-4 ALTER,
    CREATE/ALTER parity, and PRAGMA user_version bump."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "feedback.db"

    def tearDown(self):
        self._td.cleanup()

    def test_fresh_db_has_ladder_columns_and_user_version_4(self):
        with FeedbackStore.open(self.path) as fb:
            cols = {r[1] for r in fb.conn.execute("PRAGMA table_info(decisions)")}
            self.assertTrue(_LADDER_OUTCOME_COLUMN_NAMES.issubset(cols))
            self.assertEqual(fb.conn.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_gen3_db_gets_ladder_columns_via_alter_and_bumps_user_version(self):
        import sqlite3

        raw = sqlite3.connect(str(self.path))
        raw.executescript(_GEN3_DECISIONS_DDL)
        raw.execute("PRAGMA user_version = 3")
        _seed_decision_row(raw, "g3-1")
        raw.commit()
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 3)
        raw.close()

        with FeedbackStore.open(self.path) as fb:
            cols = {r[1] for r in fb.conn.execute("PRAGMA table_info(decisions)")}
            self.assertTrue(_LADDER_OUTCOME_COLUMN_NAMES.issubset(cols))
            self.assertEqual(fb.conn.execute("PRAGMA user_version").fetchone()[0], 4)
            # pre-existing row gains the columns as NULL
            row = fb.conn.execute(
                "SELECT ladder_classification, realized_r FROM decisions WHERE id = 'g3-1'"
            ).fetchone()
            self.assertIsNone(row["ladder_classification"])
            self.assertIsNone(row["realized_r"])

    def test_open_twice_does_not_raise_duplicate_column(self):
        with FeedbackStore.open(self.path):
            pass
        with FeedbackStore.open(self.path) as fb:  # must not raise
            cols = {r[1] for r in fb.conn.execute("PRAGMA table_info(decisions)")}
            self.assertTrue(_LADDER_OUTCOME_COLUMN_NAMES.issubset(cols))

    def test_fresh_db_and_upgraded_gen3_table_info_identical(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            fresh_path = Path(td) / "fresh.db"
            with FeedbackStore.open(fresh_path) as fb:
                fresh = _normalized_table_info(fb.conn)

            upgraded_path = Path(td) / "upgraded.db"
            raw = sqlite3.connect(str(upgraded_path))
            raw.executescript(_GEN3_DECISIONS_DDL)
            raw.execute("PRAGMA user_version = 3")
            raw.commit()
            raw.close()
            with FeedbackStore.open(upgraded_path) as fb:
                upgraded = _normalized_table_info(fb.conn)

        self.assertEqual(set(fresh), set(upgraded))
        self.assertTrue(_LADDER_OUTCOME_COLUMN_NAMES.issubset(set(fresh)))
        for col in fresh:
            self.assertEqual(fresh[col], upgraded[col], f"column {col!r} diverges")


class TestStampLadderOutcome(unittest.TestCase):
    """gen-4 stamp_ladder_outcome: two-pass survival, all-unset no-op."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "feedback.db"

    def tearDown(self):
        self._td.cleanup()

    def _ladder_row(self, fb, decision_id: str):
        return fb.conn.execute(
            "SELECT sequence_str, realized_r, ladder_classification, horizon_open "
            "FROM decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()

    def test_two_pass_first_value_survives(self):
        with FeedbackStore.open(self.path) as fb:
            row_id, _ = fb.insert(_make_decision())
            # pass 1: stamp sequence_str only
            fb.stamp_ladder_outcome(row_id, sequence_str="E1->E2->TP1->SL")
            # pass 2: stamp realized_r only — must NOT wipe sequence_str
            fb.stamp_ladder_outcome(row_id, realized_r=-0.167)
            row = self._ladder_row(fb, row_id)
            self.assertEqual(row["sequence_str"], "E1->E2->TP1->SL")
            self.assertAlmostEqual(row["realized_r"], -0.167)

    def test_all_unset_is_noop(self):
        with FeedbackStore.open(self.path) as fb:
            row_id, _ = fb.insert(_make_decision())
            fb.stamp_ladder_outcome(row_id, sequence_str="E1->TP1")
            before = self._ladder_row(fb, row_id)
            # an all-unset call must not change anything (and must not error)
            fb.stamp_ladder_outcome(row_id)
            after = self._ladder_row(fb, row_id)
            self.assertEqual(tuple(before), tuple(after))

    def test_horizon_open_stored_as_text(self):
        with FeedbackStore.open(self.path) as fb:
            row_id, _ = fb.insert(_make_decision())
            fb.stamp_ladder_outcome(
                row_id,
                ladder_classification="OPEN",
                horizon_open=str(True),
            )
            row = self._ladder_row(fb, row_id)
            self.assertEqual(row["horizon_open"], "True")
            self.assertEqual(row["ladder_classification"], "OPEN")


class TestIterDecisionsForLadder(unittest.TestCase):
    """gen-4 iter_decisions_for_ladder: matured-window + NULL-classification gate,
    and the 3-field click-orthogonal projection."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "feedback.db"

    def tearDown(self):
        self._td.cleanup()

    def test_returns_only_unreplayed_in_window_three_fields(self):
        with FeedbackStore.open(self.path) as fb:
            # in-window, unreplayed
            fb.insert(_make_decision(brief_date=dt.date(2026, 5, 28), ticker="AAA", theme="t1"))
            # in-window but already replayed (classification set)
            r2, _ = fb.insert(
                _make_decision(brief_date=dt.date(2026, 5, 28), ticker="BBB", theme="t2")
            )
            fb.stamp_ladder_outcome(r2, ladder_classification="TP_FULL")
            # out of window (older than lookback_start)
            fb.insert(_make_decision(brief_date=dt.date(2026, 5, 10), ticker="CCC", theme="t3"))

            rows = fb.iter_decisions_for_ladder(lookback_start=dt.date(2026, 5, 20))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(len(row), 3)  # id, brief_date, ticker only
            _id, brief_date, ticker = row
            self.assertEqual(brief_date, "2026-05-28")
            self.assertEqual(ticker, "AAA")

    def test_ordered_newest_first(self):
        with FeedbackStore.open(self.path) as fb:
            fb.insert(_make_decision(brief_date=dt.date(2026, 5, 25), ticker="OLD", theme="t1"))
            fb.insert(_make_decision(brief_date=dt.date(2026, 5, 28), ticker="NEW", theme="t2"))
            rows = fb.iter_decisions_for_ladder(lookback_start=dt.date(2026, 5, 20))
            self.assertEqual([r[1] for r in rows], ["2026-05-28", "2026-05-25"])


if __name__ == "__main__":
    unittest.main()
