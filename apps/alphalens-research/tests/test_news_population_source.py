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

    def test_a_class_share_is_folded_to_the_house_spelling(self):
        """Feeds emit ``BRK.B``; SEC, our universe and the price vendors use ``BRK-B``.

        Measured on the whole-history news run: 87 of 158 ``split_unchecked`` rows were
        one ticker, ``BRK.B``, and retrying them for 120 days cannot help because the
        cause is the spelling. ``yfinance`` returns 0 closes for ``BRK.B`` and 20 for
        ``BRK-B`` over the same month. `catalyst_resolver._normalize_symbol` already
        folds the separator for exactly this reason.
        """
        _news(
            self.news,
            "2026-05-19",
            [{"id": "a", "tickers": ["BRK.B", "brk.b", "AAPL"], "source": "polygon"}],
        )
        pop = sl.news_population_source(self.news).build(dt.date(2026, 5, 19))
        self.assertEqual(sorted(pop["ticker"]), ["AAPL", "BRK-B"])

    def test_the_folded_spellings_of_one_name_are_one_row_and_not_two(self):
        """Two spellings of one name are one observation; the label is about the name."""
        _news(
            self.news,
            "2026-05-19",
            [
                {"id": "a", "tickers": ["BRK.B"], "source": "polygon"},
                {"id": "b", "tickers": ["BRK-B"], "source": "gdelt"},
            ],
        )
        pop = sl.news_population_source(self.news).build(dt.date(2026, 5, 19))
        self.assertEqual(list(pop["ticker"]), ["BRK-B"])

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


class TestThePublicationGateDoesNotApplyToAFeed(unittest.TestCase):
    """Found by adversarial review of this PR, and it would have been silent.

    `published_before_open` asks whether the stored LIST existed before the arrival
    open — a question about a thematic brief being rewritten after it. A news feed has
    no list and no publication stamp, so the gate answers from the journal window
    (2026-05-19 .. 2026-09-15) and returns None outside it.

    None makes `compute_selection_label` return a uniform `publication_unknown`, which
    is NON-TERMINAL. So every feed row from 2026-09-16 onward would be recomputed on
    every run and never carry a label, while the job reported success — and that window
    is exactly the post-Jev-release sample the wide population exists to collect.
    """

    def test_a_feed_date_after_the_journal_window_is_not_publication_unknown(self):
        from alphalens_pipeline.thematic.publication import HISTORY_RECORD_WINDOW

        after = HISTORY_RECORD_WINDOW[1] + dt.timedelta(days=30)
        from alphalens_pipeline.feedback.split_audit import SpanAudit

        clean = SpanAudit(answered=True, breaks=frozenset(), unchecked=frozenset())
        label = sl.compute_selection_label(
            {},
            "AAPL",
            brief_date=after,
            published_before_open=sl.publication_verdict_for(
                sl.news_population_source(Path("nowhere")), after, population=None
            ),
            last_closed_session=dt.date(2026, 12, 31),
            newest_session=dt.date(2026, 12, 31),
            audit=clean,
        )
        self.assertNotEqual(
            label.statuses[sl.ar_key(20)],
            sl.STATUS_PUBLICATION_UNKNOWN,
            "a feed row has no publication to be unknown about",
        )

    def test_the_first_journal_date_is_not_set_final_after_open_for_a_feed(self):
        from alphalens_pipeline.thematic.publication import HISTORY_RECORD_WINDOW

        first = HISTORY_RECORD_WINDOW[0]
        verdict = sl.publication_verdict_for(
            sl.news_population_source(Path("nowhere")), first, population=None
        )
        self.assertIsNot(verdict, False, "a feed has no list that could be set after the open")

    def test_the_thematic_source_still_consults_the_gate(self):
        # The gate is real for a brief and must keep working: the whole point of #1494.
        from alphalens_pipeline.thematic.publication import HISTORY_RECORD_WINDOW

        src = sl.thematic_population_source(Path("b"), Path("s"))
        pop = pd.DataFrame([{"ticker": "AAPL", sl.BRIEF_PUBLISHED_AT: None}])
        after = HISTORY_RECORD_WINDOW[1] + dt.timedelta(days=30)
        self.assertIsNone(sl.publication_verdict_for(src, after, population=pop))

    def test_a_source_declares_whether_the_gate_applies(self):
        self.assertTrue(sl.thematic_population_source(Path("b"), Path("s")).publication_gate)
        self.assertFalse(sl.news_population_source(Path("nowhere")).publication_gate)


class TestTheGateFlagReachesTheWrittenRow(unittest.TestCase):
    """The third time in this change that a test on the accessor missed the path.

    Asserting on `publication_verdict_for` passes even when `_stamp_date` ignores
    `source.publication_gate` and consults the journal anyway. Only a run that reads
    the row it wrote can tell.
    """

    def _stamp(self, source, date):
        from collections import Counter

        with TemporaryDirectory() as d:
            out = Path(d) / "labels"
            out.mkdir()
            sl._stamp_date(
                date,
                source=source,
                labels_dir=out,
                reader=sl._SessionReader(Path(d) / "no_prices"),
                now=dt.datetime(2027, 6, 1, tzinfo=dt.UTC),
                last_closed_session=dt.date(2027, 5, 28),
                newest_session=dt.date(2027, 5, 28),
                counts=Counter(),
                exchange="XNYS",
                references=sl._ReferenceCloses(lambda *a, **k: None),
            )
            path = out / f"{date.isoformat()}.parquet"
            return pd.read_parquet(path) if path.exists() else pd.DataFrame()

    def test_a_feed_row_past_the_journal_window_is_never_publication_unknown(self):
        from alphalens_pipeline.thematic.publication import HISTORY_RECORD_WINDOW

        after = HISTORY_RECORD_WINDOW[1] + dt.timedelta(days=30)
        with TemporaryDirectory() as d:
            news = Path(d) / "news"
            news.mkdir()
            pd.DataFrame([{"id": "a", "tickers": ["AAPL"], "source": "polygon"}]).to_parquet(
                news / f"{after.isoformat()}.parquet", index=False
            )
            rows = self._stamp(sl.news_population_source(news), after)
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(
            rows[sl.status_key(20)].iloc[0],
            sl.STATUS_PUBLICATION_UNKNOWN,
            "the gate must not be consulted for a population that has no list",
        )

    def test_a_brief_row_past_the_journal_window_still_is_publication_unknown(self):
        # The gate is real for a brief with no stamp, and #1494 is why. If this ever
        # stops holding, the flag has been applied to the wrong source.
        from alphalens_pipeline.thematic.publication import HISTORY_RECORD_WINDOW

        after = HISTORY_RECORD_WINDOW[1] + dt.timedelta(days=30)
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "b").mkdir()
            (root / "s").mkdir()
            pd.DataFrame([{"ticker": "AAPL", "theme": "ai", "source": "thematic"}]).to_parquet(
                root / "b" / f"{after.isoformat()}.parquet", index=False
            )
            rows = self._stamp(sl.thematic_population_source(root / "b", root / "s"), after)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[sl.status_key(20)].iloc[0], sl.STATUS_PUBLICATION_UNKNOWN)


class TestTheReviewFindings(unittest.TestCase):
    """Five findings from the zen pass, each adjudicated by running it."""

    def test_a_feed_row_carries_its_own_lane_not_the_thematic_one(self):
        # `2026_09_experts_last_look.py:313` already does `keep = lane == SOURCE_LANE`,
        # so this is not a documentation nicety: a feed row labelled "thematic" would be
        # picked up by a filter meant for the thematic funnel.
        with TemporaryDirectory() as d:
            news = Path(d)
            pd.DataFrame([{"id": "a", "tickers": ["AAPL"], "source": "polygon"}]).to_parquet(
                news / "2026-05-19.parquet", index=False
            )
            row = sl.news_population_source(news).build(dt.date(2026, 5, 19)).iloc[0]
        self.assertEqual(row["lane"], sl.LANE_NEWS_FEED)
        self.assertNotEqual(row["lane"], sl.LANE_THEMATIC)

    def test_a_thematic_row_keeps_the_thematic_lane(self):
        brief = pd.DataFrame([{"ticker": "AAPL", "theme": "ai", "source": "thematic"}])
        self.assertEqual(sl.build_population(brief, None)["lane"].iloc[0], sl.LANE_THEMATIC)

    def test_a_scalar_in_the_tickers_column_is_skipped_not_split_into_letters(self):
        """Measured: iterating the string "AAPL" yields 'A','A','P','L' — four junk
        tickers that would be stamped and stored. A try/except on TypeError, which the
        review suggested, catches a NaN but NOT this, because it does not raise.

        Adjudicated further: this shape cannot come out of a parquet file, whose column
        is typed, and `to_parquet` refuses a mixed list/string column outright. So the
        guard protects against an object-dtype frame reaching the builder some other
        way, which is why the frame is injected rather than written to disk. The guard
        is cheap and the alternative failure is silent.
        """
        from unittest import mock

        frame = pd.DataFrame({"tickers": pd.Series(["AAPL", ["MSFT"]], dtype=object)})
        with TemporaryDirectory() as d:
            news = Path(d)
            pd.DataFrame([{"id": "x", "tickers": ["ZZZZ"], "source": "polygon"}]).to_parquet(
                news / "2026-05-19.parquet", index=False
            )
            with mock.patch.object(sl.pd, "read_parquet", return_value=frame):
                pop = sl.news_population_source(news).build(dt.date(2026, 5, 19))
        self.assertEqual(list(pop["ticker"]), ["MSFT"], "a bare string must contribute nothing")

    def test_a_nan_in_the_tickers_column_does_not_abort_the_date(self):
        with TemporaryDirectory() as d:
            news = Path(d)
            pd.DataFrame(
                [
                    {"id": "a", "tickers": None, "source": "rss"},
                    {"id": "b", "tickers": ["MSFT"], "source": "polygon"},
                ]
            ).to_parquet(news / "2026-05-19.parquet", index=False)
            pop = sl.news_population_source(news).build(dt.date(2026, 5, 19))
        self.assertEqual(list(pop["ticker"]), ["MSFT"])

    def test_stamp_date_reaches_the_gate_only_through_the_shared_helper(self):
        """Two copies of the same decision, one tested and one inline, is how the defect
        this PR fixed would come back. Asserted behaviourally rather than by grepping the
        source: a source count caught the function DEFINITION as well as the call, which
        is the third brittle text assertion of this change.
        """
        from collections import Counter
        from unittest import mock

        calls = []

        def spy(source, brief_date, *, population, recovered=None, exchange="XNYS"):
            calls.append(brief_date)
            return True

        with TemporaryDirectory() as d:
            news = Path(d) / "news"
            news.mkdir()
            pd.DataFrame([{"id": "a", "tickers": ["AAPL"], "source": "polygon"}]).to_parquet(
                news / "2026-05-19.parquet", index=False
            )
            with mock.patch.object(sl, "publication_verdict_for", spy):
                sl._stamp_date(
                    dt.date(2026, 5, 19),
                    source=sl.news_population_source(news),
                    labels_dir=Path(d) / "out",
                    reader=sl._SessionReader(Path(d) / "no_prices"),
                    now=dt.datetime(2027, 6, 1, tzinfo=dt.UTC),
                    last_closed_session=dt.date(2027, 5, 28),
                    newest_session=dt.date(2027, 5, 28),
                    counts=Counter(),
                    exchange="XNYS",
                    references=sl._ReferenceCloses(lambda *a, **k: None),
                )
        self.assertEqual(
            calls, [dt.date(2026, 5, 19)], "_stamp_date must not carry its own copy of the gate"
        )

    def test_the_version_constant_explains_why_a_new_population_does_not_bump_it(self):
        # The constant's comment lists "the population rule" among the things that force
        # a bump. A reader hitting that will read the omission as an oversight unless the
        # reasoning sits next to it.
        source = _SCRIPT_SOURCE()
        head = source[: source.index("SEL_LABEL_VERSION") + 2000]
        self.assertIn("PopulationSource", head)

    def test_not_applicable_is_documented_where_shadow_available_is_declared(self):
        self.assertIn("not applicable", _SCRIPT_SOURCE())


def _SCRIPT_SOURCE() -> str:
    import alphalens_pipeline.feedback.selection_label as mod

    return Path(mod.__file__).read_text()


if __name__ == "__main__":
    unittest.main()
