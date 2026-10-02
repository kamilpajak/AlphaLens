"""The label stamper gains a second population: names a news feed tagged.

Why this population exists. The briefed panel is 423 episodes over 64 arrival
sessions, and a power simulation on the real structure put a 0.03 effect at 71%
against an 80% bar — the briefed side cannot resolve the effect its own point
estimates suggest. The raw feeds already carry 10197 priceable (date, ticker) pairs
over the same 136 dates, roughly nine times what the briefs keep, and nothing reads
them. Same ingest, same label definition, same horizons.

What these tests protect:

* **the existing store must not move.** #1227 is REGISTERED and PENDING on a frozen
  panel drawn from `selection_labels`. The thematic path has to behave byte for byte
  as it does today, and the news population has to land in its OWN directory.
* **the brief-only steps must not run** for a feed population. Pre-open recovery and
  the publication stamp are facts about a thematic brief; a news article has neither.
* **one row per (date, ticker)**, however many articles tagged it, because the label
  is about the name's forward return and not about the article count.
"""

from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
from alphalens_pipeline.feedback import selection_label as sl


def _news(tmp: Path, date: str, rows: list[dict]) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(tmp / f"{date}.parquet", index=False)
    return tmp / f"{date}.parquet"


class TestTheNewsPopulationSource(unittest.TestCase):
    def setUp(self):
        self._d = TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.news = Path(self._d.name) / "thematic_news"

    def test_one_row_per_date_ticker_however_many_articles_tagged_it(self):
        _news(
            self.news,
            "2026-05-19",
            [
                {"id": "a", "tickers": ["AAPL", "MSFT"], "source": "polygon"},
                {"id": "b", "tickers": ["AAPL"], "source": "polygon"},
            ],
        )
        src = sl.news_population_source(self.news)
        pop = src.build(dt.date(2026, 5, 19))
        self.assertEqual(sorted(pop["ticker"]), ["AAPL", "MSFT"])

    def test_the_population_column_names_this_population(self):
        # The discriminator a reader must filter on. Without it the two populations
        # are indistinguishable in a frame that holds both.
        _news(self.news, "2026-05-19", [{"id": "a", "tickers": ["AAPL"], "source": "polygon"}])
        pop = sl.news_population_source(self.news).build(dt.date(2026, 5, 19))
        self.assertEqual(list(pop["population"]), [sl.POPULATION_NEWS_FEED_TAGGED])

    def test_it_produces_exactly_the_stage_columns_the_schema_declares(self):
        # A missing stage column would raise on write; an extra one is dropped
        # silently by pyarrow, which is the direction that cannot fail on its own.
        _news(self.news, "2026-05-19", [{"id": "a", "tickers": ["AAPL"], "source": "polygon"}])
        pop = sl.news_population_source(self.news).build(dt.date(2026, 5, 19))
        self.assertEqual(list(pop.columns), ["ticker", *sl.STAGE_COLUMNS])

    def test_the_brief_only_stage_values_are_empty_rather_than_invented(self):
        # A feed name was never briefed and never proposed under a theme. Writing
        # True or a theme name here would make it indistinguishable from one that was.
        _news(self.news, "2026-05-19", [{"id": "a", "tickers": ["AAPL"], "source": "polygon"}])
        row = sl.news_population_source(self.news).build(dt.date(2026, 5, 19)).iloc[0]
        self.assertFalse(bool(row["briefed_any_theme"]))
        self.assertEqual(list(row["themes_briefed"]), [])
        self.assertEqual(list(row["themes_proposed"]), [])
        self.assertIsNone(row["mapper_config_version"])

    def test_an_article_with_no_ticker_tag_contributes_nothing(self):
        _news(
            self.news,
            "2026-05-19",
            [
                {"id": "a", "tickers": None, "source": "rss"},
                {"id": "b", "tickers": [], "source": "gdelt"},
                {"id": "c", "tickers": ["AAPL"], "source": "polygon"},
            ],
        )
        pop = sl.news_population_source(self.news).build(dt.date(2026, 5, 19))
        self.assertEqual(list(pop["ticker"]), ["AAPL"])

    def test_tickers_are_upper_cased_and_blanks_dropped(self):
        _news(
            self.news,
            "2026-05-19",
            [{"id": "a", "tickers": ["aapl", "", "  ", "msft"], "source": "polygon"}],
        )
        pop = sl.news_population_source(self.news).build(dt.date(2026, 5, 19))
        self.assertEqual(sorted(pop["ticker"]), ["AAPL", "MSFT"])

    def test_its_dates_are_the_news_files_on_disk(self):
        _news(self.news, "2026-05-19", [{"id": "a", "tickers": ["AAPL"], "source": "polygon"}])
        _news(self.news, "2026-05-20", [{"id": "b", "tickers": ["MSFT"], "source": "polygon"}])
        self.assertEqual(
            sl.news_population_source(self.news).dates(),
            {dt.date(2026, 5, 19), dt.date(2026, 5, 20)},
        )

    def test_a_date_with_no_file_builds_an_empty_population_rather_than_raising(self):
        pop = sl.news_population_source(self.news).build(dt.date(2026, 5, 19))
        self.assertTrue(pop.empty)
        self.assertEqual(list(pop.columns), ["ticker", *sl.STAGE_COLUMNS])

    def test_the_source_declares_that_pre_open_recovery_does_not_apply(self):
        # Pre-open recovery replaces a date's list with the one its BRIEF held at the
        # arrival open. A news feed has no brief and no publication stamp, so running
        # it would drop the whole population on any date the journal knows about.
        self.assertFalse(sl.news_population_source(self.news).pre_open_recovery)
        self.assertTrue(sl.thematic_population_source(Path("b"), Path("s")).pre_open_recovery)


class TestTheThematicSourceStillDescribesTodaysBehaviour(unittest.TestCase):
    """The default path must be the one the live job and #1227's panel already use."""

    def test_the_default_source_is_the_thematic_one(self):
        self.assertTrue(sl.thematic_population_source(Path("b"), Path("s")).pre_open_recovery)

    def test_it_folds_brief_and_shadow_exactly_as_build_population_does(self):
        brief = pd.DataFrame([{"ticker": "AAPL", "theme": "ai", "source": "thematic"}])
        shadow = pd.DataFrame([{"ticker": "MSFT", "theme": "ai", "source": "llm"}])
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "b").mkdir()
            (root / "s").mkdir()
            brief.to_parquet(root / "b" / "2026-05-19.parquet", index=False)
            shadow.to_parquet(root / "s" / "2026-05-19.parquet", index=False)
            got = sl.thematic_population_source(root / "b", root / "s").build(dt.date(2026, 5, 19))
        want = sl.build_population(brief, shadow)
        pd.testing.assert_frame_equal(
            got.reset_index(drop=True), want.reset_index(drop=True), check_dtype=False
        )

    def test_its_dates_are_the_union_of_brief_and_shadow_files(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "b").mkdir()
            (root / "s").mkdir()
            pd.DataFrame([{"ticker": "A"}]).to_parquet(
                root / "b" / "2026-05-19.parquet", index=False
            )
            pd.DataFrame([{"ticker": "B"}]).to_parquet(
                root / "s" / "2026-05-20.parquet", index=False
            )
            self.assertEqual(
                sl.thematic_population_source(root / "b", root / "s").dates(),
                {dt.date(2026, 5, 19), dt.date(2026, 5, 20)},
            )


class TestTheTwoPopulationsStayApart(unittest.TestCase):
    def test_the_news_labels_directory_is_not_the_selection_labels_directory(self):
        # #1227 is registered and pending on a panel drawn from `selection_labels`.
        # Writing a nine-times-larger population into the same files would change what
        # that frozen panel contains.
        self.assertNotEqual(sl.DEFAULT_NEWS_LABELS_DIR, sl.DEFAULT_LABELS_DIR)
        self.assertEqual(sl.DEFAULT_NEWS_LABELS_DIR.name, "news_labels")

    def test_the_two_population_names_differ(self):
        self.assertNotEqual(sl.POPULATION_NEWS_FEED_TAGGED, sl.POPULATION_BRIEFED_OR_PROPOSED)


class TestShadowAvailabilityComesFromTheSourceNotTheFrame(unittest.TestCase):
    """Inferring it from the population disagrees with the old code on an EMPTY date.

    A date can have a shadow file and no population at all — a brief later emptied. The
    old driver passed `shadow is not None`, which is True there; a frame-derived value
    is False, and the recovered rows would then carry the wrong stage value. The source
    reports it so the two cannot diverge.
    """

    def test_the_thematic_source_reports_a_shadow_file_even_when_the_population_is_empty(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "b").mkdir()
            (root / "s").mkdir()
            # a shadow file with no LLM rows -> build_population yields nothing
            pd.DataFrame([{"ticker": "AAPL", "theme": "ai", "source": "mechanical"}]).to_parquet(
                root / "s" / "2026-05-19.parquet", index=False
            )
            src = sl.thematic_population_source(root / "b", root / "s")
            self.assertTrue(src.build(dt.date(2026, 5, 19)).empty)
            self.assertTrue(src.shadow_available(dt.date(2026, 5, 19)))

    def test_a_date_with_no_shadow_file_reports_false(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "b").mkdir()
            (root / "s").mkdir()
            src = sl.thematic_population_source(root / "b", root / "s")
            self.assertFalse(src.shadow_available(dt.date(2026, 5, 19)))

    def test_the_news_source_never_claims_a_shadow(self):
        with TemporaryDirectory() as d:
            src = sl.news_population_source(Path(d))
            self.assertFalse(src.shadow_available(dt.date(2026, 5, 19)))


class TestTheWiringAndNotJustTheAccessor(unittest.TestCase):
    """Mutation found the hole these close.

    Asserting on `source.shadow_available(date)` passes even when `_stamp_date` ignores
    it and goes back to deriving the value from the population frame. The difference
    only shows on the path, so these drive `_stamp_date` and watch what it hands on.
    """

    def _drive(self, source, *, recovered, patched_apply):
        from unittest import mock

        with TemporaryDirectory() as d:
            with (
                mock.patch.object(sl, "pre_open_names", return_value=recovered),
                mock.patch.object(sl, "apply_pre_open_population", patched_apply),
            ):
                sl._stamp_date(
                    dt.date(2026, 5, 19),
                    source=source,
                    labels_dir=Path(d),
                    reader=sl._SessionReader(Path(d)),
                    now=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
                    last_closed_session=dt.date(2026, 8, 29),
                    newest_session=dt.date(2026, 8, 29),
                    counts=__import__("collections").Counter(),
                    exchange="XNYS",
                    references=sl._ReferenceCloses(lambda *a, **k: None),
                )

    def test_stamp_date_passes_the_sources_shadow_flag_not_the_frames(self):
        seen = {}

        def spy(population, recovered, *, shadow_available):
            seen["shadow_available"] = shadow_available
            return population

        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "b").mkdir()
            (root / "s").mkdir()
            # a shadow file that yields NO population: the frame then says False while
            # the source says True, and the old driver said True.
            pd.DataFrame([{"ticker": "AAPL", "theme": "ai", "source": "mechanical"}]).to_parquet(
                root / "s" / "2026-05-19.parquet", index=False
            )
            src = sl.thematic_population_source(root / "b", root / "s")
            self.assertTrue(src.build(dt.date(2026, 5, 19)).empty)
            self._drive(src, recovered=["AAPL"], patched_apply=spy)
        self.assertTrue(
            seen.get("shadow_available"),
            "an empty population with a shadow file must still report the shadow",
        )

    def test_stamp_date_never_runs_pre_open_recovery_for_a_news_source(self):
        calls = []

        def spy(population, recovered, *, shadow_available):
            calls.append(recovered)
            return population

        with TemporaryDirectory() as d:
            news = Path(d) / "news"
            news.mkdir()
            pd.DataFrame([{"id": "a", "tickers": ["AAPL"], "source": "polygon"}]).to_parquet(
                news / "2026-05-19.parquet", index=False
            )
            self._drive(sl.news_population_source(news), recovered=["ZZZZ"], patched_apply=spy)
        self.assertEqual(calls, [], "the brief recovery step must not touch a feed population")


if __name__ == "__main__":
    unittest.main()
