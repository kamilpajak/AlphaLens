"""The top-10 theme cut is a coin toss, so it must be a FAIR one, and recorded.

Measured on 17 production days: the top-10 boundary is a tie on 12 of them, the
tied pool has median size 7, and 82 themes were dropped purely by whatever broke
the tie. What broke it was ``groupby(sort=True)`` followed by a stable
``sort_values`` — i.e. alphabetical order. ``ai_training_data`` beat
``yen_support`` on the first letter, every single day.

Two things follow. First, an alphabetical bias is not a policy anyone chose, so
replacing it costs nothing. Second, the selector is INDIFFERENT inside a tied
pool, which makes the tie the one place a randomised draw is free — and a
randomised draw is what turns "these ten were picked" into "each of these had
probability p of being picked", the only form in which a later off-policy
evaluation of an alternative rule is defined rather than merely noisy.

The whole thing hinges on one property: the pipeline runs SIX times a day on the
same ``asof`` and regenerates that date's parquets every time. A tie-break that
is not a pure function of (asof, theme) churns the day's output six times.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.thematic.extraction import themes

# Eight themes, one event each on the same recent day: identical count_window,
# count_recent and count_baseline, so identical novelty_score. A fully-tied pool
# is the ONLY fixture that can tell a tie-break apart from the primary sort.
_TIED_THEMES = [
    "aa_theme",
    "bb_theme",
    "cc_theme",
    "dd_theme",
    "ee_theme",
    "ff_theme",
    "gg_theme",
    "hh_theme",
]


def _event_row(news_id: str, asof: str, themes_list: list[str]) -> dict:
    return {
        "news_id": news_id,
        "event_type": "product_launch",
        "primary_entities": [],
        "themes": themes_list,
        "sentiment": "positive",
        "second_order_implications": [],
        "confidence": 0.8,
        "model": "deepseek-v4-flash",
        "extracted_at": pd.Timestamp(asof, tz="UTC"),
    }


def _write_tied_day(events_dir: Path, day: str, extra_rows: list[dict] | None = None) -> None:
    rows = [_event_row(f"n{i}", day, [theme]) for i, theme in enumerate(_TIED_THEMES)]
    rows.extend(extra_rows or [])
    pd.DataFrame(rows).to_parquet(events_dir / f"{day}.parquet", index=False)


class TestSeededTiebreak(unittest.TestCase):
    def test_a_fully_tied_pool_is_not_ordered_alphabetically(self):
        # The defect, stated as a test: identical scores must NOT resolve to
        # first-letter order. Fully tied on both sort keys, so nothing but the
        # tie-break can be under test here.
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            _write_tied_day(events_dir, "2026-05-15")
            rollup = themes.roll_up(
                asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30
            )

        self.assertEqual(set(rollup["theme"]), set(_TIED_THEMES))
        self.assertEqual(
            len(set(rollup["novelty_score"])), 1, "fixture must be fully tied to be meaningful"
        )
        self.assertNotEqual(list(rollup["theme"]), sorted(_TIED_THEMES))

    def test_the_same_asof_gives_the_same_order_every_time(self):
        # Six slots a day rebuild the same asof. If they disagree, the day's
        # selected themes churn and every downstream artifact is unstable.
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            _write_tied_day(events_dir, "2026-05-15")
            first = themes.roll_up(asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30)
            second = themes.roll_up(
                asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30
            )

        self.assertEqual(list(first["theme"]), list(second["theme"]))

    def test_a_different_asof_gives_a_different_order(self):
        # Positive control for the test above: a tie-break hard-wired to one
        # constant permutation would satisfy determinism vacuously.
        with tempfile.TemporaryDirectory() as tmpdir:
            events_dir = Path(tmpdir)
            _write_tied_day(events_dir, "2026-05-15")
            monday = themes.roll_up(
                asof=dt.date(2026, 5, 15), events_dir=events_dir, window_days=30
            )
            tuesday = themes.roll_up(
                asof=dt.date(2026, 5, 16), events_dir=events_dir, window_days=30
            )

        self.assertEqual(set(monday["theme"]), set(tuesday["theme"]))
        self.assertNotEqual(list(monday["theme"]), list(tuesday["theme"]))

    def test_a_theme_key_does_not_move_when_the_pool_changes(self):
        # `extract_daily` APPENDS to the same asof parquet on every slot, so the
        # set of tied themes is NOT fixed across the six runs of one day. A
        # tie-break that permutes the pool (shuffle by position) would reorder
        # every survivor when one theme joins or leaves. A per-theme key cannot.
        asof = dt.date(2026, 5, 15)
        keys = themes.tiebreak_keys(_TIED_THEMES, asof=asof)
        subset_keys = themes.tiebreak_keys(_TIED_THEMES[2:], asof=asof)

        self.assertEqual(list(subset_keys), list(keys[2:]))

    def test_the_key_survives_a_fresh_interpreter(self):
        # Python's builtin hash() on str is salted per process by PYTHONHASHSEED,
        # so a tie-break built on it would silently reshuffle every restart of
        # the container. Two interpreters with DIFFERENT salts must agree.
        script = (
            "from alphalens_pipeline.thematic.extraction import themes;"
            "import datetime as dt;"
            "print(','.join(themes.tiebreak_keys("
            "['aa_theme','bb_theme','cc_theme'], asof=dt.date(2026,5,15))))"
        )
        outputs = []
        for salt in ("0", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=salt)
            outputs.append(
                subprocess.run(
                    [sys.executable, "-c", script],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=env,
                ).stdout.strip()
            )

        self.assertTrue(outputs[0])
        self.assertEqual(outputs[0], outputs[1])

    def test_a_better_score_always_outranks_a_worse_one(self):
        # The tie-break is the LAST key, never the first. A theme with a strictly
        # higher novelty_score must lead on every asof, or randomisation has
        # leaked into the policy instead of staying inside its indifference set.
        for offset in range(10):
            asof = dt.date(2026, 5, 15) + dt.timedelta(days=offset)
            with tempfile.TemporaryDirectory() as tmpdir:
                events_dir = Path(tmpdir)
                _write_tied_day(
                    events_dir,
                    asof.isoformat(),
                    extra_rows=[
                        _event_row(f"hot{i}", asof.isoformat(), ["zz_hot_theme"]) for i in range(5)
                    ],
                )
                rollup = themes.roll_up(asof=asof, events_dir=events_dir, window_days=30)

            with self.subTest(asof=asof):
                self.assertEqual(rollup["theme"].iloc[0], "zz_hot_theme")

    def test_count_window_still_breaks_a_novelty_tie_before_the_seed(self):
        # The second sort key must keep its precedence: randomisation applies
        # only where BOTH existing keys are equal.
        rollup = pd.DataFrame(
            [
                {"theme": "aa_small", "novelty_score": 5.0, "count_window": 2},
                {"theme": "zz_big", "novelty_score": 5.0, "count_window": 40},
            ]
        )
        ordered = themes.apply_tiebreak(rollup, asof=dt.date(2026, 5, 15))

        self.assertEqual(list(ordered["theme"]), ["zz_big", "aa_small"])


class TestSelectionPropensity(unittest.TestCase):
    """Marginal inclusion probability per theme — not a joint slate probability."""

    def _rollup(self) -> pd.DataFrame:
        # Two themes clear the cut outright; four tie exactly at the boundary
        # score and share the two remaining slots; one is below the threshold.
        rows = [
            {"theme": "sure_a", "novelty_score": 9.0, "count_window": 9},
            {"theme": "sure_b", "novelty_score": 8.0, "count_window": 8},
            {"theme": "tied_a", "novelty_score": 4.0, "count_window": 4},
            {"theme": "tied_b", "novelty_score": 4.0, "count_window": 4},
            {"theme": "tied_c", "novelty_score": 4.0, "count_window": 4},
            {"theme": "tied_d", "novelty_score": 4.0, "count_window": 4},
            {"theme": "cold", "novelty_score": 0.5, "count_window": 1},
        ]
        return pd.DataFrame(rows)

    def _propensity(self, **kwargs) -> dict[str, float]:
        frame = self._rollup()
        values = themes.selection_propensity(frame, threshold=3.0, max_themes=4, **kwargs)
        return dict(zip(frame["theme"], values, strict=True))

    def test_a_theme_strictly_above_the_cut_is_certain(self):
        prop = self._propensity()
        self.assertEqual(prop["sure_a"], 1.0)
        self.assertEqual(prop["sure_b"], 1.0)

    def test_the_tied_pool_shares_the_remaining_slots(self):
        # 4 slots, 2 taken outright, 4 themes tied at the boundary -> 2/4.
        prop = self._propensity()
        for theme in ("tied_a", "tied_b", "tied_c", "tied_d"):
            self.assertAlmostEqual(prop[theme], 0.5, msg=theme)

    def test_a_theme_below_the_novelty_threshold_is_impossible(self):
        self.assertEqual(self._propensity()["cold"], 0.0)

    def test_the_propensities_add_up_to_the_slots_actually_filled(self):
        # The marginal probabilities of a fixed-size draw must sum to the size of
        # the draw. This is the arithmetic check that catches an off-by-one in
        # "slots remaining".
        self.assertAlmostEqual(sum(self._propensity().values()), 4.0)

    def test_every_eligible_theme_is_certain_when_the_cut_does_not_bind(self):
        frame = self._rollup()
        values = themes.selection_propensity(frame, threshold=3.0, max_themes=25)
        prop = dict(zip(frame["theme"], values, strict=True))

        self.assertEqual(prop["tied_a"], 1.0)
        self.assertEqual(prop["cold"], 0.0)
        self.assertAlmostEqual(sum(prop.values()), 6.0)

    def test_the_propensity_does_not_depend_on_the_seed(self):
        # It is an EX-ANTE probability over the draw, so it must be identical on
        # every asof even though the realised selection is not.
        frame = self._rollup()
        a = list(themes.selection_propensity(frame, threshold=3.0, max_themes=4))
        shuffled = themes.apply_tiebreak(frame, asof=dt.date(2027, 1, 1))
        b = list(themes.selection_propensity(shuffled, threshold=3.0, max_themes=4))

        self.assertEqual(sorted(a), sorted(b))


class TestRollupRecordsTheDraw(unittest.TestCase):
    def _write(self, out_dir: Path) -> pd.DataFrame:
        rollup = pd.DataFrame(
            [
                {
                    "theme": "aa_theme",
                    "count_window": 4,
                    "count_recent": 4,
                    "count_baseline": 0,
                    "novelty_score": 4.0,
                    "rate_surprise": 2.0,
                    "excess_activity": 3.0,
                    "first_seen": pd.Timestamp("2026-08-05", tz="UTC"),
                    "latest_seen": pd.Timestamp("2026-08-05", tz="UTC"),
                },
                {
                    "theme": "bb_theme",
                    "count_window": 4,
                    "count_recent": 4,
                    "count_baseline": 0,
                    "novelty_score": 4.0,
                    "rate_surprise": 2.0,
                    "excess_activity": 3.0,
                    "first_seen": pd.Timestamp("2026-08-05", tz="UTC"),
                    "latest_seen": pd.Timestamp("2026-08-05", tz="UTC"),
                },
            ]
        )
        themes.write_theme_rollup(
            dt.date(2026, 8, 5),
            themes.apply_tiebreak(rollup, asof=dt.date(2026, 8, 5)),
            selected=["aa_theme"],
            out_dir=out_dir,
            novelty_config_version="cfg-token",
            threshold=3.0,
            max_themes=1,
        )
        return pd.read_parquet(out_dir / "2026-08-05.parquet")

    def test_the_file_carries_the_seed_the_propensity_and_the_rule_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            df = self._write(Path(tmpdir))

        for column in ("selection_propensity", "tiebreak_seed", "tiebreak_version"):
            self.assertIn(column, df.columns)
            self.assertTrue(df[column].notna().all(), f"{column} shipped all-null")
        self.assertEqual(set(df["tiebreak_seed"]), {themes.tiebreak_seed(dt.date(2026, 8, 5))})
        self.assertEqual(set(df["tiebreak_version"]), {themes.TIEBREAK_VERSION})

    def test_the_two_tied_themes_each_had_half_a_chance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            df = self._write(Path(tmpdir))
        prop = dict(zip(df["theme"], df["selection_propensity"], strict=True))

        self.assertAlmostEqual(prop["aa_theme"], 0.5)
        self.assertAlmostEqual(prop["bb_theme"], 0.5)

    def test_no_declared_column_is_silently_absent(self):
        # `reindex` in the writer turns a renamed key into an all-null float
        # column with no error, so the schema must be asserted, not assumed.
        with tempfile.TemporaryDirectory() as tmpdir:
            df = self._write(Path(tmpdir))

        self.assertEqual(list(df.columns), list(themes.THEME_ROLLUP_COLUMNS))
        self.assertTrue(df["tiebreak_key"].notna().all())


class TestNoveltyConfigVersionCarriesTheTiebreak(unittest.TestCase):
    def test_the_token_names_the_tiebreak_rule(self):
        # Two selection rules that produce different picks from the same counts
        # must not be indistinguishable in stored data.
        token = themes.novelty_config_version(window_days=30, recent_days=7, threshold=3.0)
        payload = json.loads(token)

        self.assertEqual(payload["tiebreak"], themes.TIEBREAK_VERSION)


if __name__ == "__main__":
    unittest.main()
