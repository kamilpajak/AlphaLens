"""Unit tests for the shared ticker-episode panel loader.

The loader exists because four scripts under ``scripts/ml/`` each carried their
own copy of the same eight-step chain, and the copies had already drifted: one
joined a native ``dt.date`` against a filename-derived string and matched zero
rows, one filtered the maturity status after the episode collapse, and none of
them pinned ``sel_label_version``. These cases pin the parts that drifted.

Every case here is outcome-blind in the sense the research gates use: it writes
its own synthetic parquets into a temporary directory and never reads a real
store. No case may call a registered script's run path.
"""

from __future__ import annotations

import datetime as dt
import inspect
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np
import pandas as pd
from alphalens_research.diagnostics import episode_panel as ep

# --------------------------------------------------------------- store shape
LABELS_DIRNAME = "selection_labels"
BRIEFS_DIRNAME = "thematic_briefs"

STATUS_COLUMN = "sel_label_status_20"
ANCHOR_COLUMN = "anchor_session"
VERSION_COLUMN = "sel_label_version"
OUTCOME = "sel_ar_20"
SECOND_OUTCOME = "sel_ar_10"
ATR = "technical_atr_pct"

STATUS_OK = "ok"
STATUS_IMMATURE = "immature"

VERSION_V3 = "sel-label-v3"
VERSION_V2 = "sel-label-v2"
VERSION_ABSENT = "sel-label-v9"
UNKNOWN_COLUMN = "no_such_version"

CUTOFF = "2026-07-05"
BURNT_DATE = "2026-07-03"
CUTOFF_EXCLUSIVE_DATE = "2026-07-05"
FIRST_HELD_OUT_DATE = "2026-07-06"

# A Wednesday, and an NYSE session in its own right. Paired with an anchor on
# the NEXT session it separates "the stored arrival" from "the arrival the
# calendar would compute", which owner decision D4 says must differ.
SESSION_BRIEF_DATE = "2026-09-02"
SESSION_NEXT_DAY = "2026-09-03"

# The chained-repeat fixture, lifted from test_a20_atr_confirmation so the two
# loaders are measured against the same numbers.
CHAIN_SESSIONS = 8
CHAIN_PER_SESSION = 4
CHAIN_ROWS = CHAIN_SESSIONS * CHAIN_PER_SESSION
CHAIN_REPEATS = CHAIN_SESSIONS - 1
CHAIN_EPISODES = CHAIN_ROWS - CHAIN_REPEATS

ARRIVAL = "arrival"


def _sessions(count: int, start: str = FIRST_HELD_OUT_DATE) -> list[str]:
    from alphalens_pipeline.market.calendar import advance_trading_sessions

    first = dt.date.fromisoformat(start)
    return [advance_trading_sessions(first, i).isoformat() for i in range(count)]


class _Store:
    """Two directories of per-date parquets shaped like the real stores.

    Two real properties of the stores are reproduced deliberately, because both
    have caused a silent miss: the label store stamps ``brief_date`` as a native
    ``dt.date``, and a brief parquet carries NO ``brief_date`` column at all, so
    the join key on that side can only come from the filename.
    """

    def __init__(self, root: Path):
        self.labels = root / LABELS_DIRNAME
        self.briefs = root / BRIEFS_DIRNAME
        self.labels.mkdir(parents=True)
        self.briefs.mkdir(parents=True)

    def write(
        self,
        brief_date: str,
        tickers: list[str],
        *,
        anchor: str | None = None,
        status: str = STATUS_OK,
        briefed: bool = True,
        version: str = VERSION_V3,
        outcomes: list[float | None] | None = None,
        stamped_brief_date: Any = None,
        write_brief: bool = True,
        brief_extra: dict[str, Any] | None = None,
    ) -> None:
        stamped = stamped_brief_date or dt.date.fromisoformat(brief_date)
        labels = pd.DataFrame(
            {
                "brief_date": [stamped] * len(tickers),
                "ticker": list(tickers),
                ANCHOR_COLUMN: anchor or brief_date,
                "briefed_any_theme": briefed,
                STATUS_COLUMN: status,
                VERSION_COLUMN: version,
                OUTCOME: outcomes if outcomes is not None else [0.0] * len(tickers),
            }
        )
        labels.to_parquet(self.labels / f"{brief_date}.parquet", index=False)
        if not write_brief:
            return
        briefs = pd.DataFrame(
            {
                "ticker": list(tickers),
                ATR: np.linspace(4.0, 9.0, len(tickers)),
                **(brief_extra or {}),
            }
        )
        briefs.to_parquet(self.briefs / f"{brief_date}.parquet", index=False)


class _PanelCase(unittest.TestCase):
    """Fixture plumbing shared by every case: one temporary two-store root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = _Store(Path(self._tmp.name))

    def spec(self, **overrides: Any) -> ep.PanelSpec:
        defaults: dict[str, Any] = {
            "labels_dir": self.store.labels,
            "briefs_dir": self.store.briefs,
            "side": ep.SIDE_HELD_OUT,
            "cutoff": CUTOFF,
            "population": ep.POPULATION_BRIEFED,
            "status_column": STATUS_COLUMN,
            "resolved_status": STATUS_OK,
            "anchor_column": ANCHOR_COLUMN,
            "arrival_as_str": True,
            "version_pins": {VERSION_COLUMN: VERSION_V3},
            "require_pinned": frozenset({VERSION_COLUMN}),
        }
        defaults.update(overrides)
        return ep.PanelSpec(**defaults)

    def with_outcome(self, spec: ep.PanelSpec, **overrides: Any) -> pd.DataFrame:
        kwargs: dict[str, Any] = {"outcome": OUTCOME, "brief_columns": [ATR]}
        kwargs.update(overrides)
        return ep.episode_panel_with_outcome(spec, **kwargs)


class TestTheJoinKeyType(_PanelCase):
    """The label store stamps a native date and the brief store has no date
    column at all, so the key on the brief side comes from the filename. Joining
    a ``dt.date`` against that string matches NOTHING and returns a frame that
    looks clean: the measured miss was 391 of 391 rows.
    """

    TICKERS = ["AAA", "BBB", "CCC"]

    def test_a_native_date_label_joins_a_filename_keyed_brief(self):
        self.store.write(FIRST_HELD_OUT_DATE, self.TICKERS)

        panel = self.with_outcome(self.spec())

        self.assertEqual(len(panel), len(self.TICKERS))
        self.assertEqual(sorted(panel["ticker"]), sorted(self.TICKERS))

    def test_the_label_store_really_stamps_a_native_date(self):
        # The refutation control. If the fixture wrote a string the case above
        # would pass against a loader that never stringifies anything.
        self.store.write(FIRST_HELD_OUT_DATE, self.TICKERS)
        written = pd.read_parquet(self.store.labels / f"{FIRST_HELD_OUT_DATE}.parquet")
        self.assertIsInstance(written["brief_date"].iloc[0], dt.date)

    def test_a_brief_parquet_carries_no_brief_date_column(self):
        # The other half of the control: the key cannot come from the brief rows.
        self.store.write(FIRST_HELD_OUT_DATE, self.TICKERS)
        written = pd.read_parquet(self.store.briefs / f"{FIRST_HELD_OUT_DATE}.parquet")
        self.assertNotIn("brief_date", written.columns)


class TestAnEmptyJoinIsABug(_PanelCase):
    """A join matching no rows is a key defect, not an absence of data. Returning
    an empty frame lets a caller report "not enough episodes yet" for a store
    that is full.
    """

    def test_two_sides_sharing_no_key_are_refused(self):
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA"], write_brief=True)
        # Rewrite the brief file under a ticker the label side does not carry.
        pd.DataFrame({"ticker": ["ZZZ"], ATR: [5.0]}).to_parquet(
            self.store.briefs / f"{FIRST_HELD_OUT_DATE}.parquet", index=False
        )

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec())
        self.assertIn("0 rows", str(caught.exception))

    def test_a_side_with_no_parquets_is_refused(self):
        self.store.write(BURNT_DATE, ["AAA"])

        with self.assertRaises(ep.PanelError):
            self.with_outcome(self.spec())


class TestTheArrivalIsTheStoredAnchor(_PanelCase):
    """Owner decision D4: the arrival is the anchor the label store stamped,
    never ``session_on_or_after(brief_date)``. The two disagreed on 296 of 466
    burnt rows, and a brief date that is itself a session is where the wrong one
    looks right.
    """

    def test_the_arrival_comes_from_the_anchor_column(self):
        self.store.write(SESSION_BRIEF_DATE, ["AAA"], anchor=SESSION_NEXT_DAY)

        panel = self.with_outcome(self.spec())

        self.assertEqual(list(panel[ARRIVAL]), [SESSION_NEXT_DAY])

    def test_the_anchor_column_is_renamed_away(self):
        self.store.write(SESSION_BRIEF_DATE, ["AAA"], anchor=SESSION_NEXT_DAY)

        panel = self.with_outcome(self.spec())

        self.assertNotIn(ANCHOR_COLUMN, panel.columns)


class TestFiltersRunBeforeTheCollapse(_PanelCase):
    """The chained collapse keeps the FIRST row of an episode, so filtering
    after it represents an episode by a row that was dropped and empties the
    panel. Both filters are checked, because an order bug fixed on one of them
    says nothing about the other.
    """

    TICKER = "AAA"

    def _two_chained_sessions(self, *, first: dict[str, Any], second: dict[str, Any]) -> list[str]:
        first_date, second_date = _sessions(2)
        self.store.write(first_date, [self.TICKER], **first)
        self.store.write(second_date, [self.TICKER], **second)
        return [first_date, second_date]

    def test_the_status_filter_runs_before_the_collapse(self):
        _, second_date = self._two_chained_sessions(
            first={"status": STATUS_IMMATURE}, second={"status": STATUS_OK}
        )

        panel = self.with_outcome(self.spec())

        self.assertEqual(len(panel), 1)
        self.assertEqual(list(panel["brief_date"]), [second_date])

    def test_the_population_filter_runs_before_the_collapse(self):
        _, second_date = self._two_chained_sessions(
            first={"briefed": False}, second={"briefed": True}
        )

        panel = self.with_outcome(self.spec())

        self.assertEqual(len(panel), 1)
        self.assertEqual(list(panel["brief_date"]), [second_date])

    def test_the_population_filter_is_switchable(self):
        # Positive control for the case above: with no population restriction
        # the unbriefed row is the one the collapse keeps.
        first_date, _ = self._two_chained_sessions(
            first={"briefed": False}, second={"briefed": True}
        )

        panel = self.with_outcome(self.spec(population=ep.POPULATION_ALL))

        self.assertEqual(len(panel), 1)
        self.assertEqual(list(panel["brief_date"]), [first_date])


class TestTwoLabelVersionsMustNotPool(_PanelCase):
    """Nothing in the four copies pinned ``sel_label_version``, so two label
    definitions could pool into one panel with nothing raised. A pin is only
    worth having if all four ways of getting it wrong are refused.
    """

    def _two_versions(self) -> list[str]:
        first_date, second_date = _sessions(2)
        self.store.write(first_date, ["AAA"], version=VERSION_V3)
        self.store.write(second_date, ["BBB"], version=VERSION_V2)
        return [first_date, second_date]

    def test_a_required_pin_left_unset_is_refused(self):
        self._two_versions()

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec(version_pins={}))
        self.assertIn(VERSION_COLUMN, str(caught.exception))

    def test_a_pinned_version_excludes_the_other_one(self):
        self._two_versions()

        panel = self.with_outcome(self.spec())

        self.assertEqual(sorted(panel["ticker"]), ["AAA"])

    def test_a_pin_matching_nothing_is_refused(self):
        self._two_versions()

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec(version_pins={VERSION_COLUMN: VERSION_ABSENT}))
        self.assertIn(VERSION_ABSENT, str(caught.exception))

    def test_a_pin_on_a_column_the_store_lacks_is_refused(self):
        self._two_versions()

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(
                self.spec(
                    version_pins={UNKNOWN_COLUMN: VERSION_V3},
                    require_pinned=frozenset({UNKNOWN_COLUMN}),
                )
            )
        self.assertIn(UNKNOWN_COLUMN, str(caught.exception))


class TestTheBlindPathCannotReturnAnOutcome(_PanelCase):
    """Three layers, because each one has a hole the others close: the signature
    carries no outcome argument, the reads come from a frozen allowlist, and the
    returned columns are asserted. The last layer is the one that catches a merge
    suffix and a brief store that one day gains an outcome column.
    """

    TICKERS = ["AAA", "BBB"]

    def setUp(self):
        super().setUp()
        self.store.write(FIRST_HELD_OUT_DATE, self.TICKERS)

    def _watch_reads(self) -> tuple[list[str], Any]:
        seen: list[str] = []
        real = pd.read_parquet

        def watched(path, columns=None, **kwargs):
            seen.extend(columns or [])
            return real(path, columns=columns, **kwargs)

        return seen, watched

    def test_the_blind_frame_carries_exactly_the_structure_columns(self):
        # An equality, not an assertNotIn: only equality catches an outcome
        # arriving under a name nobody thought to exclude, such as sel_ar_20_y.
        panel = ep.episode_panel_blind(self.spec())

        self.assertEqual(
            sorted(panel.columns),
            sorted(
                [
                    "brief_date",
                    "ticker",
                    ARRIVAL,
                    "briefed_any_theme",
                    STATUS_COLUMN,
                    VERSION_COLUMN,
                ]
            ),
        )

    def test_the_blind_path_reads_only_allowlisted_columns(self):
        spec = self.spec()
        seen, watched = self._watch_reads()

        with mock.patch.object(pd, "read_parquet", watched):
            ep.episode_panel_blind(spec)

        self.assertTrue(seen, "positive control: the watcher recorded nothing at all")
        self.assertLessEqual(set(seen), set(ep.blind_label_columns(spec)))

    def test_the_blind_path_takes_no_outcome_argument(self):
        # The structural layer. It fails against a loader whose blindness is one
        # keyword argument away from being lost.
        self.assertNotIn("outcome", inspect.signature(ep.episode_panel_blind).parameters)

    def test_an_outcome_column_in_the_brief_store_is_never_read(self):
        other = _sessions(2)[1]
        self.store.write(other, ["CCC"], brief_extra={OUTCOME: [0.5]})
        spec = self.spec()
        seen, watched = self._watch_reads()

        with mock.patch.object(pd, "read_parquet", watched):
            panel = ep.episode_panel_blind(spec)

        self.assertNotIn(OUTCOME, panel.columns)
        self.assertNotIn(OUTCOME, seen)


class TestNoOutcomeShapedFeatureColumn(_PanelCase):
    """A caller could otherwise reach a second held-out label through the feature
    list, which a denylist on the outcome argument alone would miss.
    """

    def test_an_outcome_shaped_brief_column_is_refused(self):
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA"])

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec(), brief_columns=[SECOND_OUTCOME])
        self.assertIn(SECOND_OUTCOME, str(caught.exception))

    def test_the_screen_runs_before_any_store_read(self):
        # Nothing is written at all: the refusal must come from the argument,
        # not from a column missing on disk.
        with self.assertRaises(ep.PanelError):
            self.with_outcome(self.spec(), brief_columns=[SECOND_OUTCOME])


class TestTheResultAssertion(_PanelCase):
    """The third blindness layer, tested directly because nothing in the stores
    can reach it today. An extracted guard with no test of its own is how six
    gaps entered one refactor; the guard is mutated, not the function that calls
    it.
    """

    SUFFIXED_OUTCOME = f"{OUTCOME}_y"

    def test_a_merge_suffixed_outcome_is_caught(self):
        frame = pd.DataFrame({"ticker": ["AAA"], self.SUFFIXED_OUTCOME: [0.4]})

        with self.assertRaises(ep.PanelError) as caught:
            ep._assert_blind(frame, outcome=None)
        self.assertIn(self.SUFFIXED_OUTCOME, str(caught.exception))

    def test_the_named_outcome_is_allowed_through(self):
        # Positive control: the assertion must not refuse the column the caller
        # asked for, or the outcome path could never return anything.
        frame = pd.DataFrame({"ticker": ["AAA"], OUTCOME: [0.4]})

        self.assertIsNone(ep._assert_blind(frame, outcome=OUTCOME))

    def test_the_blind_path_actually_calls_the_assertion(self):
        # The guard above is unreachable from the stores as they are shaped
        # today, so without this case deleting the CALL changes nothing and no
        # test notices. The allowlist is widened to simulate layer 1 failing,
        # which is the situation layer 3 exists for.
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA"])
        spec = self.spec()
        widened = (*ep.blind_label_columns(spec), OUTCOME)

        with mock.patch.object(ep, "blind_label_columns", return_value=widened):
            with self.assertRaises(ep.PanelError) as caught:
                ep.episode_panel_blind(spec)
        self.assertIn(OUTCOME, str(caught.exception))

    def test_a_suffixed_outcome_is_caught_even_beside_the_named_one(self):
        frame = pd.DataFrame({"ticker": ["AAA"], OUTCOME: [0.4], self.SUFFIXED_OUTCOME: [0.5]})

        with self.assertRaises(ep.PanelError):
            ep._assert_blind(frame, outcome=OUTCOME)


class TestAnOutcomeShapedSpecIsRefused(_PanelCase):
    """A caller could otherwise smuggle a label in as the status column, which
    the blind allowlist would then read and return as structure.
    """

    def test_an_outcome_shaped_status_column_is_refused(self):
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA"])

        with self.assertRaises(ep.PanelError) as caught:
            ep.episode_panel_blind(self.spec(status_column=OUTCOME))
        self.assertIn(OUTCOME, str(caught.exception))


class TestADuplicatedBriefKeyIsRefused(_PanelCase):
    """One brief row per (brief_date, ticker) is what makes the join m:1. A
    duplicate would multiply episodes, so it is refused as a store defect rather
    than surfacing as pandas' own MergeError.
    """

    def test_two_brief_rows_for_one_ticker_are_refused(self):
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA"])
        pd.DataFrame({"ticker": ["AAA", "AAA"], ATR: [5.0, 6.0]}).to_parquet(
            self.store.briefs / f"{FIRST_HELD_OUT_DATE}.parquet", index=False
        )

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec())
        self.assertIn("unique", str(caught.exception))


class TestAnUnusableAnchorIsRefused(_PanelCase):
    """``.str.slice`` hands a missing value back as a float NaN even after
    ``astype(str)``, and ``groupby`` drops a NaN key, so a bad anchor DELETES an
    episode instead of inflating the cluster count. The guard is checked against
    the panel, not the file.
    """

    def test_a_missing_anchor_on_a_kept_row_is_named(self):
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA", "BBB"])
        path = self.store.labels / f"{FIRST_HELD_OUT_DATE}.parquet"
        frame = pd.read_parquet(path)
        frame.loc[frame["ticker"] == "BBB", ANCHOR_COLUMN] = None
        frame.to_parquet(path, index=False)

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec())
        self.assertIn(ANCHOR_COLUMN, str(caught.exception))

    def test_a_missing_anchor_on_an_excluded_row_does_not_refuse(self):
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA", "BBB"])
        path = self.store.labels / f"{FIRST_HELD_OUT_DATE}.parquet"
        frame = pd.read_parquet(path)
        bad = frame["ticker"] == "BBB"
        frame.loc[bad, ANCHOR_COLUMN] = None
        frame.loc[bad, STATUS_COLUMN] = STATUS_IMMATURE
        frame.to_parquet(path, index=False)

        panel = self.with_outcome(self.spec())

        self.assertEqual(list(panel["ticker"]), ["AAA"])


class TestTheTwoCountsAgree(_PanelCase):
    """The blind count is what a preflight may quote, so it has to predict the
    outcome path's count and be an UPPER bound on it, never a lower one.
    """

    TICKERS = ["AAA", "BBB", "CCC"]

    def test_the_counts_match_on_a_clean_store(self):
        self.store.write(FIRST_HELD_OUT_DATE, self.TICKERS)

        counts = ep.episode_counts(self.spec())
        panel = self.with_outcome(self.spec())

        self.assertEqual(counts, (len(panel), panel[ARRIVAL].nunique()))

    def test_a_resolved_row_with_a_null_outcome_makes_the_blind_count_larger(self):
        self.store.write(FIRST_HELD_OUT_DATE, self.TICKERS, outcomes=[0.1, None, 0.3])

        episodes, _ = ep.episode_counts(self.spec())
        panel = self.with_outcome(self.spec())

        self.assertGreater(episodes, len(panel))


class TestTheUnitIsEpisodesNotRows(_PanelCase):
    """Ledger rule 5 counts ticker-episodes under the chained 5-session collapse.
    The numbers are the ones test_a20_atr_confirmation already measures.
    """

    def test_a_chained_repeat_is_not_a_second_episode(self):
        rng = np.random.default_rng(17)
        previous: str | None = None
        for index, session in enumerate(_sessions(CHAIN_SESSIONS)):
            names = [f"T{index:03d}X{j:03d}" for j in range(CHAIN_PER_SESSION)]
            if previous is not None:
                names[-1] = previous
            previous = names[0]
            self.store.write(session, names, outcomes=list(rng.normal(size=len(names))))

        episodes, clusters = ep.episode_counts(self.spec())

        self.assertEqual(clusters, CHAIN_SESSIONS)
        self.assertEqual(episodes, CHAIN_EPISODES)


class TestTheCutoffBoundary(_PanelCase):
    """The cutoff is the discovery freeze and is closed on the burnt side. An
    off-by-one here moves a frozen boundary with nothing failing.
    """

    def setUp(self):
        super().setUp()
        self.store.write(CUTOFF_EXCLUSIVE_DATE, ["ONCUTOFF"])
        self.store.write(FIRST_HELD_OUT_DATE, ["AFTER"])

    def test_the_held_out_side_excludes_the_cutoff_date(self):
        panel = self.with_outcome(self.spec(side=ep.SIDE_HELD_OUT))

        self.assertEqual(list(panel["ticker"]), ["AFTER"])

    def test_the_burnt_side_includes_the_cutoff_date(self):
        panel = self.with_outcome(self.spec(side=ep.SIDE_BURNT))

        self.assertEqual(list(panel["ticker"]), ["ONCUTOFF"])


class TestTheStemAndTheDateColumnAgree(_PanelCase):
    """The loader filters on the parquet STEM while two call sites filter on the
    ``brief_date`` COLUMN. Those are the same filter only if one file holds one
    brief date, so the loader asserts it rather than assuming it.
    """

    def test_a_file_whose_rows_disagree_with_its_name_is_refused(self):
        self.store.write(
            FIRST_HELD_OUT_DATE,
            ["AAA"],
            stamped_brief_date=dt.date.fromisoformat(BURNT_DATE),
        )

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec())
        self.assertIn(BURNT_DATE, str(caught.exception))


class TestTheLoaderOwnsNoFrozenConstant(unittest.TestCase):
    """Every frozen choice stays the caller's. A default here would leave two
    constants to keep frozen instead of one.
    """

    CALLER_OWNED = (
        "cutoff",
        "population",
        "status_column",
        "resolved_status",
        "arrival_as_str",
    )

    def test_no_caller_owned_field_has_a_default(self):
        import dataclasses

        defaults = {
            field.name
            for field in fields(ep.PanelSpec)
            if field.default is not dataclasses.MISSING
            or field.default_factory is not dataclasses.MISSING
        }

        self.assertEqual(defaults & set(self.CALLER_OWNED), set())


if __name__ == "__main__":
    unittest.main()
