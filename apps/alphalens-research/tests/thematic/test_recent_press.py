import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from alphalens_research.thematic.verification import recent_press

# Two-row Polygon response after the 2026-05-22 canonical-client consolidation.
# The mock layer is now ``PolygonClient.get_news_range`` (returns ``list[dict]``)
# instead of the previous ``_http_get_json`` (which returned the full envelope
# with ``results`` / ``next_url``). Pagination + HTTP + retry are owned by
# PolygonClient; tests at this layer only see the flat row list.
SAMPLE_POLYGON_ROWS = [
    {
        "id": "p1",
        "published_utc": "2026-05-10T14:30:00Z",
        "title": "Beam Global expands quantum compute partnerships",
        "description": "Press release announces quantum computing pilot.",
        "tickers": ["BEEM"],
        "keywords": ["quantum", "energy storage"],
        "insights": [],
        "article_url": "https://example.com/beem-quantum",
        "publisher": {"name": "PRNewswire"},
    },
    {
        "id": "p2",
        "published_utc": "2026-05-12T08:00:00Z",
        "title": "Beam Global Q1 earnings beat",
        "description": "Revenue up 50% YoY",
        "tickers": ["BEEM"],
        "keywords": ["earnings"],
        "insights": [],
        "article_url": "https://example.com/beem-q1",
        "publisher": {"name": "Reuters"},
    },
]


def _mock_client(*, rows=None, side_effect=None) -> MagicMock:
    """Build a mock PolygonClient whose ``get_news_range`` returns ``rows``.

    Pass ``side_effect`` to simulate exceptions (rate-limit, network error)
    raised by the canonical client; this is the post-consolidation analogue
    of the old ``patch.object(recent_press, "_http_get_json", side_effect=...)``
    pattern.
    """
    client = MagicMock()
    if side_effect is not None:
        client.get_news_range.side_effect = side_effect
    else:
        client.get_news_range.return_value = rows if rows is not None else SAMPLE_POLYGON_ROWS
    return client


class TestFetchRecentNews(unittest.TestCase):
    def test_fetch_recent_news_calls_polygon_with_ticker_filter(self):
        client = _mock_client()

        items = recent_press.fetch_recent_news(
            ticker="BEEM",
            asof=dt.date(2026, 5, 15),
            lookback_days=30,
            client=client,
        )

        # The canonical PolygonClient is responsible for URL construction +
        # Bearer auth (the api key is NEVER in the URL post-consolidation),
        # so we assert against the kwargs passed to its get_news_range method.
        client.get_news_range.assert_called_once()
        kwargs = client.get_news_range.call_args.kwargs
        self.assertEqual(kwargs["ticker"], "BEEM")
        self.assertEqual(kwargs["start"], dt.datetime(2026, 4, 15, tzinfo=dt.UTC))
        self.assertEqual(kwargs["end"], dt.datetime(2026, 5, 16, tzinfo=dt.UTC))
        self.assertEqual(len(items), 2)

    def test_fetch_caches_to_parquet_and_reuses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            client = _mock_client()
            df = recent_press.fetch_recent_news_cached(
                ticker="BEEM",
                asof=dt.date(2026, 5, 15),
                lookback_days=30,
                client=client,
                cache_dir=cache_dir,
            )
            self.assertEqual(len(df), 2)
            cached = cache_dir / "BEEM_2026-05-15.parquet"
            self.assertTrue(cached.exists())

            blocking_client = MagicMock()
            blocking_client.get_news_range.side_effect = AssertionError("no call")
            df2 = recent_press.fetch_recent_news_cached(
                ticker="BEEM",
                asof=dt.date(2026, 5, 15),
                lookback_days=30,
                client=blocking_client,
                cache_dir=cache_dir,
            )
            self.assertEqual(len(df2), 2)


class TestVerificationGate(unittest.TestCase):
    def test_has_theme_in_recent_press_true_on_keyword_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self.assertTrue(
                recent_press.has_theme_in_recent_press(
                    ticker="BEEM",
                    asof=dt.date(2026, 5, 15),
                    keywords=["quantum"],
                    client=_mock_client(),
                    cache_dir=cache_dir,
                )
            )

    def test_has_theme_in_recent_press_matches_title_or_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            # 'energy storage' is in the keywords field of p1
            self.assertTrue(
                recent_press.has_theme_in_recent_press(
                    ticker="BEEM",
                    asof=dt.date(2026, 5, 15),
                    keywords=["energy storage"],
                    client=_mock_client(),
                    cache_dir=cache_dir,
                )
            )

    def test_has_theme_in_recent_press_false_on_miss(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self.assertFalse(
                recent_press.has_theme_in_recent_press(
                    ticker="BEEM",
                    asof=dt.date(2026, 5, 15),
                    keywords=["alien_invasion", "cybersecurity"],
                    client=_mock_client(),
                    cache_dir=cache_dir,
                )
            )

    def test_has_theme_returns_false_when_no_press_releases(self):
        # PolygonClient returned cleanly with zero rows — real "no press in
        # window" signal, distinct from a fetch error. Stays False per tri-state.
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            result = recent_press.has_theme_in_recent_press(
                ticker="UNKN",
                asof=dt.date(2026, 5, 15),
                keywords=["anything"],
                client=_mock_client(rows=[]),
                cache_dir=cache_dir,
            )
            self.assertIs(result, False)

    def test_has_theme_returns_none_on_api_error(self):
        # PolygonClient rate-limit / network error = unknown, not False.
        # Operator can distinguish "we couldn't check" from "we checked and no hit".
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self.assertIsNone(
                recent_press.has_theme_in_recent_press(
                    ticker="BEEM",
                    asof=dt.date(2026, 5, 15),
                    keywords=["quantum"],
                    client=_mock_client(side_effect=RuntimeError("rate limit")),
                    cache_dir=cache_dir,
                )
            )

    def test_has_theme_in_recent_press_handles_nan_in_keywords_cell(self):
        # Zen pre-merge HIGH finding: the keywords lambda uses ``x is not
        # None`` check, which lets NaN through (NaN is a float). ``" ".join``
        # then raises TypeError. The fetch's exception handler would mark
        # gates_unknown, hiding a real keyword hit elsewhere in the response.
        nan_rows = [
            {
                "id": "p_bad",
                "published_utc": "2026-05-10T14:30:00Z",
                "title": "ignore",
                "description": "",
                "tickers": ["BEEM"],
                "keywords": None,  # parquet round-trip → NaN
                "insights": [],
                "article_url": "u",
                "publisher": {"name": "x"},
            },
            {
                "id": "p_good",
                "published_utc": "2026-05-11T00:00:00Z",
                "title": "Beam launches quantum platform",
                "description": "",
                "tickers": ["BEEM"],
                "keywords": ["quantum"],
                "insights": [],
                "article_url": "u",
                "publisher": {"name": "y"},
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self.assertTrue(
                recent_press.has_theme_in_recent_press(
                    ticker="BEEM",
                    asof=dt.date(2026, 5, 15),
                    keywords=["quantum"],
                    client=_mock_client(rows=nan_rows),
                    cache_dir=cache_dir,
                )
            )


class TestWindowUniverseFetch(unittest.TestCase):
    def test_fetch_window_universe_caches_one_unfiltered_pull(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            client = _mock_client()

            df = recent_press.fetch_window_universe(
                asof=dt.date(2026, 5, 15),
                lookback_days=30,
                client=client,
                cache_dir=cache_dir,
            )
            # Exactly one call to the canonical client (PolygonClient handles
            # pagination internally — wrapper sees a single flat-list response).
            self.assertEqual(client.get_news_range.call_count, 1)
            # Universe-wide fetch must NOT scope to a single ticker.
            self.assertIsNone(client.get_news_range.call_args.kwargs.get("ticker"))
            self.assertEqual(len(df), 2)
            cache_file = cache_dir / "_universe_2026-05-15.parquet"
            self.assertTrue(cache_file.exists())

    def test_fetch_recent_news_passes_through_paginated_rows(self):
        # Pagination is the canonical client's responsibility now; the wrapper
        # receives whatever flat row list PolygonClient produces. Confirm that
        # a "two-pages-worth" row list flows through unmodified.
        two_pages_worth = SAMPLE_POLYGON_ROWS * 2  # 4 rows total
        client = _mock_client(rows=two_pages_worth)

        items = recent_press.fetch_recent_news(
            ticker=None,
            asof=dt.date(2026, 5, 15),
            lookback_days=30,
            client=client,
        )
        self.assertEqual(client.get_news_range.call_count, 1)
        self.assertEqual(len(items), 4)


class TestHasThemeInPressFrame(unittest.TestCase):
    """Tri-state semantics for the in-memory frame matcher.

    - ``True``  — ticker has rows in the frame AND a keyword hit.
    - ``False`` — ticker has rows in the frame, NO keyword hit (real "no").
    - ``None``  — ticker has NO rows in the frame (we don't know; the
      orchestrator should fall back to a per-ticker fetch). Also returned
      when the frame is empty.

    The None case prevents silent false-negatives when Polygon's batch
    firehose fails to tag a ticker on articles that do mention it.
    """

    @staticmethod
    def _two_ticker_frame():
        import pandas as pd

        return pd.DataFrame(
            [
                {
                    "id": "1",
                    "published_utc": "2026-05-10T14:30:00Z",
                    "title": "Beam quantum partnership",
                    "description": "",
                    "url": "u1",
                    "tickers": ["BEEM"],
                    "keywords": ["quantum"],
                    "publisher": "x",
                },
                {
                    "id": "2",
                    "published_utc": "2026-05-11T00:00:00Z",
                    "title": "NVDA earnings",
                    "description": "",
                    "url": "u2",
                    "tickers": ["NVDA"],
                    "keywords": ["earnings"],
                    "publisher": "y",
                },
            ]
        )

    def test_returns_true_when_ticker_present_and_keyword_hits(self):
        df = self._two_ticker_frame()
        self.assertIs(
            recent_press.has_theme_in_press_frame(ticker="BEEM", keywords=["quantum"], press_df=df),
            True,
        )

    def test_returns_false_when_ticker_present_but_keyword_misses(self):
        # NVDA HAS rows in the frame but none mention "quantum" — this is a
        # real "no" (we checked, didn't find), distinct from "we couldn't
        # check because no rows for this ticker". Stays False.
        df = self._two_ticker_frame()
        self.assertIs(
            recent_press.has_theme_in_press_frame(ticker="NVDA", keywords=["quantum"], press_df=df),
            False,
        )

    def test_returns_none_when_ticker_absent_from_frame(self):
        # Polygon's batch firehose did not tag this ticker on any article in
        # the window — we have no evidence either way. Return None so the
        # orchestrator falls back to a per-ticker fetch instead of treating
        # the silence as a real "no". (Issue #149 root cause.)
        df = self._two_ticker_frame()
        self.assertIsNone(
            recent_press.has_theme_in_press_frame(ticker="VRT", keywords=["quantum"], press_df=df)
        )

    def test_empty_frame_returns_none(self):
        # An empty frame is equivalent to "no rows for this ticker" — no
        # evidence, fall through to per-ticker.
        import pandas as pd

        df = pd.DataFrame(
            columns=[
                "id",
                "published_utc",
                "title",
                "description",
                "url",
                "tickers",
                "keywords",
                "publisher",
            ]
        )
        self.assertIsNone(
            recent_press.has_theme_in_press_frame(ticker="BEEM", keywords=["quantum"], press_df=df)
        )

    def test_empty_keywords_returns_none(self):
        # Defensive: caller passed an empty keyword iterable. We can't say
        # "no" (we never matched anything) — return None, not False.
        df = self._two_ticker_frame()
        self.assertIsNone(
            recent_press.has_theme_in_press_frame(ticker="BEEM", keywords=[], press_df=df)
        )

    def test_handles_nan_cells_in_tickers_or_keywords_columns(self):
        # Zen pre-merge HIGH finding: pandas/parquet round-trips can leave
        # NaN in cells where the schema expects a list. ``x is not None``
        # passes for NaN (it's a float), but ``list(x)`` then raises
        # TypeError. The ``_safe`` wrapper would catch and silently mark
        # gates_unknown for every candidate — bypassing the per-ticker
        # fallback entirely on a single NaN row.
        import numpy as np
        import pandas as pd

        df = pd.DataFrame(
            [
                {
                    "id": "nan_row",
                    "published_utc": "2026-05-10T14:30:00Z",
                    "title": "should be ignored",
                    "description": "",
                    "url": "u1",
                    "tickers": np.nan,
                    "keywords": np.nan,
                    "publisher": "x",
                },
                {
                    "id": "real_row",
                    "published_utc": "2026-05-11T00:00:00Z",
                    "title": "NVDA launches quantum tools",
                    "description": "",
                    "url": "u2",
                    "tickers": ["NVDA"],
                    "keywords": ["Quantum"],
                    "publisher": "y",
                },
            ]
        )
        result = recent_press.has_theme_in_press_frame(
            ticker="NVDA", keywords=["quantum"], press_df=df
        )
        self.assertIs(result, True)

    def test_ticker_match_is_case_insensitive_in_dataframe_cells(self):
        # If Polygon ever returns lower-case tickers in `tickers` cells,
        # the ticker filter must still match. ``ticker.upper()`` on the
        # caller side is not enough — the cell contents need normalising too.
        import pandas as pd

        df = pd.DataFrame(
            [
                {
                    "id": "1",
                    "published_utc": "2026-05-10T14:30:00Z",
                    "title": "nvda quantum",
                    "description": "",
                    "url": "u1",
                    "tickers": ["nvda"],  # lower-case
                    "keywords": ["quantum"],
                    "publisher": "x",
                },
            ]
        )
        self.assertIs(
            recent_press.has_theme_in_press_frame(ticker="NVDA", keywords=["quantum"], press_df=df),
            True,
        )


class TestFetchRecentNewsPagination(unittest.TestCase):
    def test_paginates_past_old_max_pages_cap_of_ten(self):
        # Regression test for issue #149 bug 2: the previous cap of 10 pages
        # × 100 limit = 1000 rows covered ~3 days on Polygon's US firehose,
        # NOT the intended 30-day lookback. After the 2026-05-22 canonical
        # client consolidation, pagination is owned by ``PolygonClient`` and
        # tested independently in ``tests/test_polygon_client.py``. At this
        # wrapper layer we only need to confirm that the row count
        # PolygonClient produces flows through unchanged — including beyond
        # the historical 10-page ceiling.
        rows_to_serve = 15  # ten was the old ceiling
        rows = [
            {
                "id": f"p{i}",
                "published_utc": "2026-05-01T00:00:00Z",
                "title": f"page {i}",
                "description": "",
                "tickers": ["BEEM"],
                "keywords": [],
                "article_url": f"https://example.com/{i}",
                "publisher": {"name": "x"},
            }
            for i in range(1, rows_to_serve + 1)
        ]
        client = _mock_client(rows=rows)
        items = recent_press.fetch_recent_news(
            ticker=None,
            asof=dt.date(2026, 5, 15),
            lookback_days=30,
            client=client,
        )
        self.assertEqual(
            len(items),
            rows_to_serve,
            f"expected all {rows_to_serve} rows passed through; got {len(items)}",
        )


if __name__ == "__main__":
    unittest.main()
