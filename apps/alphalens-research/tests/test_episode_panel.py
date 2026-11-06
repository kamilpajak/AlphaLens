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

# A real column of ``SEL_LABEL_SCHEMA`` that is not a version: pinning on it is
# how a caller would otherwise widen the blind allowlist onto the label store.
NON_VERSION_LABEL_COLUMN = "published_before_open"

CUTOFF = "2026-07-05"
BURNT_DATE = "2026-07-03"
CUTOFF_EXCLUSIVE_DATE = "2026-07-05"
FIRST_HELD_OUT_DATE = "2026-07-06"

# Three cutoffs that are not the ISO text the stem comparison needs. The first
# is the dangerous one: it sorts BELOW every real stem, so on the burnt side it
# pulls the held-out files in.
NON_ISO_CUTOFFS = ("2026-7-5", "2026-07", "26-07-05")

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
        anchor_is_date: bool = False,
    ) -> None:
        stamped = stamped_brief_date or dt.date.fromisoformat(brief_date)
        # The real store stamps the anchor as a date32, which pandas hands back
        # as a native ``dt.date``. Writing it as text by default keeps the older
        # cases readable; the dtype cases need the real shape, because that is
        # the only shape on which ``arrival_as_str`` changes anything.
        stamped_anchor: Any = anchor or brief_date
        if anchor_is_date:
            stamped_anchor = dt.date.fromisoformat(str(stamped_anchor))
        labels = pd.DataFrame(
            {
                "brief_date": [stamped] * len(tickers),
                "ticker": list(tickers),
                ANCHOR_COLUMN: stamped_anchor,
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
    panel. All FOUR row filters are checked, because an order bug fixed on one
    of them says nothing about the other three: measured on this branch, moving
    the version pin or the outcome dropna after the collapse left the whole file
    green while the panel silently changed.
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

    def test_the_version_pin_runs_before_the_collapse(self):
        _, second_date = self._two_chained_sessions(
            first={"version": VERSION_V2}, second={"version": VERSION_V3}
        )

        panel = self.with_outcome(self.spec())

        self.assertEqual(len(panel), 1)
        self.assertEqual(list(panel["brief_date"]), [second_date])

    def test_the_outcome_dropna_runs_before_the_collapse(self):
        _, second_date = self._two_chained_sessions(
            first={"outcomes": [None]}, second={"outcomes": [0.4]}
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
    worth having if every way of getting it wrong is refused, and the cases
    below name the layer that refuses each one, because they are not all the
    same layer: a missing pin is refused by the spec check, a pin matching
    nothing by the pin helper, and a pin naming a column the store lacks by the
    reader, since a pin key goes into the read list.
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

    def test_a_pin_on_a_column_the_store_lacks_is_refused_by_the_reader(self):
        # Named for the layer that actually refuses it. A pin key goes into the
        # read list, so the file is what reports it missing, and the message
        # says which file - which is the useful half. Asserting the column name
        # alone would also pass against the pin helper's own branch, and that
        # branch is unreachable through the public API.
        first_date, _ = self._two_versions()

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(
                self.spec(
                    version_pins={UNKNOWN_COLUMN: VERSION_V3},
                    require_pinned=frozenset({UNKNOWN_COLUMN}),
                )
            )
        self.assertIn(UNKNOWN_COLUMN, str(caught.exception))
        self.assertIn(f"{first_date}.parquet", str(caught.exception))

    def test_the_pin_helper_refuses_a_column_its_frame_lacks(self):
        # The pin helper's own branch, mutated directly: nothing in the stores
        # can reach it, so without this case deleting it changes nothing.
        frame = pd.DataFrame({"ticker": ["AAA"], VERSION_COLUMN: [VERSION_V3]})

        with self.assertRaises(ep.PanelError) as caught:
            ep._apply_pins(frame, {UNKNOWN_COLUMN: VERSION_V3})
        self.assertIn(UNKNOWN_COLUMN, str(caught.exception))


class TestTheBlindPathCannotReturnAnOutcome(_PanelCase):
    """Three layers: the signature carries no outcome argument, the reads come
    from an allowlist, and the returned columns are asserted. Layer 1 is what
    closes a brief store that one day gains an outcome column, because the blind
    path reads the brief side for the join key alone; the last layer catches a
    merge suffix and a future edit to the first two.
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


class TestTheByNameScreenOpensNoFile(_PanelCase):
    """Blindness layer 2, pinned where it differs from layer 3.

    The case above passes against a loader with no screen at all, because the
    column it names is missing from disk. This one writes a store that really
    CARRIES the second label, so without the screen the build runs: measured on
    this branch, removing the screen opened 2 files and read the held-out column
    into memory before layer 3 refused. Two assertions per role, therefore —
    the message names the ROLE, and no file was opened.
    """

    def setUp(self):
        super().setUp()
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA"], brief_extra={SECOND_OUTCOME: [0.5]})

    def _refused_with_nothing_opened(self, role: str, build: Any) -> None:
        opened: list[str] = []
        real_read = pd.read_parquet
        real_schema = ep._schema_names

        def watched_read(path, **kwargs):
            opened.append(str(path))
            return real_read(path, **kwargs)

        def watched_schema(path):
            opened.append(str(path))
            return real_schema(path)

        with (
            mock.patch.object(pd, "read_parquet", watched_read),
            mock.patch.object(ep, "_schema_names", watched_schema),
            self.assertRaises(ep.PanelError) as caught,
        ):
            build()

        message = str(caught.exception)
        self.assertIn(role, message)
        self.assertIn(SECOND_OUTCOME, message)
        # "outcome-shaped" is asserted, not just any refusal: two of these four
        # arguments are also refused for a second reason, and a case that
        # accepts either message stops pinning the blindness screen.
        self.assertIn("outcome-shaped", message)
        self.assertEqual(opened, [])

    def test_a_brief_feature_naming_a_label_is_refused_by_name(self):
        self._refused_with_nothing_opened(
            "brief_columns",
            lambda: self.with_outcome(self.spec(), brief_columns=[SECOND_OUTCOME]),
        )

    def test_a_version_pin_naming_a_label_is_refused_by_name(self):
        self._refused_with_nothing_opened(
            "version_pins",
            lambda: ep.episode_panel_blind(
                self.spec(
                    version_pins={SECOND_OUTCOME: VERSION_V3},
                    require_pinned=frozenset({SECOND_OUTCOME}),
                )
            ),
        )

    def test_an_anchor_column_naming_a_label_is_refused_by_name(self):
        self._refused_with_nothing_opened(
            "the spec",
            lambda: ep.episode_panel_blind(self.spec(anchor_column=SECOND_OUTCOME)),
        )

    def test_a_status_column_naming_a_label_is_refused_by_name(self):
        self._refused_with_nothing_opened(
            "the spec",
            lambda: ep.episode_panel_blind(self.spec(status_column=SECOND_OUTCOME)),
        )

    def test_the_fixture_really_carries_the_second_label(self):
        # The control. Without this the four cases above could pass against a
        # store the screen never had to protect.
        written = pd.read_parquet(self.store.briefs / f"{FIRST_HELD_OUT_DATE}.parquet")
        self.assertIn(SECOND_OUTCOME, written.columns)


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


class TestEveryNamedBriefColumnMustExist(_PanelCase):
    """A brief feature is required in every file on this side, and a name no
    file carries is refused rather than returned as a null column.

    That is the whole reason this loader offers no optional brief read: a typo
    would otherwise come back as a column silently absent from the frame, which
    is the case that matters, and the promise "null where the file lacks it"
    does not cover it.
    """

    MISSPELLED_FEATURE = "technical_atr_pc"

    def test_a_brief_feature_no_file_carries_is_refused(self):
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA"])

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec(), brief_columns=[self.MISSPELLED_FEATURE])
        self.assertIn(self.MISSPELLED_FEATURE, str(caught.exception))

    def test_a_brief_feature_missing_from_one_file_of_two_is_refused(self):
        # The partial case, which is the one an optional read would have been
        # for. It is refused too, so the panel never pools a feature that only
        # half the history carries.
        first_date, second_date = _sessions(2)
        self.store.write(first_date, ["AAA"], brief_extra={"new_feature": [7.0]})
        self.store.write(second_date, ["BBB"])

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec(), brief_columns=["new_feature"])
        self.assertIn("new_feature", str(caught.exception))
        self.assertIn(f"{second_date}.parquet", str(caught.exception))


class TestAnUnusableAnchorIsRefused(_PanelCase):
    """``.str.slice`` hands a missing value back as a float NaN even after
    ``astype(str)``, and ``nunique()`` does not count a NaN, so a bad anchor
    under-counts the arrival clusters while leaving the episode count untouched.
    The guard is checked against the rows the panel KEEPS, not against the file,
    which is why it runs after the collapse.
    """

    TICKER = "AAA"

    def test_a_missing_anchor_on_a_kept_row_is_named(self):
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA", "BBB"])
        path = self.store.labels / f"{FIRST_HELD_OUT_DATE}.parquet"
        frame = pd.read_parquet(path)
        frame.loc[frame["ticker"] == "BBB", ANCHOR_COLUMN] = None
        frame.to_parquet(path, index=False)

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec())
        self.assertIn(ANCHOR_COLUMN, str(caught.exception))

    def test_a_null_anchor_beside_a_non_iso_one_is_still_refused(self):
        # Two KINDS of unusable value at once. The sample the message quotes is
        # built from a set, and after ``astype(str)`` a missing value comes back
        # as a float NaN, so sorting the raw values compares a float to a str
        # and raises TypeError instead of the message meant for the caller.
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA", "BBB", "CCC"])
        path = self.store.labels / f"{FIRST_HELD_OUT_DATE}.parquet"
        frame = pd.read_parquet(path)
        frame.loc[frame["ticker"] == "BBB", ANCHOR_COLUMN] = None
        frame.loc[frame["ticker"] == "CCC", ANCHOR_COLUMN] = "not-a-date"
        frame.to_parquet(path, index=False)

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec())
        self.assertIn(ANCHOR_COLUMN, str(caught.exception))
        self.assertIn("not-a-date", str(caught.exception))

    def test_a_bad_anchor_on_a_row_the_collapse_drops_does_not_refuse(self):
        # The guard's scope is the rows the panel KEEPS, which is what its
        # docstring says. A chained repeat is dropped by the collapse, so a bad
        # anchor on it must not refuse the whole panel - and the message would
        # say the value is "in the panel" when it is not.
        first_date, second_date = _sessions(2)
        self.store.write(first_date, [self.TICKER])
        self.store.write(second_date, [self.TICKER], anchor="not-a-date")

        panel = self.with_outcome(self.spec())

        self.assertEqual(list(panel["brief_date"]), [first_date])

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
    """The blind count is what a preflight may quote, so on a clean store it has
    to be the count the outcome path returns.
    """

    TICKERS = ["AAA", "BBB", "CCC"]
    #: Three tickers, one brief date, one anchor: three episodes, one cluster.
    CLEAN_COUNTS = (3, 1)

    def test_the_counts_match_on_a_clean_store(self):
        self.store.write(FIRST_HELD_OUT_DATE, self.TICKERS)

        counts = ep.episode_counts(self.spec())
        panel = self.with_outcome(self.spec())

        # The expectation is written down rather than computed by the loader on
        # both sides of the equality: otherwise a defect shared by the two paths
        # keeps the test green.
        self.assertEqual(counts, self.CLEAN_COUNTS)
        self.assertEqual(counts, (len(panel), panel[ARRIVAL].nunique()))

    def test_a_resolved_row_with_a_null_outcome_makes_the_blind_count_larger(self):
        self.store.write(FIRST_HELD_OUT_DATE, self.TICKERS, outcomes=[0.1, None, 0.3])

        episodes, _ = ep.episode_counts(self.spec())
        panel = self.with_outcome(self.spec())

        self.assertGreater(episodes, len(panel))


class TestTheBlindCountIsNotAnUpperBound(_PanelCase):
    """Dropping a null-outcome row can UN-CHAIN an episode, so the outcome path
    can return MORE episodes and MORE clusters than the blind preflight.

    ``episode_counts`` documented itself as an upper bound on both counts. It is
    one on resolved ROWS only. The row the outcome path drops is the row that
    held two appearances inside one chain window, and removing it leaves the two
    ends far enough apart to be two episodes.
    """

    TICKER = "AAA"
    #: Sessions 0, 5 and 10 of the same ticker: 0 chains to 5 and 5 chains to
    #: 10 at the 5-session window, but 0 and 10 do not chain to each other.
    SPACED_INDEXES = (0, 5, 10)
    SPACED_OUTCOMES = (0.1, None, 0.3)

    def test_dropping_the_middle_row_splits_one_episode_into_two(self):
        sessions = _sessions(max(self.SPACED_INDEXES) + 1)
        for index, outcome in zip(self.SPACED_INDEXES, self.SPACED_OUTCOMES, strict=True):
            self.store.write(sessions[index], [self.TICKER], outcomes=[outcome])

        blind = ep.episode_counts(self.spec())
        panel = self.with_outcome(self.spec())

        self.assertEqual(blind, (1, 1))
        self.assertEqual((len(panel), panel[ARRIVAL].nunique()), (2, 2))


class TestTheUnitIsEpisodesNotRows(_PanelCase):
    """Ledger rule 5 counts ticker-episodes under the chained 5-session collapse.
    The numbers are the ones test_a20_atr_confirmation already measures.

    The unit is pinned from both sides. A near repeat must be ONE episode and a
    distant repeat must be TWO: a de-duplication keyed on the ticker alone
    satisfies the first and fails the second, and that is the mutation that
    moves the episode count against a frozen floor with nothing failing.
    """

    FAR_TICKER = "FAR"
    #: Twenty-one sessions apart is four chain windows: the two appearances
    #: cannot be one episode under any window the ledger would accept.
    FAR_SESSION_COUNT = 21
    FAR_COUNTS = (2, 2)

    def test_a_repeat_outside_the_chain_window_is_a_second_episode(self):
        sessions = _sessions(self.FAR_SESSION_COUNT)
        for session in (sessions[0], sessions[-1]):
            self.store.write(session, [self.FAR_TICKER])

        self.assertEqual(ep.episode_counts(self.spec()), self.FAR_COUNTS)

    def test_two_identical_rows_in_one_file_are_one_episode(self):
        # Same (brief_date, ticker) twice. The chained collapse sees distance
        # zero, so it keeps the first row and the duplicate never doubles an
        # episode. The brief side is left unique, or the m:1 join would refuse
        # the store before this is reached.
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA"])
        path = self.store.labels / f"{FIRST_HELD_OUT_DATE}.parquet"
        frame = pd.read_parquet(path)
        pd.concat([frame, frame], ignore_index=True).to_parquet(path, index=False)

        panel = self.with_outcome(self.spec())

        self.assertEqual(len(panel), 1)

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


class TestNoSpecFieldHasADefault(unittest.TestCase):
    """Every field of the spec stays the caller's.

    The reason differs by field - the cutoff, the population and the status
    column are pre-registered choices a default would duplicate, the rest are
    the caller's for symmetry - so the rule is the same for all eleven and the
    assertion is over EVERY field rather than over a hand-written list of
    the interesting ones. A list has to be maintained beside the dataclass, and
    the one that was here covered 5 of the 11 fields - so a default on
    ``require_pinned``, which turns "two label definitions cannot pool" into
    opt-in, was invisible.
    """

    def test_no_field_of_the_spec_has_a_default(self):
        import dataclasses

        defaults = sorted(
            field.name
            for field in fields(ep.PanelSpec)
            if field.default is not dataclasses.MISSING
            or field.default_factory is not dataclasses.MISSING
        )

        self.assertEqual(defaults, [])


class TestTheScreenCoversEveryStampedLabel(unittest.TestCase):
    """The screen is a name shape, so it goes stale the moment the stamper adds a
    label family. It already had: ``sel_zar_h`` and ``sel_car_mean_20`` are
    written on every row of the production store and matched nothing, so the
    blind path handed a held-out ``sel_zar_20`` value back without raising.

    This pins the contract instead of the prefixes: every value column the
    stamper writes must be refused by name. A new family then fails here rather
    than silently leaking through the result assertion.
    """

    #: ``car_20_event`` and ``car_40_event``; the third member is the version.
    EVENT_CAR_VALUE_COUNT = 2
    EVENT_CAR_VERSION_COLUMN = "event_car_version"

    def test_every_value_key_the_stamper_writes_is_outcome_shaped(self):
        from alphalens_pipeline.feedback.selection_label import VALUE_KEYS

        missed = [key for key in VALUE_KEYS if not ep._OUTCOME_SHAPED.match(key)]
        self.assertEqual([], missed)

    def test_every_event_window_value_column_is_outcome_shaped(self):
        # The second family the screen's ``car_`` prefix is there for. Neither
        # store this loader reads holds it today, which is why it needs its own
        # case: deleting the prefix left the rest of the file green.
        from alphalens_pipeline.feedback.event_car import EVENT_CAR_COLUMNS

        values = [column for column in EVENT_CAR_COLUMNS if not column.endswith("_version")]
        self.assertEqual(len(values), self.EVENT_CAR_VALUE_COUNT)
        for column in values:
            self.assertIsNotNone(ep._OUTCOME_SHAPED.match(column), column)

    def test_the_event_window_version_column_is_not_outcome_shaped(self):
        # The negative control for the case above, and a real one: this column
        # sits in the same tuple, and a screen written as ``car`` rather than
        # ``car_`` would refuse it and block a legitimate structural read.
        self.assertIsNone(ep._OUTCOME_SHAPED.match(self.EVENT_CAR_VERSION_COLUMN))

    def test_a_structural_column_is_not_outcome_shaped(self):
        # The positive control: a screen that matched everything would pass the
        # test above and refuse every legitimate blind read.
        for column in ep.BLIND_STRUCTURE_COLUMNS:
            self.assertIsNone(ep._OUTCOME_SHAPED.match(column), column)


class TestTheSpecGuardsThatNeedNoFile(_PanelCase):
    """The side and the population are pre-registered choices, so a value that
    is neither of the two allowed tokens is refused rather than treated as one.

    The side case uses a store with BOTH sides populated on purpose. With only
    the held-out files written, an unknown side still raises - but from the
    "no parquets" check, so the case passes against a loader with no side guard
    at all and a pre-registered side choice silently flipped to the burnt side.
    """

    def test_an_unknown_population_is_refused(self):
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA"])

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec(population="everything"))
        self.assertIn("everything", str(caught.exception))

    def test_an_unknown_side_is_refused_with_both_sides_populated(self):
        self.store.write(BURNT_DATE, ["BURNTA"])
        self.store.write(FIRST_HELD_OUT_DATE, ["HELDOUTA"])

        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec(side="heldout"))
        self.assertIn("heldout", str(caught.exception))

    def test_the_two_sides_are_really_both_populated(self):
        # The control for the case above: if either side were empty the "no
        # parquets" check would raise and the side guard would go untested.
        self.store.write(BURNT_DATE, ["BURNTA"])
        self.store.write(FIRST_HELD_OUT_DATE, ["HELDOUTA"])

        self.assertEqual(
            list(self.with_outcome(self.spec(side=ep.SIDE_BURNT))["ticker"]), ["BURNTA"]
        )
        self.assertEqual(
            list(self.with_outcome(self.spec(side=ep.SIDE_HELD_OUT))["ticker"]), ["HELDOUTA"]
        )


class TestANullBriefedFlagIsNotBriefed(_PanelCase):
    """The store writes ``briefed_any_theme`` nullable, so a row the stamper
    never resolved arrives as null rather than False. Treating null as briefed
    would quietly widen the confirmation population - the same rule the sibling
    preflight suite pins on its own population mask.
    """

    TICKERS = ["AAA", "BBB", "CCC"]
    FLAGS = [None, False, True]
    BRIEFED_TICKER = "CCC"

    def test_a_null_flag_is_excluded_from_the_briefed_population(self):
        self.store.write(FIRST_HELD_OUT_DATE, self.TICKERS)
        path = self.store.labels / f"{FIRST_HELD_OUT_DATE}.parquet"
        frame = pd.read_parquet(path)
        frame["briefed_any_theme"] = pd.array(self.FLAGS, dtype="boolean")
        frame.to_parquet(path, index=False)

        panel = self.with_outcome(self.spec())

        self.assertEqual(list(panel["ticker"]), [self.BRIEFED_TICKER])


class TestNothingMaturedIsNotADefect(_PanelCase):
    """A filter that keeps no row is an absence of data; a pin that matches no
    row is a store defect. Keeping those two apart is what ``episode_counts``
    publishes, so the pin is applied BEFORE the status and population filters:
    afterwards it sees an empty panel and blames the label version for it.
    """

    TICKERS = ["AAA", "BBB"]

    def test_a_store_where_nothing_has_matured_counts_zero(self):
        self.store.write(FIRST_HELD_OUT_DATE, self.TICKERS, status=STATUS_IMMATURE)

        self.assertEqual(ep.episode_counts(self.spec()), (0, 0))

    def test_a_store_where_nothing_was_briefed_counts_zero(self):
        self.store.write(FIRST_HELD_OUT_DATE, self.TICKERS, briefed=False)

        self.assertEqual(ep.episode_counts(self.spec()), (0, 0))

    def test_a_version_the_store_never_held_still_raises(self):
        # The other half: the pin moving earlier must not turn a store defect
        # into a quiet zero.
        self.store.write(FIRST_HELD_OUT_DATE, self.TICKERS)

        with self.assertRaises(ep.PanelError) as caught:
            ep.episode_counts(self.spec(version_pins={VERSION_COLUMN: VERSION_ABSENT}))
        self.assertIn(VERSION_ABSENT, str(caught.exception))


class TestTheCutoffIsISOText(_PanelCase):
    """The cutoff is compared against the parquet STEM as text. A cutoff that is
    not ISO text sorts somewhere else entirely: ``2026-7-5`` sorts BELOW every
    real stem, so the burnt side silently takes the held-out files in, and a
    ``dt.date`` raises a bare TypeError that never names the field.
    """

    def setUp(self):
        super().setUp()
        self.store.write(BURNT_DATE, ["BURNTA"])
        self.store.write(FIRST_HELD_OUT_DATE, ["HELDOUTA"])

    def _refused(self, cutoff: Any) -> str:
        with self.assertRaises(ep.PanelError) as caught:
            self.with_outcome(self.spec(side=ep.SIDE_BURNT, cutoff=cutoff))
        return str(caught.exception)

    def test_a_non_iso_cutoff_is_refused(self):
        for cutoff in NON_ISO_CUTOFFS:
            with self.subTest(cutoff=cutoff):
                self.assertIn("cutoff", self._refused(cutoff))

    def test_a_date_object_cutoff_is_refused(self):
        self.assertIn("cutoff", self._refused(dt.date.fromisoformat(CUTOFF)))

    def test_the_iso_cutoff_still_splits_the_two_sides(self):
        # The positive control: the guard must not refuse the cutoff every
        # caller actually passes.
        panel = self.with_outcome(self.spec(side=ep.SIDE_BURNT))

        self.assertEqual(list(panel["ticker"]), ["BURNTA"])


class TestTheTwoStoresAreDifferentDirectories(_PanelCase):
    """Pointing ``briefs_dir`` at the label store returns a LARGER panel with
    nothing raised: the brief side is read for the ticker alone, and the label
    store carries every ticker, so the join stops restricting the panel to the
    names the brief carried. The reverse swap does raise, so only one direction
    is silent.
    """

    #: The brief file holds the first ticker only, so a panel carrying all
    #: three is a panel the brief side has stopped restricting.
    LABEL_TICKERS = ["AAA", "BBB", "CCC"]
    BRIEF_TICKERS = ["AAA"]

    def setUp(self):
        super().setUp()
        self.store.write(FIRST_HELD_OUT_DATE, self.LABEL_TICKERS)
        pd.DataFrame({"ticker": self.BRIEF_TICKERS, ATR: [5.0]}).to_parquet(
            self.store.briefs / f"{FIRST_HELD_OUT_DATE}.parquet", index=False
        )

    def test_one_directory_for_both_stores_is_refused(self):
        # The blind path, because that is the silent case: it reads the brief
        # side for the ticker alone, and the label store carries that column,
        # so nothing is missing and nothing raises.
        with self.assertRaises(ep.PanelError) as caught:
            ep.episode_panel_blind(self.spec(briefs_dir=self.store.labels))
        self.assertIn("same directory", str(caught.exception))

    def test_the_brief_side_is_what_restricts_the_panel(self):
        # The control, and the measurement that makes the case above worth
        # having: pointed at the real brief store the panel is one row, and
        # pointed at the label store it was three.
        panel = ep.episode_panel_blind(self.spec())

        self.assertEqual(list(panel["ticker"]), self.BRIEF_TICKERS)


class TestAPinKeyIsAVersionColumn(_PanelCase):
    """``version_pins`` keys are appended to the blind allowlist, so they are the
    one caller-extensible part of it. Requiring each key to be a version column
    is what keeps layer 1 an allowlist: without it a caller can name any
    label-store column, have the blind path read it, and filter the panel on it.

    It also stops three crashes from inside pandas. A key the allowlist already
    requests duplicates a column in the read list, and the build then died with
    a message naming neither the pin nor the column.
    """

    def setUp(self):
        super().setUp()
        self.store.write(FIRST_HELD_OUT_DATE, ["AAA"])

    def _refused(self, **overrides: Any) -> str:
        with self.assertRaises(ep.PanelError) as caught:
            ep.episode_panel_blind(self.spec(**overrides))
        return str(caught.exception)

    def test_a_pin_key_that_is_not_a_version_column_is_refused(self):
        # The expected SHAPE is asserted, not just that something was refused.
        # Each of these also produces a refusal without the guard - a column the
        # store lacks, or a pin that matches no row - and those messages name
        # the column too, so a case asserting the name alone passes against no
        # guard at all.
        for column in (
            NON_VERSION_LABEL_COLUMN,
            *ep.BLIND_STRUCTURE_COLUMNS,
            ANCHOR_COLUMN,
            STATUS_COLUMN,
        ):
            with self.subTest(column=column):
                message = self._refused(version_pins={VERSION_COLUMN: VERSION_V3, column: "x"})
                self.assertIn(column, message)
                self.assertIn("*_version", message)

    def test_a_version_shaped_pin_on_the_status_column_names_the_alternative(self):
        # The one collision the version shape cannot catch: a status column that
        # is itself version-shaped. The message has to say what to use instead,
        # because pinning the status is a natural misreading of the field.
        message = self._refused(
            status_column=VERSION_COLUMN, version_pins={VERSION_COLUMN: VERSION_V3}
        )

        self.assertIn("structure", message)
        self.assertIn("resolved_status", message)

    def test_the_allowlist_never_repeats_a_column(self):
        # ``blind_label_columns`` is public and validates nothing itself, so the
        # read list it returns has to hold each column once on its own.
        spec = self.spec(status_column=VERSION_COLUMN, version_pins={VERSION_COLUMN: VERSION_V3})
        columns = ep.blind_label_columns(spec)

        self.assertEqual(len(columns), len(set(columns)))

    def test_the_pin_the_callers_actually_use_is_accepted(self):
        # The positive control: the guard must not refuse the one pin the whole
        # design exists for.
        panel = ep.episode_panel_blind(self.spec())

        self.assertEqual(list(panel["ticker"]), ["AAA"])


class TestTheArrivalDtypeIsTheCallersChoice(_PanelCase):
    """``arrival_as_str`` decides what the caller gets back, and the two branches
    are not interchangeable: the a20 family clusters on the ten-character text,
    while a caller handing the arrival to a calendar helper needs the stored
    date. The fixture writes the anchor as a date32, the way the store does,
    because against a text anchor both branches return the same thing and the
    flag is dead to the suite.
    """

    def setUp(self):
        super().setUp()
        self.store.write(SESSION_BRIEF_DATE, ["AAA"], anchor=SESSION_NEXT_DAY, anchor_is_date=True)

    def test_true_returns_the_sliced_iso_text(self):
        panel = self.with_outcome(self.spec(arrival_as_str=True))

        value = panel[ARRIVAL].iloc[0]
        self.assertIsInstance(value, str)
        self.assertEqual(value, SESSION_NEXT_DAY)

    def test_false_returns_the_stored_date_verbatim(self):
        panel = self.with_outcome(self.spec(arrival_as_str=False))

        value = panel[ARRIVAL].iloc[0]
        self.assertNotIsInstance(value, str)
        self.assertEqual(value, dt.date.fromisoformat(SESSION_NEXT_DAY))

    def test_the_fixture_really_stores_a_date(self):
        # The control. Against a text anchor the case above passes for the
        # wrong reason, because ``astype(str)`` of a string is that string.
        written = pd.read_parquet(self.store.labels / f"{SESSION_BRIEF_DATE}.parquet")
        self.assertIsInstance(written[ANCHOR_COLUMN].iloc[0], dt.date)


if __name__ == "__main__":
    unittest.main()
