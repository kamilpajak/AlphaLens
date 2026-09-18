"""The /edge population on a date whose stored brief was written after the open (#1494 step 2).

On 14 as-of dates the stored brief is not the list a reader could have acted on. The seam
here hands every pass that writes into the population-ladder store the list that WAS on
screen at the arrival open: names the later run dropped come back, names it added leave.

The dates and names are the real recovered record, so a change to that record shows up here
rather than in a synthetic fixture that agrees with itself.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from alphalens_pipeline.feedback import pre_open_population
from alphalens_pipeline.thematic.pre_open_setup import PRE_OPEN_SETUPS, pre_open_setup

# 2026-06-04 recovered CRL, FDS, GME, IRDM. The brief still stores CRL and IRDM, so FDS and
# GME are the two names that had to be rebuilt.
RECOVERED_DATE = dt.date(2026, 6, 4)
KEPT = "CRL"
REBUILT = "FDS"
# A date the recovery could not answer, so the seam must not touch it.
UNTOUCHED_DATE = dt.date(2026, 6, 9)

_SETUP = {
    "schema_version": "1.1.0",
    "status": "OK",
    "asof_close": 100.0,
    "atr": 2.0,
    "disaster_stop": 90.0,
    "suggested_size_pct": 3.0,
    "order_ttl_days": 10,
    "entry_tiers": [{"limit": 99.0, "alloc_pct": 100.0, "tag": "shallow pullback"}],
    "tp_tranches": [{"target": 110.0, "tranche_pct": 100.0}],
}


def _write_brief(briefs_dir: Path, brief_date: dt.date, tickers: list[str]) -> None:
    briefs_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "ticker": ticker,
                "theme": "ai",
                "verified": True,
                "suggested_size_pct": 3.0,
                "layer4_weighted_score": 7.5,
                "scorer_config_version": "scorer-v9",
                "source": "thematic",
                "event_overlap": False,
                "brief_trade_setup": json.dumps(_SETUP),
            }
            for ticker in tickers
        ]
    ).to_parquet(briefs_dir / f"{brief_date.isoformat()}.parquet")


class ADateWithNoRecoveredListIsUntouchedTest(unittest.TestCase):
    def test_it_returns_the_stored_brief_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            briefs = Path(tmp)
            _write_brief(briefs, UNTOUCHED_DATE, ["NVDA", "AMD"])
            got = pre_open_population.load_brief_for_population(UNTOUCHED_DATE, briefs)
            self.assertEqual([c.ticker for c in got], ["NVDA", "AMD"])
            self.assertEqual(got[0].theme, "ai")
            self.assertEqual(got[0].scorer_config_version, "scorer-v9")

    def test_a_missing_brief_still_raises(self) -> None:
        # The monitor catches this to skip the date; swallowing it here would turn a
        # missing brief into "no candidates that day".
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                pre_open_population.load_brief_for_population(UNTOUCHED_DATE, Path(tmp))


class ARecoveredDateIsRebuiltFromTheOpenListTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.briefs = Path(self._tmp.name)
        # What the store holds today: the kept name, plus one the later run added.
        _write_brief(self.briefs, RECOVERED_DATE, [KEPT, "LATEADD"])
        self.got = pre_open_population.load_brief_for_population(RECOVERED_DATE, self.briefs)
        self.by_ticker = {c.ticker: c for c in self.got}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_population_is_the_recovered_list_in_its_recorded_order(self) -> None:
        self.assertEqual([c.ticker for c in self.got], ["CRL", "FDS", "GME", "IRDM"])

    def test_a_name_added_after_the_open_is_gone(self) -> None:
        self.assertNotIn("LATEADD", self.by_ticker)

    def test_a_kept_name_keeps_everything_the_brief_says(self) -> None:
        kept = self.by_ticker[KEPT]
        self.assertEqual(kept.theme, "ai")
        self.assertEqual(kept.scorer_config_version, "scorer-v9")
        self.assertEqual(kept.layer4_weighted_score, 7.5)
        self.assertEqual(kept.trade_setup, _SETUP)

    def test_a_rebuilt_name_carries_the_frozen_setup(self) -> None:
        rebuilt = self.by_ticker[REBUILT]
        self.assertEqual(rebuilt.trade_setup, pre_open_setup(RECOVERED_DATE, REBUILT))

    def test_a_rebuilt_name_is_verified_so_the_ladder_plans_it(self) -> None:
        self.assertTrue(self.by_ticker[REBUILT].verified)

    def test_a_rebuilt_name_claims_no_theme_and_no_scorer(self) -> None:
        # The journal recorded names, not themes, and not which scorer config ranked them.
        rebuilt = self.by_ticker[REBUILT]
        self.assertEqual(rebuilt.theme, "")
        self.assertEqual(rebuilt.scorer_config_version, "")
        self.assertIsNone(rebuilt.layer4_weighted_score)

    def test_a_rebuilt_name_stays_in_the_thematic_lane(self) -> None:
        # ``source`` is the candidate lane and the event-lane cohorts filter on it; a
        # reconstruction marker does not belong in it.
        self.assertEqual(self.by_ticker[REBUILT].source, "thematic")
        self.assertFalse(self.by_ticker[REBUILT].event_overlap)

    def test_a_rebuilt_name_takes_its_size_from_the_frozen_setup(self) -> None:
        rebuilt = self.by_ticker[REBUILT]
        assert rebuilt.trade_setup is not None
        self.assertEqual(rebuilt.suggested_size_pct, rebuilt.trade_setup["suggested_size_pct"])

    def test_every_row_carries_the_brief_date(self) -> None:
        for candidate in self.got:
            self.assertEqual(candidate.brief_date, RECOVERED_DATE)


class ARecoveredNameWithNoFrozenSetupIsNotPlannableTest(unittest.TestCase):
    def test_it_comes_back_without_a_setup(self) -> None:
        # No new fallback is introduced for a name the record cannot serve: it takes the
        # non-plannable path the existing code already gives a setup-less candidate.
        with tempfile.TemporaryDirectory() as tmp:
            briefs = Path(tmp)
            _write_brief(briefs, RECOVERED_DATE, [KEPT])
            got = pre_open_population.load_brief_for_population(RECOVERED_DATE, briefs, setups={})
            missing = {c.ticker: c for c in got}[REBUILT]
            self.assertIsNone(missing.trade_setup)
            self.assertTrue(missing.verified)


class EveryPassThatWritesTheStoreUsesTheSeamTest(unittest.TestCase):
    """The replay is not the only pass that turns a brief into store columns.

    The size overlay and the chart payload each look a store row's ticker up in the brief.
    Left on the raw loader they would find nothing for a re-added name and leave its columns
    NULL, so /edge would show the recovered row with an empty ``% book`` and no chart.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.briefs = Path(self._tmp.name)
        _write_brief(self.briefs, RECOVERED_DATE, [KEPT, "LATEADD"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_size_pass_finds_a_setup_for_a_rebuilt_name(self) -> None:
        from alphalens_pipeline.feedback import population_ladder_monitor

        setups = population_ladder_monitor._load_setups_for_date(RECOVERED_DATE, self.briefs)
        assert setups is not None
        self.assertIn(REBUILT, setups)
        self.assertNotIn("LATEADD", setups)

    def test_the_chart_pass_finds_a_setup_for_a_rebuilt_name(self) -> None:
        from alphalens_pipeline.feedback import ladder_chart

        setups = ladder_chart._load_setups_for_date(RECOVERED_DATE, self.briefs)
        assert setups is not None
        self.assertIn(REBUILT, setups)
        self.assertNotIn("LATEADD", setups)

    def test_an_untouched_date_is_unchanged_in_both_passes(self) -> None:
        from alphalens_pipeline.feedback import ladder_chart, population_ladder_monitor

        _write_brief(self.briefs, UNTOUCHED_DATE, ["NVDA"])
        for module in (population_ladder_monitor, ladder_chart):
            setups = module._load_setups_for_date(UNTOUCHED_DATE, self.briefs)
            assert setups is not None
            self.assertEqual(sorted(setups), ["NVDA"])


class TheRebuiltCandidateIsIndependentOfTheRecordTest(unittest.TestCase):
    def test_mutating_a_returned_setup_does_not_change_the_frozen_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            briefs = Path(tmp)
            _write_brief(briefs, RECOVERED_DATE, [KEPT])
            got = pre_open_population.load_brief_for_population(RECOVERED_DATE, briefs)
            rebuilt = {c.ticker: c for c in got}[REBUILT]
            assert rebuilt.trade_setup is not None
            rebuilt.trade_setup["disaster_stop"] = -1.0
            self.assertNotEqual(PRE_OPEN_SETUPS[RECOVERED_DATE][REBUILT]["disaster_stop"], -1.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
