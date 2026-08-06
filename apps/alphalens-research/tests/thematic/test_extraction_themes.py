import datetime as dt
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.thematic.extraction import themes


def _event_row(news_id, asof, themes_list, primary=None, confidence=0.8):
    return {
        "news_id": news_id,
        "event_type": "product_launch",
        "primary_entities": primary or [],
        "themes": themes_list,
        "sentiment": "positive",
        "second_order_implications": [],
        "confidence": confidence,
        "model": "gemini-2.5-flash",
        "extracted_at": pd.Timestamp(asof, tz="UTC"),
    }


class TestRollUp(unittest.TestCase):
    def _write(self, events_dir: Path, date: dt.date, rows: list[dict]):
        df = pd.DataFrame(rows)
        df.to_parquet(events_dir / f"{date.isoformat()}.parquet", index=False)

    def test_collects_themes_across_days(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            self._write(
                events_dir,
                dt.date(2026, 5, 10),
                [_event_row("a", "2026-05-10", ["quantum_computing", "AI"])],
            )
            self._write(
                events_dir,
                dt.date(2026, 5, 15),
                [_event_row("b", "2026-05-15", ["quantum_computing", "biotech"])],
            )

            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30)

            counts = dict(zip(df["theme"], df["count_window"], strict=True))
            self.assertEqual(counts["quantum_computing"], 2)
            self.assertEqual(counts["ai"], 1)  # "AI" slugged to "ai"
            self.assertEqual(counts["biotech"], 1)

    def test_format_variants_collapse_to_one_slug(self):
        # The same concept written two ways ("AI ethics" vs "AI_ethics") must
        # count as ONE theme: the rollup slugifies on read, so a format change
        # never spuriously splits a theme (or flags it novel) across the window.
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            self._write(
                events_dir,
                dt.date(2026, 5, 10),
                [_event_row("a", "2026-05-10", ["AI ethics"])],
            )
            self._write(
                events_dir,
                dt.date(2026, 5, 14),
                [_event_row("b", "2026-05-14", ["AI_ethics"])],
            )

            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30)

            counts = dict(zip(df["theme"], df["count_window"], strict=True))
            self.assertEqual(counts, {"ai_ethics": 2})

    def test_novelty_score_uses_7d_over_30d_ratio(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            # Backfill 'cybersecurity' steadily across 30 days, then a spike for 'quantum' in last 7
            for d in range(30):
                date = dt.date(2026, 5, 15) - dt.timedelta(days=d)
                self._write(
                    events_dir,
                    date,
                    [_event_row(f"cs_{d}", date.isoformat(), ["cybersecurity"])],
                )
            for d in range(7):
                date = dt.date(2026, 5, 15) - dt.timedelta(days=d)
                self._write(
                    events_dir,
                    date,
                    [
                        _event_row(f"cs_{d}_b", date.isoformat(), ["cybersecurity"]),
                        _event_row(f"q_{d}", date.isoformat(), ["quantum_computing"]),
                    ],
                )

            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30)
            df_indexed = df.set_index("theme")

            # Cybersecurity steady → low novelty
            self.assertLess(df_indexed.loc["cybersecurity", "novelty_score"], 1.5)
            # Quantum spike → high novelty (≥3x baseline)
            self.assertGreaterEqual(df_indexed.loc["quantum_computing", "novelty_score"], 3.0)

    def test_first_seen_and_latest_seen_dates_tracked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            self._write(
                events_dir,
                dt.date(2026, 5, 10),
                [_event_row("a", "2026-05-10", ["AI"])],
            )
            self._write(
                events_dir,
                dt.date(2026, 5, 14),
                [_event_row("b", "2026-05-14", ["AI"])],
            )

            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30)
            ai = df[df["theme"] == "ai"].iloc[0]
            self.assertEqual(ai["first_seen"].date(), dt.date(2026, 5, 10))
            self.assertEqual(ai["latest_seen"].date(), dt.date(2026, 5, 14))

    def test_ignores_events_outside_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            # event 60 days ago, well outside default 30d window
            old_date = dt.date(2026, 5, 15) - dt.timedelta(days=60)
            self._write(events_dir, old_date, [_event_row("old", old_date.isoformat(), ["AI"])])

            self._write(
                events_dir,
                dt.date(2026, 5, 14),
                [_event_row("new", "2026-05-14", ["biotech"])],
            )

            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30)
            self.assertEqual(set(df["theme"]), {"biotech"})

    def test_empty_events_dir_returns_empty_frame(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=Path(tmpdir), window_days=30)
            self.assertEqual(len(df), 0)
            for col in ["theme", "count_window", "novelty_score", "first_seen", "latest_seen"]:
                self.assertIn(col, df.columns)

    def test_flag_novel_uses_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            # Mix one steady + one spiking
            for d in range(30):
                date = dt.date(2026, 5, 15) - dt.timedelta(days=d)
                self._write(events_dir, date, [_event_row(f"s_{d}", date.isoformat(), ["steady"])])
            for d in range(3):
                date = dt.date(2026, 5, 15) - dt.timedelta(days=d)
                self._write(
                    events_dir, date, [_event_row(f"q_{d}", date.isoformat(), ["novel_theme"])]
                )

            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30)
            novel = themes.flag_novel(df, threshold=3.0)
            self.assertIn("novel_theme", set(novel["theme"]))
            self.assertNotIn("steady", set(novel["theme"]))


class TestRateSurprise(unittest.TestCase):
    """The growth ratio is blind to how much evidence stands behind it.

    With the production constants (window 30, recent 7, baseline 23, threshold 3.0)
    a theme with 3 recent articles against a baseline of 1 scores 9.86 and passes,
    while 120 recent against a baseline of 200 scores 1.97 and fails — three
    articles outrank a hundred and twenty. Worse, 30/50, 60/100 and 120/200 all
    score IDENTICALLY at 1.97, so the measure cannot see sample size at all. The
    rate-surprise score answers the question the ratio was standing in for: how
    unlikely is THIS many articles under the rate the baseline implies.

    Telemetry only — nothing selects on it yet.
    """

    RECENT_DAYS = 7
    BASELINE_DAYS = 23

    def _score(self, count_recent: int, count_baseline: int) -> float:
        return themes.rate_surprise(
            count_recent,
            count_baseline,
            recent_days=self.RECENT_DAYS,
            baseline_days=self.BASELINE_DAYS,
        )

    def test_a_large_sample_outranks_a_tiny_one_the_ratio_prefers(self):
        # The exact inversion measured on 2026-08-06 production data.
        tiny = self._score(3, 1)
        large = self._score(120, 200)
        self.assertGreater(
            large, tiny, "120 recent against a 200 baseline must outrank 3 against 1"
        )

    def test_identical_ratios_are_not_identical_scores(self):
        # 30/50, 60/100 and 120/200 are the same ratio and the same verdict under
        # the old measure. They are NOT the same amount of evidence.
        weak = self._score(30, 50)
        medium = self._score(60, 100)
        strong = self._score(120, 200)
        self.assertLess(weak, medium)
        self.assertLess(medium, strong)

    def test_a_steady_theme_scores_low_regardless_of_its_size(self):
        # A theme arriving at a constant rate is not news. Both a small and a
        # large steady theme must sit near the bottom, which is what lets a
        # genuine burst inside a large theme stand out.
        small_steady = self._score(3, 10)
        large_steady = self._score(30, 100)
        self.assertLess(small_steady, 3.0)
        self.assertLess(large_steady, 3.0)

    def test_more_recent_articles_score_higher_for_the_same_baseline(self):
        baseline = 50
        scores = [self._score(k, baseline) for k in (10, 20, 40, 80)]
        self.assertEqual(scores, sorted(scores))

    def test_a_zero_baseline_does_not_produce_an_infinite_score(self):
        # A brand-new theme has no baseline at all. Without smoothing the tail
        # probability is 0 and the score is +inf, which would make every
        # first-sighting singleton beat everything else forever — the very
        # failure mode this replaces.
        score = self._score(3, 0)
        self.assertTrue(math.isfinite(score))

    def test_no_articles_at_all_scores_zero_not_nan(self):
        self.assertEqual(self._score(0, 0), 0.0)

    def test_extreme_counts_stay_ordered_instead_of_flattening(self):
        # Computed on the linear tail, these all underflow to p == 0 and collapse
        # onto one plateau, so a 500-article week would rank equal to a 1000-article
        # one. The log-space tail keeps them apart.
        scores = [self._score(k, 100) for k in (300, 500, 800, 1200)]
        self.assertEqual(scores, sorted(scores))
        self.assertTrue(all(math.isfinite(s) for s in scores))
        self.assertEqual(len(set(scores)), len(scores), "extreme counts must not tie")

    def test_roll_up_carries_the_score_for_every_theme(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            df = pd.DataFrame(
                [
                    _event_row("a", "2026-05-14", ["quantum_computing"]),
                    _event_row("b", "2026-05-15", ["quantum_computing"]),
                    _event_row("c", "2026-05-15", ["biotech"]),
                ]
            )
            df.to_parquet(events_dir / "2026-05-15.parquet", index=False)
            rollup = themes.roll_up(
                asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30
            )
        self.assertIn("rate_surprise", rollup.columns)
        self.assertTrue(rollup["rate_surprise"].notna().all())
        # The existing ratio still decides the ordering — this column is telemetry.
        self.assertEqual(
            list(rollup["theme"]),
            list(rollup.sort_values(["novelty_score", "count_window"], ascending=False)["theme"]),
        )


class TestWriteThemeRollup(unittest.TestCase):
    """The daily rollup is persisted for EVERY theme, not just the selected ten.

    Selection today keeps the top ten themes by growth ratio and discards the
    rest without a trace, so "what would a different rule have picked on
    2026-07-31?" is unanswerable after the fact. Storing every theme with its
    counts and both scores makes any policy that is a function of those numbers
    replayable from disk — no LLM calls, no re-run.
    """

    def _rollup(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "theme": "box_office",
                    "count_window": 4,
                    "count_recent": 3,
                    "count_baseline": 1,
                    "novelty_score": 9.86,
                    "rate_surprise": 1.95,
                    "excess_activity": 2.7,
                    "first_seen": pd.Timestamp("2026-07-30", tz="UTC"),
                    "latest_seen": pd.Timestamp("2026-08-05", tz="UTC"),
                },
                {
                    "theme": "quantum_computing",
                    "count_window": 320,
                    "count_recent": 120,
                    "count_baseline": 200,
                    "novelty_score": 1.97,
                    "rate_surprise": 13.7,
                    "excess_activity": 59.1,
                    "first_seen": pd.Timestamp("2026-07-07", tz="UTC"),
                    "latest_seen": pd.Timestamp("2026-08-05", tz="UTC"),
                },
            ]
        )

    def _write(self, out_dir: Path, selected: list[str]):
        themes.write_theme_rollup(
            dt.date(2026, 8, 5),
            self._rollup(),
            selected=selected,
            out_dir=out_dir,
            novelty_config_version="cfg-token",
        )
        return pd.read_parquet(out_dir / "2026-08-05.parquet")

    def test_writes_every_theme_not_only_the_selected_ones(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            df = self._write(Path(tmpdir), ["box_office"])
        self.assertEqual(set(df["theme"]), {"box_office", "quantum_computing"})

    def test_marks_which_themes_were_actually_mapped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            df = self._write(Path(tmpdir), ["box_office"])
        selected = dict(zip(df["theme"], df["selected"], strict=True))
        self.assertTrue(selected["box_office"])
        self.assertFalse(selected["quantum_computing"])

    def test_ranks_each_score_independently_so_the_two_can_be_compared(self):
        # The whole point: on this day the ratio ranks box_office first and the
        # surprise score ranks quantum_computing first. Both orders must survive.
        with tempfile.TemporaryDirectory() as tmpdir:
            df = self._write(Path(tmpdir), ["box_office"])
        by_theme = df.set_index("theme")
        self.assertEqual(by_theme.loc["box_office", "novelty_rank"], 1)
        self.assertEqual(by_theme.loc["quantum_computing", "novelty_rank"], 2)
        self.assertEqual(by_theme.loc["quantum_computing", "rate_surprise_rank"], 1)
        self.assertEqual(by_theme.loc["box_office", "rate_surprise_rank"], 2)
        # Third score ranks independently of the other two, and disagrees with the ratio.
        self.assertEqual(by_theme.loc["quantum_computing", "excess_activity_rank"], 1)
        self.assertEqual(by_theme.loc["box_office", "excess_activity_rank"], 2)
        self.assertEqual(by_theme.loc["box_office", "novelty_rank"], 1)

    def test_keeps_the_first_and_last_seen_dates_for_replay(self):
        # roll_up already computes them; dropping them at the write would cost a
        # replay the ability to ask "was this theme first seen yesterday?".
        with tempfile.TemporaryDirectory() as tmpdir:
            df = self._write(Path(tmpdir), ["box_office"])
        self.assertIn("first_seen", df.columns)
        self.assertIn("latest_seen", df.columns)
        self.assertTrue(df["first_seen"].notna().all())

    def test_stamps_the_asof_and_the_novelty_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            df = self._write(Path(tmpdir), [])
        self.assertEqual(set(df["asof"].astype(str)), {"2026-08-05"})
        self.assertEqual(set(df["novelty_config_version"]), {"cfg-token"})

    def test_an_empty_rollup_writes_no_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            themes.write_theme_rollup(
                dt.date(2026, 8, 5),
                pd.DataFrame(),
                selected=[],
                out_dir=out,
                novelty_config_version="cfg-token",
            )
            self.assertEqual(list(out.glob("*.parquet")), [])


class TestExcessActivity(unittest.TestCase):
    """Third score beside the ratio and the Poisson tail. Telemetry only.

    The ratio and the tail probability both measure RELATIVE acceleration, so both
    rank a theme that went 0 -> 6 above a large theme running 1.5x its baseline.
    Excess activity asks a different question on the count scale: how many more
    recent articles than the baseline rate implies. Measured on one production day
    it reorders the slate completely, which is exactly why nothing selects on it
    until there is downstream yield data to choose between the three.
    """

    def _write(self, events_dir: Path, date: dt.date, rows: list[dict]):
        pd.DataFrame(rows).to_parquet(events_dir / f"{date.isoformat()}.parquet", index=False)

    def test_excess_is_recent_minus_the_baseline_rate_scaled_to_the_recent_window(self):
        # 40 recent against 86 baseline over 23 days: expected 7/23*86 = 26.2, excess 13.8.
        self.assertAlmostEqual(
            themes.excess_activity(40, 86, recent_days=7, baseline_days=23), 13.8, places=1
        )

    def test_a_theme_below_its_baseline_rate_has_negative_excess(self):
        self.assertLess(themes.excess_activity(48, 165, recent_days=7, baseline_days=23), 0.0)

    def test_a_zero_baseline_makes_excess_the_recent_count(self):
        self.assertAlmostEqual(
            themes.excess_activity(6, 0, recent_days=7, baseline_days=23), 6.0, places=6
        )

    def test_it_ranks_a_large_mildly_elevated_theme_above_a_small_burst(self):
        # The ordering the ratio and the tail probability both get the other way round.
        big = themes.excess_activity(40, 86, recent_days=7, baseline_days=23)
        small = themes.excess_activity(6, 0, recent_days=7, baseline_days=23)
        self.assertGreater(big, small)

    def test_a_zero_recent_window_clamps_instead_of_zeroing_the_expectation(self):
        # Unguarded, recent_days=0 makes the expectation 0 and the excess equal to the
        # raw recent count -- a plausible-looking number for an undefined quantity.
        # Clamp to one day, matching the ratio's own max(recent_days, 1).
        self.assertEqual(
            themes.excess_activity(6, 10, recent_days=0, baseline_days=23),
            themes.excess_activity(6, 10, recent_days=1, baseline_days=23),
        )
        self.assertNotEqual(themes.excess_activity(6, 10, recent_days=0, baseline_days=23), 6.0)

    def test_roll_up_carries_excess_for_every_theme(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            self._write(
                events_dir,
                dt.date(2026, 5, 15),
                [_event_row("a", "2026-05-15", ["quantum_computing"])],
            )

            df = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30)

            self.assertIn("excess_activity", df.columns)
            self.assertTrue(df["excess_activity"].notna().all())

    def test_tied_excess_shares_a_rank_and_the_next_theme_skips(self):
        # method="min" keeps "rank r means r-1 themes scored strictly higher". A switch
        # to dense or first ranking would silently break that and go unnoticed, because
        # a large share of themes tie at the same small excess.
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            themes.write_theme_rollup(
                dt.date(2026, 8, 5),
                pd.DataFrame(
                    [
                        {
                            "theme": "a",
                            "novelty_score": 1.0,
                            "rate_surprise": 1.0,
                            "excess_activity": 5.0,
                        },
                        {
                            "theme": "b",
                            "novelty_score": 1.0,
                            "rate_surprise": 1.0,
                            "excess_activity": 5.0,
                        },
                        {
                            "theme": "c",
                            "novelty_score": 1.0,
                            "rate_surprise": 1.0,
                            "excess_activity": 1.0,
                        },
                    ]
                ),
                selected=[],
                out_dir=out,
                novelty_config_version="cfg",
            )
            ranks = (
                pd.read_parquet(out / "2026-08-05.parquet")
                .set_index("theme")["excess_activity_rank"]
                .to_dict()
            )

        self.assertEqual(ranks["a"], 1)
        self.assertEqual(ranks["b"], 1)
        self.assertEqual(ranks["c"], 3)

    def test_selection_is_untouched_by_the_new_score(self):
        # flag_novel still gates on the ratio: this PR adds a column, not a policy.
        rollup = pd.DataFrame(
            [
                {"theme": "small_burst", "novelty_score": 9.9, "excess_activity": 3.0},
                {"theme": "large_steady", "novelty_score": 1.5, "excess_activity": 99.0},
            ]
        )

        novel = themes.flag_novel(rollup, threshold=3.0)

        self.assertEqual(list(novel["theme"]), ["small_burst"])


class TestNoveltyConfigVersion(unittest.TestCase):
    """The novelty config token pins window/recent/threshold so a future tune of
    those params makes pre- vs post-change novelty values non-poolable on purpose
    (mirrors mapper_config_version / ladder_config_version / panel_config_version).
    """

    def test_stable_for_identical_inputs(self):
        a = themes.novelty_config_version(window_days=30, recent_days=7, threshold=3.0)
        b = themes.novelty_config_version(window_days=30, recent_days=7, threshold=3.0)
        self.assertEqual(a, b)

    def test_changes_with_window_days(self):
        a = themes.novelty_config_version(window_days=30, recent_days=7, threshold=3.0)
        b = themes.novelty_config_version(window_days=45, recent_days=7, threshold=3.0)
        self.assertNotEqual(a, b)

    def test_changes_with_recent_days(self):
        a = themes.novelty_config_version(window_days=30, recent_days=7, threshold=3.0)
        b = themes.novelty_config_version(window_days=30, recent_days=5, threshold=3.0)
        self.assertNotEqual(a, b)

    def test_changes_with_threshold(self):
        a = themes.novelty_config_version(window_days=30, recent_days=7, threshold=3.0)
        b = themes.novelty_config_version(window_days=30, recent_days=7, threshold=2.5)
        self.assertNotEqual(a, b)

    def test_is_canonical_json_with_schema_marker(self):
        import json

        token = themes.novelty_config_version(window_days=30, recent_days=7, threshold=3.0)
        payload = json.loads(token)
        self.assertIn("schema", payload)
        self.assertEqual(payload["window_days"], 30)
        self.assertEqual(payload["recent_days"], 7)
        self.assertEqual(payload["threshold"], 3.0)
        # canonical: sorted keys, compact separators → byte-stable across runs
        self.assertEqual(token, json.dumps(payload, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    unittest.main()
