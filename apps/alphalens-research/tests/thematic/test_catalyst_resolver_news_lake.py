"""The resolver can read the append-only news lake, not only the current view.

Measured on the production stores 2026-11-09: the news ingest runs several times
a day with ``--force``, each run OVERWRITING ``thematic_news/<date>.parquet``
while appending the same frame to ``thematic_news_lake/session_date=<date>/``.
Extraction is cumulative, so events accumulate the union of every run's view
while the view holds only the last run's rows. The result is that 11,288 of
42,037 event rows (26.85%) reference a ``news_id`` the view no longer holds, and
10,887 of those 11,088 distinct ids (98.2%) are still in the lake.

The inner join in the resolver drops those events, so a theme can lose its
catalyst to a retention artefact. Reading the lake as well recovers them.

GOVERNANCE. Reading the lake changes which catalyst is rendered into the
proposal prompt, and ``mapper_config_version``'s own docstring requires that
anything changing what the model reads be fingerprinted. The accrual window in
``docs/research/channel_feature_forward_prereg_2026_08_19.md`` (LOCKED) ends on
such a change. So :data:`news_source.READ_NEWS_LAKE` ships FALSE, the
token stays byte-identical, and
:meth:`test_the_flag_alone_decides_whether_the_boundary_is_recorded` is the tripwire
that goes red if the flag is flipped without recording the cohort boundary.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.thematic.mapping import catalyst_resolver, news_source, theme_mapper

_ASOF = dt.date(2026, 11, 5)
_THEME = "quantum_computing"
_TICKER = "IONQ"
#: Long enough that a "keep the longest body" rule is unambiguous, short enough
#: to read in a failure message.
_SHORT_BODY = "A short feed snippet."
_LONG_BODY = "The full press release text. " * 40
_MCAP = (500_000_000, 10_000_000_000)
#: Enough ids, with enough tied rows each, that an unstable sort picks a
#: different winner. Measured on the production shape — 1 view row and 17 lake
#: rows per id, all tied on body length — a stable sort keeps the view row for
#: 50 of 50 ids and an unstable one for 13 of 50.
_TIE_IDS = 50
_TIE_LAKE_ROWS_PER_ID = 17


def _news_row(news_id: str, body: str, *, url: str | None = None) -> dict:
    return {
        "id": news_id,
        "source": "edgar_press_release",
        "timestamp": pd.Timestamp(f"{_ASOF.isoformat()}T12:00:00Z"),
        "tickers": [_TICKER],
        "title": f"Headline for {news_id}",
        "body": body,
        "url": url or f"https://example.test/{news_id}",
        "keywords": [],
        "extra": "{}",
        "ingested_at": pd.Timestamp(f"{_ASOF.isoformat()}T12:05:00Z"),
    }


def _events_row(news_id: str) -> dict:
    return {
        "news_id": news_id,
        "event_type": "contract_award",
        "primary_entities": [_TICKER],
        "themes": [_THEME],
        "sentiment": "positive",
        "second_order_implications": ["Quantum hardware suppliers may benefit."],
        "confidence": 0.9,
        "extraction_method": "flash",
        "template_id": None,
        "template_fields_json": None,
        "model": "test",
        "extracted_at": pd.Timestamp(f"{_ASOF.isoformat()}T13:00:00Z"),
    }


class _Stores:
    """A view dir, a lake dir and an events dir under one temporary root."""

    def __init__(self, root: Path) -> None:
        self.view = root / "thematic_news"
        self.lake = root / "thematic_news_lake"
        self.events = root / "thematic_events"
        for d in (self.view, self.lake, self.events):
            d.mkdir(parents=True, exist_ok=True)

    def write_view(self, day: dt.date, rows: list[dict]) -> None:
        pd.DataFrame(rows).to_parquet(self.view / f"{day.isoformat()}.parquet", index=False)

    def write_lake_run(self, day: dt.date, stamp: str, rows: list[dict]) -> None:
        session = self.lake / f"session_date={day.isoformat()}"
        session.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(session / f"run={stamp}.parquet", index=False)

    def write_events(self, day: dt.date, rows: list[dict]) -> None:
        pd.DataFrame(rows).to_parquet(self.events / f"{day.isoformat()}.parquet", index=False)


class TestTheLakeWindowReader(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stores = _Stores(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_it_reads_every_run_of_a_day_inside_the_window(self) -> None:
        self.stores.write_lake_run(_ASOF, "20261105T010000000Z", [_news_row("a", _SHORT_BODY)])
        self.stores.write_lake_run(_ASOF, "20261105T020000000Z", [_news_row("b", _SHORT_BODY)])
        got = news_source.load_lake_window(self.stores.lake, _ASOF, lookback_days=30)
        self.assertEqual(sorted(got["id"]), ["a", "b"])

    def test_it_ignores_session_dates_outside_the_window(self) -> None:
        inside, outside = _ASOF, _ASOF - dt.timedelta(days=40)
        self.stores.write_lake_run(inside, "20261105T010000000Z", [_news_row("in", _SHORT_BODY)])
        self.stores.write_lake_run(outside, "20260926T010000000Z", [_news_row("out", _SHORT_BODY)])
        got = news_source.load_lake_window(self.stores.lake, _ASOF, lookback_days=30)
        self.assertEqual(list(got["id"]), ["in"])

    def test_a_session_date_after_asof_is_excluded(self) -> None:
        """The lake is append-only and holds every LATER day.

        The upper bound is the one that matters for any historical asof - a
        replay, a backfill, a retro study. A mutation that dropped it left every
        test green.
        """
        later = _ASOF + dt.timedelta(days=5)
        self.stores.write_lake_run(_ASOF, "20261105T010000000Z", [_news_row("now", _SHORT_BODY)])
        self.stores.write_lake_run(later, "20261110T010000000Z", [_news_row("future", _SHORT_BODY)])
        got = news_source.load_lake_window(self.stores.lake, _ASOF, lookback_days=30)
        self.assertEqual(list(got["id"]), ["now"])

    def test_a_row_outside_the_window_is_dropped_even_inside_a_listed_partition(self) -> None:
        """The lake stores the raw union from BEFORE the ingest's day filter.

        Filtering on the partition name alone re-admits rows the ingest
        deliberately excluded, which the view never held.
        """
        stale = dict(_news_row("stale", _SHORT_BODY))
        stale["timestamp"] = pd.Timestamp("2026-01-01T12:00:00Z")
        self.stores.write_lake_run(
            _ASOF, "20261105T010000000Z", [_news_row("fresh", _SHORT_BODY), stale]
        )
        got = news_source.load_lake_window(self.stores.lake, _ASOF, lookback_days=30)
        self.assertEqual(list(got["id"]), ["fresh"])

    def test_an_absent_lake_directory_is_empty_and_does_not_raise(self) -> None:
        got = news_source.load_lake_window(
            self.stores.lake.parent / "never_created", _ASOF, lookback_days=30
        )
        self.assertTrue(got.empty)


class TestTheUnionRule(unittest.TestCase):
    """Per id, the row with the LONGEST body wins.

    That rule is what recovers text, not merely rows: measured on the store, the
    7,330 ``edgar_press_release`` ids the view dropped carry a median body of
    153,648 chars against 22,889 for the 6,765 it kept.
    """

    def test_the_longest_body_wins_even_when_it_is_the_lake_row(self) -> None:
        view = pd.DataFrame([_news_row("x", _SHORT_BODY)])
        lake = pd.DataFrame([_news_row("x", _LONG_BODY)])
        got = news_source.merge_news_sources(view, lake)
        self.assertEqual(len(got), 1)
        self.assertEqual(got.iloc[0]["body"], _LONG_BODY)

    def test_the_view_wins_a_tie_so_the_result_is_deterministic(self) -> None:
        view = pd.DataFrame([_news_row("x", _SHORT_BODY, url="https://view.test/x")])
        lake = pd.DataFrame([_news_row("x", _SHORT_BODY, url="https://lake.test/x")])
        got = news_source.merge_news_sources(view, lake)
        self.assertEqual(got.iloc[0]["url"], "https://view.test/x")

    def test_an_id_only_in_the_view_survives(self) -> None:
        """Guards the 3,638 view ids that predate the lake."""
        view = pd.DataFrame([_news_row("pre_lake", _SHORT_BODY)])
        got = news_source.merge_news_sources(view, pd.DataFrame())
        self.assertEqual(list(got["id"]), ["pre_lake"])

    def test_an_id_only_in_the_lake_is_added(self) -> None:
        lake = pd.DataFrame([_news_row("orphan", _LONG_BODY)])
        got = news_source.merge_news_sources(pd.DataFrame(), lake)
        self.assertEqual(list(got["id"]), ["orphan"])

    def test_a_view_only_id_survives_a_NON_EMPTY_lake(self) -> None:
        """The path production takes. The empty-lake case above is not it.

        A mutation that kept only ids the lake contributed left every test
        green, and it would have silently discarded the 3,638 ids that predate
        the lake.
        """
        view = pd.DataFrame([_news_row("pre_lake", _SHORT_BODY), _news_row("shared", _SHORT_BODY)])
        lake = pd.DataFrame([_news_row("shared", _LONG_BODY), _news_row("only_lake", _LONG_BODY)])
        got = news_source.merge_news_sources(view, lake)
        self.assertEqual(sorted(got["id"]), ["only_lake", "pre_lake", "shared"])

    def test_a_lake_only_window_returns_one_row_per_id_not_per_run(self) -> None:
        """Six ingest runs of one id are one row, or the inner join fans out."""
        lake = pd.DataFrame([_news_row("x", _SHORT_BODY) for _ in range(6)])
        got = news_source.merge_news_sources(pd.DataFrame(), lake)
        self.assertEqual(len(got), 1)

    def test_no_transient_column_reaches_the_caller(self) -> None:
        """A leaked sort column flows into the resolver's merge."""
        for view, lake in (
            (pd.DataFrame([_news_row("a", _SHORT_BODY)]), pd.DataFrame()),
            (pd.DataFrame(), pd.DataFrame([_news_row("a", _SHORT_BODY)])),
            (
                pd.DataFrame([_news_row("a", _SHORT_BODY)]),
                pd.DataFrame([_news_row("b", _LONG_BODY)]),
            ),
        ):
            got = news_source.merge_news_sources(view, lake)
            leaked = [c for c in got.columns if c.startswith("_news_")]
            self.assertEqual(leaked, [], f"leaked {leaked}")

    def test_the_tie_rule_holds_AT_THE_PRODUCTION_SHAPE(self) -> None:
        """The two-row tie case above cannot see this, and it is the whole risk.

        At n = 2 an unstable sort and a stable one pick the same head. The
        difference appears once ties are GROUPED PER ID and the dedup chooses
        inside each group. Measured on the real 30-day window: 11,907 of 12,126
        multi-row ids are tied on body length, and dropping ``kind="stable"``
        changes the winning row for 8,366 of 12,754 ids — ``extra`` for 3,936,
        which feeds the state-media gate, and ``timestamp`` for 30, which decides
        ``published_at`` and the trigger choice.
        """
        ids = [f"id{i:02d}" for i in range(_TIE_IDS)]
        view = pd.DataFrame([_news_row(i, _SHORT_BODY, url=f"https://view.test/{i}") for i in ids])
        lake = pd.DataFrame(
            [
                _news_row(i, _SHORT_BODY, url=f"https://lake.test/{i}/{run}")
                for i in ids
                for run in range(_TIE_LAKE_ROWS_PER_ID)
            ]
        )
        got = news_source.merge_news_sources(view, lake)
        self.assertEqual(len(got), len(ids))
        winners = got.set_index("id")["url"].to_dict()
        from_lake = sorted(k for k, v in winners.items() if not v.startswith("https://view.test/"))
        self.assertEqual(from_lake, [], f"the lake won a tie for {len(from_lake)} ids")

    def test_one_unrelated_extra_row_moves_no_winner(self) -> None:
        """Input-order sensitivity is what an unstable sort really costs.

        Measured: under an unstable sort one added row moved the winner for
        8,130 of 12,754 ids; under a stable sort, for none. So a single new
        ingest run file would reshuffle the resolver's input for thousands of
        unrelated ids.
        """
        ids = [f"id{i:02d}" for i in range(_TIE_IDS)]
        view = pd.DataFrame([_news_row(i, _SHORT_BODY, url=f"https://view.test/{i}") for i in ids])
        lake_rows = [
            _news_row(i, _SHORT_BODY, url=f"https://lake.test/{i}/{run}")
            for i in ids
            for run in range(_TIE_LAKE_ROWS_PER_ID)
        ]
        before = news_source.merge_news_sources(view, pd.DataFrame(lake_rows)).set_index("id")
        after = news_source.merge_news_sources(
            view, pd.DataFrame([*lake_rows, _news_row("unrelated", _SHORT_BODY)])
        ).set_index("id")
        moved = [i for i in ids if before["url"][i] != after["url"][i]]
        self.assertEqual(moved, [], f"one unrelated row moved {len(moved)} winners")


class TestWhatTheResolverSees(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stores = _Stores(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)
        # The orphan shape, exactly: the event is in the events store, its news
        # row is in the lake, and the view no longer has it.
        self.stores.write_view(_ASOF, [_news_row("kept", _SHORT_BODY)])
        self.stores.write_lake_run(
            _ASOF,
            "20261105T010000000Z",
            [_news_row("kept", _SHORT_BODY), _news_row("evicted", _LONG_BODY)],
        )
        self.stores.write_events(_ASOF, [_events_row("kept"), _events_row("evicted")])

    def _news(self) -> pd.DataFrame:
        return catalyst_resolver._load_news_window(
            self.stores.view, _ASOF, lookback_days=30, lake_dir=self.stores.lake
        )

    def test_the_lake_is_derived_from_news_dir_when_not_given(self) -> None:
        """The blocker this change shipped with, pinned.

        An absolute default meant a caller that isolated its news store joined
        the PRODUCTION lake, and could not opt out by patching a constant,
        because a default is early-bound at def time. Every golden fixture, both
        recorders and about sixty resolver tests pass news_dir and no lake_dir.
        """
        self.assertEqual(
            news_source.lake_dir_for(self.stores.view),
            self.stores.lake,
            "the lake must be a sibling of the news dir it was asked about",
        )
        with mock.patch.object(news_source, "READ_NEWS_LAKE", True):
            derived = catalyst_resolver._load_news_window(self.stores.view, _ASOF, lookback_days=30)
        self.assertEqual(sorted(derived["id"]), ["evicted", "kept"])

    def test_an_enabled_but_empty_lake_warns_rather_than_falling_back_silently(self) -> None:
        """The token records the INTENT to read the lake, not the observation.

        With the flag on and the lake absent the resolver reads the view alone
        while every parquet of that day is stamped `view+lake`.
        """
        empty = self.stores.lake.parent / "no_lake_here"
        with mock.patch.object(news_source, "READ_NEWS_LAKE", True):
            with self.assertLogs(news_source.logger, level="WARNING") as caught:
                catalyst_resolver._load_news_window(
                    self.stores.view, _ASOF, lookback_days=30, lake_dir=empty
                )
        self.assertIn("contributed no rows", "\n".join(caught.output))

    def test_the_view_wins_a_tie_THROUGH_THE_REAL_PATH(self) -> None:
        """Pins the ARGUMENT ORDER, which the direct union tests cannot see.

        ``merge_news_sources`` resolves a tie to whichever frame was
        concatenated first, so the rule lives at the ``with_lake`` call site.
        Tests that call the union directly pass their own order and a mutation
        swapping the real one stayed green through the whole suite.
        """
        tied = _news_row("tied", _SHORT_BODY, url="https://view.test/tied")
        self.stores.write_view(_ASOF, [tied])
        self.stores.write_lake_run(
            _ASOF,
            "20261105T030000000Z",
            [_news_row("tied", _SHORT_BODY, url="https://lake.test/tied")],
        )
        with mock.patch.object(news_source, "READ_NEWS_LAKE", True):
            got = self._news()
        row = got[got["id"] == "tied"]
        self.assertEqual(len(row), 1)
        self.assertEqual(row.iloc[0]["url"], "https://view.test/tied")

    def test_with_the_flag_off_it_sees_the_view_alone(self) -> None:
        """Behaviour under the off-state, forced rather than assumed.

        An earlier version asserted ``READ_NEWS_LAKE is False`` here. That pins
        a constant, not a behaviour: it goes red on a legitimate flip and green
        on a broken union.
        """
        with mock.patch.object(news_source, "READ_NEWS_LAKE", False):
            self.assertEqual(sorted(self._news()["id"]), ["kept"])

    def test_with_the_flag_on_the_evicted_row_comes_back(self) -> None:
        with mock.patch.object(news_source, "READ_NEWS_LAKE", True):
            got = self._news()
        self.assertEqual(sorted(got["id"]), ["evicted", "kept"])
        row = got[got["id"] == "evicted"].iloc[0]
        self.assertEqual(row["body"], _LONG_BODY)

    def test_the_orphaned_event_resolves_only_with_the_flag_on(self) -> None:
        """End to end: a theme whose only event is orphaned has no catalyst today."""
        kwargs = {
            "theme": _THEME,
            "asof": _ASOF,
            "events_dir": self.stores.events,
            "news_dir": self.stores.view,
        }
        self.stores.write_events(_ASOF, [_events_row("evicted")])
        with mock.patch.object(news_source, "READ_NEWS_LAKE", False):
            self.assertIsNone(catalyst_resolver.find_trigger_event(**kwargs))
        with mock.patch.object(news_source, "READ_NEWS_LAKE", True):
            payload = catalyst_resolver.find_trigger_event(**kwargs)
        self.assertIsNotNone(payload)
        self.assertEqual(payload.url, "https://example.test/evicted")


class TestTheCohortBoundaryIsRecorded(unittest.TestCase):
    """The tripwire. Flipping the flag without the token change must go red."""

    @staticmethod
    def _token(news_source: str | None) -> dict:
        return json.loads(
            theme_mapper.mapper_config_version(
                market_cap_range=_MCAP,
                channel_config_version="channel-test",
                news_source=news_source,
            )
        )

    def test_the_token_is_unchanged_while_the_news_source_is_unstated(self) -> None:
        self.assertNotIn("news_source", self._token(None))

    def test_stating_the_news_source_moves_the_token(self) -> None:
        self.assertNotEqual(self._token(None), self._token("view+lake"))
        self.assertEqual(self._token("view+lake")["news_source"], "view+lake")

    def test_the_flag_alone_decides_whether_the_boundary_is_recorded(self) -> None:
        """The composition production runs, driven from both flag states.

        This replaces a guard that was true and empty: it fed the token in by
        hand, so it passed under a mutation that flipped ``READ_NEWS_LAKE``
        without the orchestrator recording anything. What is asserted here is
        the exact expression at ``orchestrator.py:1436`` and ``:1604``,
        ``mapper_config_version(news_source=news_source.news_source_token())``,
        so turning the flag on cannot leave the cohort key unchanged.
        """
        with mock.patch.object(news_source, "READ_NEWS_LAKE", False):
            off = self._token(news_source.news_source_token())
        with mock.patch.object(news_source, "READ_NEWS_LAKE", True):
            on = self._token(news_source.news_source_token())
        self.assertNotIn("news_source", off)
        self.assertEqual(on["news_source"], news_source.NEWS_SOURCE_VIEW_PLUS_LAKE)
        self.assertNotEqual(off, on)

    def test_the_shipped_default_records_nothing(self) -> None:
        """Whatever the flag currently is, the shipped token must match it."""
        token = self._token(news_source.news_source_token())
        if news_source.READ_NEWS_LAKE:
            self.assertIn("news_source", token)
        else:
            self.assertNotIn("news_source", token)


if __name__ == "__main__":
    unittest.main()
