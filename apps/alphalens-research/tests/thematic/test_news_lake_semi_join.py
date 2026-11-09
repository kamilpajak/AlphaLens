"""The lake read is restricted to the news ids some event actually points at.

Measured on the production lake, 30-day window, asof 2026-11-08: of 12,754 ids
the lake holds, **6,579 (51.6%) are referenced by no event at all**, and of the
7,440 ids only the lake has, just 861 (11.6%) have an event. The lake stores the
raw per-source union from BEFORE dedup, clustering, the single-day filter and
the cap, while events are created only from VIEW rows — so most of what a full
lake read costs cannot join by construction.

That read reached 10.01 GB RSS on a 30-day window and was killed. Restricting it
to the ids in hand attacks the cause rather than the bytes per row, and unlike
pruning columns it leaves the longest-body rule untouched.

Both read sites already hold the events frame BEFORE they read the news
(`catalyst_resolver.py:645` then `:661`, and `:840` then `:847`), and the first
one holds it already narrowed to the theme — so the restriction is per theme,
not per window.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from alphalens_pipeline.thematic.mapping import catalyst_resolver, news_source

_ASOF = dt.date(2026, 11, 5)
_THEME = "quantum_computing"
_OTHER_THEME = "steel"
_TICKER = "IONQ"
#: Carries an em dash on purpose. A mutation recording the body length in BYTES
#: instead of CHARACTERS passed every test, and in production it flips the
#: longest-body tie for the 8.1% of real news rows whose two lengths differ.
_BODY = "The press release text\u2014at last."
_BODY_CHARS = len(_BODY)
_BODY_BYTES = len(_BODY.encode("utf-8"))


def _news_row(news_id: str, *, url: str | None = None) -> dict:
    return {
        "id": news_id,
        "source": "edgar_press_release",
        "timestamp": pd.Timestamp(f"{_ASOF.isoformat()}T12:00:00Z"),
        "tickers": [_TICKER],
        "title": f"Headline for {news_id}",
        "body": _BODY,
        "url": url or f"https://example.test/{news_id}",
        "keywords": [],
        "extra": "{}",
        "ingested_at": pd.Timestamp(f"{_ASOF.isoformat()}T12:05:00Z"),
    }


def _events_row(news_id: str, theme: str) -> dict:
    return {
        "news_id": news_id,
        "event_type": "contract_award",
        "primary_entities": [_TICKER],
        "themes": [theme],
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
    def __init__(self, root: Path) -> None:
        self.view = root / "thematic_news"
        self.lake = root / "thematic_news_lake"
        self.events = root / "thematic_events"
        for d in (self.view, self.lake, self.events):
            d.mkdir(parents=True, exist_ok=True)

    def write_view(self, rows: list[dict]) -> None:
        pd.DataFrame(rows).to_parquet(self.view / f"{_ASOF.isoformat()}.parquet", index=False)

    def write_lake_run(self, stamp: str, rows: list[dict]) -> None:
        session = self.lake / f"session_date={_ASOF.isoformat()}"
        session.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(session / f"run={stamp}.parquet", index=False)

    def write_events(self, rows: list[dict]) -> None:
        pd.DataFrame(rows).to_parquet(self.events / f"{_ASOF.isoformat()}.parquet", index=False)


class TestTheLakeReadIsRestricted(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stores = _Stores(Path(tmp.name))
        self.stores.write_lake_run("20261105T010000000Z", [_news_row("wanted"), _news_row("junk")])
        # The window read is cached, so every test starts from a cold cache or
        # it would read a frame another test wrote.
        news_source._read_lake_window.cache_clear()
        self.addCleanup(news_source._read_lake_window.cache_clear)

    def _with_lake(self, needed_ids) -> pd.DataFrame:
        with mock.patch.object(news_source, "READ_NEWS_LAKE", True):
            return news_source.with_lake(
                pd.DataFrame(), self.stores.lake, _ASOF, 30, needed_ids=needed_ids
            )

    def test_only_the_ids_asked_for_come_back(self) -> None:
        self.assertEqual(list(self._with_lake(frozenset({"wanted"}))["id"]), ["wanted"])

    def test_no_restriction_keeps_everything(self) -> None:
        """``None`` means no restriction, which is the pre-existing behaviour."""
        self.assertEqual(sorted(self._with_lake(None)["id"]), ["junk", "wanted"])

    def test_an_empty_id_set_never_touches_the_lake(self) -> None:
        """A theme with no events must not pay for the read, even cold.

        The pushdown this replaced got that for free. An in-memory filter has to
        ask for it, so the property is asserted rather than assumed.
        """
        with mock.patch.object(news_source.pd, "read_parquet") as spy:
            got = self._with_lake(frozenset())
        self.assertTrue(got.empty)
        spy.assert_not_called()

    def test_the_window_is_read_ONCE_however_many_themes_ask(self) -> None:
        """The whole point of the change: one read per run, not one per theme.

        Measured on the production lake, a per-theme read cost about 5.4 s
        because all 93 files are opened whatever is asked for.
        """
        real = news_source.pd.read_parquet
        calls: list[object] = []

        def counting(path, *a, **kw):
            calls.append(path)
            return real(path, *a, **kw)

        with mock.patch.object(news_source.pd, "read_parquet", counting):
            first = self._with_lake(frozenset({"wanted"}))
            after_first = len(calls)
            second = self._with_lake(frozenset({"junk"}))
        self.assertEqual(list(first["id"]), ["wanted"])
        self.assertEqual(list(second["id"]), ["junk"])
        self.assertEqual(
            len(calls),
            after_first,
            f"the second theme re-read the lake: {len(calls)} reads against {after_first}",
        )

    def test_the_cached_frame_is_not_REORDERED_by_a_merge(self) -> None:
        """Narrowing is not the only way to damage a shared object.

        The tie rule leans on the view being concatenated first under a stable
        sort, so a later theme's winner changes if the cached frame's row order
        moves. A mutation sorting both input frames in place left all 38 tests
        green, because the suite pinned only that the frame is not narrowed.
        """
        # The ids must be written in an order a sort would CHANGE, or the
        # mutation this pins is a no-op on the fixture and the test passes for
        # the wrong reason — which is what the first version of it did.
        self.stores.write_lake_run("20261105T020000000Z", [_news_row("aaa"), _news_row("zzz")])
        news_source._read_lake_window.cache_clear()
        with mock.patch.object(news_source, "READ_NEWS_LAKE", True):
            cached = news_source.load_lake_window(self.stores.lake, _ASOF, 30)
            before = list(cached["id"])
            self.assertNotEqual(
                before, sorted(before, reverse=True), "the fixture order must not be pre-sorted"
            )
            news_source.with_lake(
                pd.DataFrame(), self.stores.lake, _ASOF, 30, needed_ids=frozenset({"wanted"})
            )
            news_source.with_lake(pd.DataFrame(), self.stores.lake, _ASOF, 30, needed_ids=None)
        self.assertEqual(
            list(news_source.load_lake_window(self.stores.lake, _ASOF, 30)["id"]),
            before,
            "a merge reordered the cached frame",
        )

    def test_the_cached_frame_is_not_mutated_by_a_restriction(self) -> None:
        """A filter must not narrow what the next theme sees."""
        self.assertEqual(list(self._with_lake(frozenset({"wanted"}))["id"]), ["wanted"])
        self.assertEqual(sorted(self._with_lake(None)["id"]), ["junk", "wanted"])


class TestTheResolverAsksOnlyForWhatItNeeds(unittest.TestCase):
    """The behavioural guard: the restriction must be THE THEME'S ids."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stores = _Stores(Path(tmp.name))
        news_source._read_lake_window.cache_clear()
        self.addCleanup(news_source._read_lake_window.cache_clear)
        # Two orphans, one per theme. Neither is in the view.
        self.stores.write_view([_news_row("in_view")])
        self.stores.write_lake_run(
            "20261105T010000000Z", [_news_row("mine"), _news_row("other_theme")]
        )
        self.stores.write_events(
            [_events_row("mine", _THEME), _events_row("other_theme", _OTHER_THEME)]
        )

    def test_the_other_theme_s_news_row_is_never_read(self) -> None:
        seen: list[frozenset[str]] = []
        real = news_source.with_lake

        def spy(view, lake_dir, asof, lookback_days, *, needed_ids=None):
            seen.append(frozenset(needed_ids) if needed_ids is not None else None)
            return real(view, lake_dir, asof, lookback_days, needed_ids=needed_ids)

        with mock.patch.object(news_source, "READ_NEWS_LAKE", True):
            with mock.patch.object(news_source, "with_lake", spy):
                payload = catalyst_resolver.find_trigger_event(
                    theme=_THEME,
                    asof=_ASOF,
                    events_dir=self.stores.events,
                    news_dir=self.stores.view,
                )
        self.assertIsNotNone(payload)
        self.assertEqual(payload.url, "https://example.test/mine")
        self.assertTrue(seen, "the lake was never read")
        for asked in seen:
            self.assertIsNotNone(asked, "the resolver read the lake unrestricted")
            self.assertNotIn(
                "other_theme",
                asked,
                "the resolver asked for a news id belonging to a different theme",
            )

    def test_a_view_only_id_still_survives_the_restriction(self) -> None:
        """The union is view plus RESTRICTED lake, so the view is untouched."""
        with mock.patch.object(news_source, "READ_NEWS_LAKE", True):
            got = catalyst_resolver._load_news_window(
                self.stores.view, _ASOF, lookback_days=30, needed_ids=frozenset({"mine"})
            )
        self.assertEqual(sorted(got["id"]), ["in_view", "mine"])


class TestAnIncompleteReadIsNeverCached(unittest.TestCase):
    """The cache turned a one-theme blip into a whole-run one.

    `_read_lake_window` treats two transient states as answers: a lake that is
    not there yet, and a file that would not open. Cached, the short window is
    served to every later theme, and the module's only alarm fires on an EMPTY
    window and never on a short one.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stores = _Stores(Path(tmp.name))
        news_source._read_lake_window.cache_clear()
        self.addCleanup(news_source._read_lake_window.cache_clear)

    def test_a_file_that_failed_once_is_not_missing_for_the_whole_run(self) -> None:
        self.stores.write_lake_run("20261105T010000000Z", [_news_row("a")])
        self.stores.write_lake_run("20261105T020000000Z", [_news_row("b")])
        real = news_source.pd.read_parquet
        failed: list[str] = []

        def flaky(path, *a, **kw):
            if "run=20261105T020000000Z" in str(path) and not failed:
                failed.append(str(path))
                raise OSError("parquet magic bytes not found")
            return real(path, *a, **kw)

        with mock.patch.object(news_source.pd, "read_parquet", flaky):
            first = news_source.load_lake_window(self.stores.lake, _ASOF, 30)
            second = news_source.load_lake_window(self.stores.lake, _ASOF, 30)
        self.assertEqual(list(first["id"]), ["a"], "the first read is short, which is expected")
        self.assertEqual(
            sorted(second["id"]),
            ["a", "b"],
            "the second read served the cached PARTIAL window",
        )

    def test_a_lake_that_appears_later_is_not_absent_for_the_whole_run(self) -> None:
        absent = self.stores.lake.parent / "not_mounted_yet"
        self.assertTrue(news_source.load_lake_window(absent, _ASOF, 30).empty)
        (absent / f"session_date={_ASOF.isoformat()}").mkdir(parents=True)
        pd.DataFrame([_news_row("late")]).to_parquet(
            absent / f"session_date={_ASOF.isoformat()}" / "run=20261105T010000000Z.parquet",
            index=False,
        )
        got = news_source.load_lake_window(absent, _ASOF, 30)
        self.assertEqual(list(got["id"]), ["late"], "an absent lake was cached as the answer")

    def test_a_keyword_lookback_is_refused_rather_than_silently_missing_the_cache(self) -> None:
        """Two spellings of one key evict each other at maxsize=1."""
        with self.assertRaises(TypeError):
            news_source.load_lake_window(self.stores.lake, _ASOF, lookback_days=30)  # type: ignore[misc]


class TestTheBodyIsMeasuredAndDropped(unittest.TestCase):
    """`body` is 99.2% of a lake window's bytes and has no reader below the join."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stores = _Stores(Path(tmp.name))
        news_source._read_lake_window.cache_clear()
        self.addCleanup(news_source._read_lake_window.cache_clear)

    def test_the_lake_frame_carries_the_length_and_not_the_text(self) -> None:
        self.stores.write_lake_run("20261105T010000000Z", [_news_row("a")])
        got = news_source.load_lake_window(self.stores.lake, _ASOF, 30)
        self.assertNotIn("body", got.columns)
        self.assertEqual(list(got[news_source._BODY_LENGTH_COLUMN]), [_BODY_CHARS])
        self.assertNotEqual(
            _BODY_CHARS, _BODY_BYTES, "the fixture must make chars and bytes differ"
        )

    def test_the_merged_frame_carries_no_body_from_either_source(self) -> None:
        """A missing COLUMN fails loudly; a NaN in `body` would lie.

        The view's body is 98.8% of the view's bytes, so keeping it
        re-materialised 575 MB the lake read had just avoided — and a lake-won
        row carried a float NaN, which is truthy.
        """
        self.stores.write_view([_news_row("v")])
        self.stores.write_lake_run("20261105T010000000Z", [_news_row("l")])
        with mock.patch.object(news_source, "READ_NEWS_LAKE", True):
            got = catalyst_resolver._load_news_window(self.stores.view, _ASOF, lookback_days=30)
        self.assertEqual(sorted(got["id"]), ["l", "v"])
        self.assertNotIn("body", got.columns)

    def test_the_longest_body_rule_still_works_across_the_two_sources(self) -> None:
        """The rule reads the recorded number, so dropping the text cannot break it.

        The view carries text and no recorded length; the lake carries a length
        and no text. The longer one must still win.
        """
        short, long_ = "tiny", "x" * 500
        view = pd.DataFrame([_news_row("shared")])
        view.loc[:, "body"] = short
        lake_rows = [_news_row("shared", url="https://lake.test/shared")]
        lake_frame = pd.DataFrame(lake_rows)
        lake_frame.loc[:, "body"] = long_
        lake = news_source._measure_and_drop_body(lake_frame)
        got = news_source.merge_news_sources(view, lake)
        self.assertEqual(len(got), 1)
        self.assertEqual(
            got.iloc[0]["url"],
            "https://lake.test/shared",
            "the lake row had the longer body and must still win",
        )

    def test_the_view_still_wins_when_it_is_the_longer_one(self) -> None:
        view = pd.DataFrame([_news_row("shared", url="https://view.test/shared")])
        view.loc[:, "body"] = "y" * 500
        lake_frame = pd.DataFrame([_news_row("shared", url="https://lake.test/shared")])
        lake_frame.loc[:, "body"] = "tiny"
        lake = news_source._measure_and_drop_body(lake_frame)
        got = news_source.merge_news_sources(view, lake)
        self.assertEqual(got.iloc[0]["url"], "https://view.test/shared")


if __name__ == "__main__":
    unittest.main()
