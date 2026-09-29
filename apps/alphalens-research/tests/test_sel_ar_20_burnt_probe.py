"""Unit tests for the two pure helpers of the burnt-panel `sel_ar_20` probe.

Hermetic: no store is read, so no outcome value is touched. These cover the two
pieces of real logic that could be silently wrong and that no store read would
reveal — the rule that picks one publication timestamp when an article has
several recorded, and the construction of contiguous arrival-session blocks.

The positive controls matter more than the happy paths: a tie-break that always
returned the latest timestamp, or a fold builder that dropped the runt instead
of merging it, would pass every straightforward case.
"""

from __future__ import annotations

import ast
import contextlib
import datetime as dt
import importlib.util
import io
import math
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "ml" / "2026_09_sel_ar_20_burnt_probe.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("_burnt_probe", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load()


def _ts(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso).replace(tzinfo=dt.UTC)


class TestPickNewsTimestamp(unittest.TestCase):
    """The brief records only a DATE; the news store keeps the real timestamp.

    Two urls in the burnt window carry two recorded timestamps about 46 hours
    apart, touching 6 panel rows, so "join on url" alone is ambiguous. The
    frozen rule keeps the recovered value consistent with the date the pipeline
    itself stamped.
    """

    def test_a_candidate_on_the_stamped_date_wins_over_a_later_one(self) -> None:
        got = probe.pick_news_timestamp(
            [_ts("2026-06-03T18:00:00"), _ts("2026-06-01T09:00:00")],
            dt.date(2026, 6, 1),
        )
        self.assertEqual(got, _ts("2026-06-01T09:00:00"))

    def test_among_several_on_the_stamped_date_the_latest_wins(self) -> None:
        got = probe.pick_news_timestamp(
            [_ts("2026-06-01T09:00:00"), _ts("2026-06-01T21:30:00"), _ts("2026-06-01T04:00:00")],
            dt.date(2026, 6, 1),
        )
        self.assertEqual(got, _ts("2026-06-01T21:30:00"))

    def test_with_nothing_on_the_stamped_date_the_latest_overall_wins(self) -> None:
        got = probe.pick_news_timestamp(
            [_ts("2026-05-30T08:00:00"), _ts("2026-06-04T12:00:00")],
            dt.date(2026, 6, 1),
        )
        self.assertEqual(got, _ts("2026-06-04T12:00:00"))

    def test_no_candidate_resolves_to_none(self) -> None:
        self.assertIsNone(probe.pick_news_timestamp([], dt.date(2026, 6, 1)))

    def test_the_date_match_is_not_just_the_latest_in_disguise(self) -> None:
        """Positive control. A rule that always returned the latest timestamp
        would pass the three cases above except this one."""
        got = probe.pick_news_timestamp(
            [_ts("2026-06-01T02:00:00"), _ts("2026-06-09T23:00:00")],
            dt.date(2026, 6, 1),
        )
        self.assertEqual(got, _ts("2026-06-01T02:00:00"))
        self.assertNotEqual(got, max([_ts("2026-06-01T02:00:00"), _ts("2026-06-09T23:00:00")]))


class TestTheAnchorIsReadNeverRecomputed(unittest.TestCase):
    """A source gate, because no behavioural test would catch this.

    The label store carries `anchor_session` — the first session AFTER the
    brief date (owner decision D4). `session_on_or_after` returns the brief
    date itself whenever that date is a trading session, so recomputing the
    anchor with it disagrees with the stored value on 296 of 466 burnt rows.
    The first run of this probe did exactly that and measured 65 articles as
    published after their own anchor open; they were not.

    A test that merely checked "the panel has an arrival column" would pass
    either way, so this reads the source instead.
    """

    @staticmethod
    def _called_names(source: str) -> set[str]:
        called = set()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
        return called

    def test_the_module_never_calls_session_on_or_after(self) -> None:
        self.assertNotIn(
            "session_on_or_after",
            self._called_names(_SCRIPT.read_text()),
            "the arrival anchor is READ from the label store's `anchor_session`; "
            "recomputing it is a second definition of the label",
        )

    def test_the_gate_reads_calls_and_not_prose(self) -> None:
        """Positive control, both ways. A substring check would fire on the
        docstring that explains the rule, and would miss a call written as an
        attribute."""
        self.assertIn("session_on_or_after", _SCRIPT.read_text())  # named in prose
        self.assertIn("f", self._called_names("f(1)"))
        self.assertIn("g", self._called_names("mod.g(1)"))
        self.assertNotIn("h", self._called_names('"""h is forbidden"""'))

    def test_the_module_does_read_the_stored_anchor(self) -> None:
        """A file that dropped the anchor entirely would satisfy the gate."""
        self.assertIn("anchor_session", _SCRIPT.read_text())


class TestCatalystAgeHours(unittest.TestCase):
    """An article at or after the anchor open is not a lag, so it is EXCLUDED.

    The first version counted the violation and still entered the negative
    value. Zero rows hit it once the anchor was read rather than recomputed,
    but a counter is not an exclusion.
    """

    def test_a_normal_lag_is_returned_in_hours(self) -> None:
        got = probe.catalyst_age_hours(_ts("2026-06-01T13:30:00"), _ts("2026-06-02T13:30:00"))
        self.assertAlmostEqual(got, 24.0)

    def test_an_article_after_the_open_is_excluded_not_negated(self) -> None:
        got = probe.catalyst_age_hours(_ts("2026-06-02T18:00:00"), _ts("2026-06-02T13:30:00"))
        self.assertTrue(math.isnan(got))

    def test_an_article_exactly_at_the_open_is_excluded(self) -> None:
        """Positive control on the boundary — a strict `>` would let it through
        with an age of exactly zero."""
        moment = _ts("2026-06-02T13:30:00")
        self.assertTrue(math.isnan(probe.catalyst_age_hours(moment, moment)))

    def test_no_timestamp_is_excluded(self) -> None:
        self.assertTrue(math.isnan(probe.catalyst_age_hours(None, _ts("2026-06-02T13:30:00"))))


class TestContiguousBlockFolds(unittest.TestCase):
    """Folds are contiguous blocks of arrival sessions, runt merged into its
    predecessor. Purged folds are infeasible here (a 20-session label over a
    30-session arrival span), so these are the unpurged blocks house rule 9
    prescribes for exactly this situation."""

    @staticmethod
    def _sessions(n: int) -> list[dt.date]:
        return [dt.date(2026, 5, 20) + dt.timedelta(days=i) for i in range(n)]

    def test_every_session_lands_in_exactly_one_block(self) -> None:
        sessions = self._sessions(27)
        blocks = probe.contiguous_block_folds(sessions, block_sessions=5)
        flat = [s for b in blocks for s in b]
        self.assertEqual(sorted(flat), sorted(sessions))
        self.assertEqual(len(flat), len(set(flat)))

    def test_the_runt_is_merged_into_its_predecessor_not_dropped(self) -> None:
        """Positive control. 27 sessions at 5 per block leaves a 2-session runt;
        dropping it would still satisfy 'blocks are contiguous'."""
        blocks = probe.contiguous_block_folds(self._sessions(27), block_sessions=5)
        self.assertEqual(len(blocks), 5)
        self.assertEqual([len(b) for b in blocks], [5, 5, 5, 5, 7])

    def test_an_exact_multiple_leaves_equal_blocks(self) -> None:
        blocks = probe.contiguous_block_folds(self._sessions(25), block_sessions=5)
        self.assertEqual([len(b) for b in blocks], [5, 5, 5, 5, 5])

    def test_blocks_stay_in_calendar_order(self) -> None:
        blocks = probe.contiguous_block_folds(self._sessions(27), block_sessions=5)
        self.assertEqual([b[0] for b in blocks], sorted(b[0] for b in blocks))
        for block in blocks:
            self.assertEqual(block, sorted(block))

    def test_fewer_sessions_than_one_block_is_a_single_fold(self) -> None:
        blocks = probe.contiguous_block_folds(self._sessions(3), block_sessions=5)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0]), 3)

    def test_unsorted_input_is_ordered_before_blocking(self) -> None:
        sessions = self._sessions(10)
        blocks = probe.contiguous_block_folds(list(reversed(sessions)), block_sessions=5)
        self.assertEqual([s for b in blocks for s in b], sessions)


if __name__ == "__main__":
    unittest.main()


def _synthetic_panel(n_clusters: int = 10, per_cluster: int = 6):
    """A panel with the columns the fit and the fold comparison need.

    Built so one regressor genuinely drives the outcome and the rest are
    noise, which is what lets the assertions below distinguish a working fit
    from one that returns anything at all.
    """
    rng = np.random.default_rng(7)
    sessions = [dt.date(2026, 6, 1) + dt.timedelta(days=i) for i in range(n_clusters)]
    rows = []
    for c, session in enumerate(sessions):
        for k in range(per_cluster):
            atr = float(rng.normal())
            rows.append(
                {
                    "brief_date": session,
                    "ticker": f"T{c}{k}",
                    "arrival": session,
                    "technical_atr_pct": atr,
                    "technical_ma50_distance_pct": float(rng.normal()),
                    "technical_ma200_slope_pct_per_day": float(rng.normal()),
                    "catalyst_strength": float(rng.normal()),
                    "catalyst_age_h": float(rng.normal()),
                    probe.LABEL: -0.4 * atr + float(rng.normal(scale=0.2)),
                }
            )
    return pd.DataFrame(rows)


_NAMES = [n for _, n in probe.CONTROLS] + [n for _, n in probe.CANDIDATES]


def _named(panel):
    out = panel.copy()
    for src, name in probe.CONTROLS + probe.CANDIDATES:
        source = out[src] if src in out.columns else out.get(name)
        out[name] = pd.to_numeric(source, errors="coerce")
    return out


class TestStandardise(unittest.TestCase):
    def test_each_column_ends_at_zero_mean_and_unit_spread(self) -> None:
        out = probe.standardise(_named(_synthetic_panel()), _NAMES)
        for name in _NAMES:
            self.assertAlmostEqual(out[name].mean(), 0.0, places=9)
            self.assertAlmostEqual(out[name].std(ddof=0), 1.0, places=9)

    def test_a_constant_column_becomes_nan_rather_than_dividing_by_zero(self) -> None:
        """Positive control — a naive implementation raises or returns inf."""
        panel = _named(_synthetic_panel())
        panel["atr"] = 3.0
        out = probe.standardise(panel, ["atr"])
        self.assertTrue(out["atr"].isna().all())

    def test_it_does_not_mutate_the_frame_it_was_given(self) -> None:
        panel = _named(_synthetic_panel())
        before = panel["atr"].tolist()
        probe.standardise(panel, _NAMES)
        self.assertEqual(panel["atr"].tolist(), before)


class TestJointFit(unittest.TestCase):
    def setUp(self) -> None:
        self._boot = probe.N_BOOT
        probe.N_BOOT = 199  # the bootstrap is the slow part; 199 is enough to exercise it

    def tearDown(self) -> None:
        probe.N_BOOT = self._boot

    def test_it_recovers_the_regressor_that_drives_the_outcome(self) -> None:
        out = probe.joint_fit(probe.standardise(_named(_synthetic_panel()), _NAMES), _NAMES)
        self.assertIsNotNone(out)
        assert out is not None
        by_name = {r["name"]: r for r in out}
        self.assertEqual(sorted(by_name), sorted(_NAMES))
        self.assertLess(by_name["atr"]["beta"], 0.0)
        self.assertLess(by_name["atr"]["p_wcb"], 0.05)

    def test_it_reports_clusters_not_rows(self) -> None:
        panel = probe.standardise(_named(_synthetic_panel(n_clusters=8, per_cluster=5)), _NAMES)
        out = probe.joint_fit(panel, _NAMES)
        assert out is not None
        self.assertEqual(out[0]["n"], 40)
        self.assertEqual(out[0]["clusters"], 8)

    def test_a_panel_too_small_to_fit_returns_none_rather_than_a_number(self) -> None:
        """Positive control — returning a fit on 12 rows would be worse than
        refusing, because the caller prints whatever it gets."""
        small = probe.standardise(_named(_synthetic_panel(n_clusters=3, per_cluster=4)), _NAMES)
        self.assertIsNone(probe.joint_fit(small, _NAMES))

    def test_rows_missing_a_regressor_are_dropped_complete_case(self) -> None:
        panel = probe.standardise(_named(_synthetic_panel()), _NAMES)
        panel.loc[panel.index[:7], "catalyst_age_h"] = np.nan
        out = probe.joint_fit(panel, _NAMES)
        assert out is not None
        self.assertEqual(out[0]["n"], len(panel) - 7)


class TestFoldComparison(unittest.TestCase):
    def test_it_scores_the_model_and_both_baselines_on_the_same_folds(self) -> None:
        panel = probe.standardise(_named(_synthetic_panel(n_clusters=20, per_cluster=6)), _NAMES)
        out = probe.fold_comparison(panel, _NAMES)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(sorted(out["scores"]), ["atr_ma50", "atr_only", "model"])
        self.assertEqual(len(out["per_fold"]), 4)
        for score in out["scores"].values():
            self.assertGreaterEqual(score, -1.0)
            self.assertLessEqual(score, 1.0)

    def test_the_fit_free_baseline_tracks_the_outcome_it_was_built_to_track(self) -> None:
        """The synthetic outcome is driven by -ATR, so the fixed-direction
        baseline must score positively. A sign error would show up here."""
        panel = probe.standardise(_named(_synthetic_panel(n_clusters=20, per_cluster=6)), _NAMES)
        out = probe.fold_comparison(panel, _NAMES)
        assert out is not None
        self.assertGreater(out["scores"]["atr_only"], 0.0)

    def test_too_few_sessions_to_split_returns_none(self) -> None:
        panel = probe.standardise(_named(_synthetic_panel(n_clusters=4, per_cluster=6)), _NAMES)
        self.assertIsNone(probe.fold_comparison(panel, _NAMES))


class TestRankWithinFold(unittest.TestCase):
    def test_every_fold_is_mapped_onto_the_same_zero_to_one_range(self) -> None:
        """This is what makes folds of different sizes poolable, and it is what
        README house rule 8 prescribes. Normalising by the POOLED total instead
        would compress a small fold into a slice of the range."""
        small = probe._rank_within_fold(np.array([3.0, 1.0, 2.0]))
        large = probe._rank_within_fold(np.arange(100.0))
        self.assertAlmostEqual(float(np.max(small)), 1.0)
        self.assertAlmostEqual(float(np.max(large)), 1.0)
        self.assertAlmostEqual(float(np.min(small)), 1 / 3)
        self.assertAlmostEqual(float(np.min(large)), 1 / 100)

    def test_it_ranks_rather_than_rescaling(self) -> None:
        got = probe._rank_within_fold(np.array([10.0, 1000.0, 20.0]))
        self.assertEqual(list(got), [1 / 3, 1.0, 2 / 3])


class TestOlsPredict(unittest.TestCase):
    def test_it_reproduces_a_line_it_was_fitted_on(self) -> None:
        frame = pd.DataFrame({"x": [0.0, 1.0, 2.0, 3.0]})
        y = np.array([1.0, 3.0, 5.0, 7.0])  # 1 + 2x
        got = probe._ols_predict(frame, frame, y, ["x"])
        for expected, actual in zip(y, got, strict=True):
            self.assertAlmostEqual(actual, expected, places=9)

    def test_it_scores_the_validation_frame_not_the_training_one(self) -> None:
        train = pd.DataFrame({"x": [0.0, 1.0, 2.0]})
        val = pd.DataFrame({"x": [10.0]})
        got = probe._ols_predict(train, val, np.array([0.0, 1.0, 2.0]), ["x"])
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(float(got[0]), 10.0, places=6)


class TestRunPrintsTheWholeReport(unittest.TestCase):
    """Smoke test of the reporting path over a synthetic panel.

    `build_panel` reads three stores and cannot run here, so it is replaced.
    Everything downstream of it is real, which is the half that formats the
    numbers a reader will act on.
    """

    def test_it_reports_every_section(self) -> None:
        panel = _named(_synthetic_panel(n_clusters=20, per_cluster=6))
        diag = {
            "pre_join": 200,
            "joined": 130,
            "episodes": len(panel),
            "clusters": 20,
            "no_brief": 70,
            "news_unresolved": 1,
            "pit_violations": 0,
        }
        boot = probe.N_BOOT
        build = probe.build_panel
        probe.N_BOOT = 199
        probe.build_panel = lambda: (panel, diag)
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                probe.run()
        finally:
            probe.N_BOOT = boot
            probe.build_panel = build
        out = buffer.getvalue()
        for expected in (
            "BURNT-PANEL NEWS-AXIS PROBE",
            "PRIMARY — jointly fitted",
            "PER-CANDIDATE",
            "SECONDARY — unpurged contiguous block folds. THIS LEAKS",
            "baseline A",
            "baseline B",
            "charges 0",
        ):
            self.assertIn(expected, out)
        self.assertIn("20 arrival-session clusters", out)
        self.assertIn("PIT violations (article at/after the arrival open) 0", out)
