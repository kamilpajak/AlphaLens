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
:meth:`test_the_token_must_record_the_lake_once_the_flag_is_on` is the tripwire
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
            "lake_dir": self.stores.lake,
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
