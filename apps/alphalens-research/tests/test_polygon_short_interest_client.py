"""Tests for Polygon /stocks/v1/short-interest client (domain wrapper).

PIT contract: at asof t, only settlements with (settlement_date + 8 BD) <= t are
visible. Disk cache at ~/.alphalens/polygon_short_interest/{ticker}.parquet.

After the 2026-05-22 canonical-client consolidation, this wrapper delegates HTTP
to :class:`alphalens_pipeline.data.alt_data.polygon_client.PolygonClient` via DI. Tests
mock at the client level (``polygon_client.get_short_interest``) instead of at
the requests / urllib level — that's the supported mock layer post-migration
and matches the SecEdgar / AlphaVantage / Gemini test patterns.

Locked into v4 v2 pre-reg per
docs/research/preregistration/params_alt_data_screener_v2_2026_04_30.json:
features short_interest_pct_float_change_60d, rank_short_interest_pct_float,
log1p_days_to_cover all source from this client.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

_SAMPLE_AAPL_ROWS = [
    {
        "settlement_date": "2024-01-12",
        "ticker": "AAPL",
        "short_interest": 101_263_039,
        "avg_daily_volume": 50_000_000,
        "days_to_cover": 2.03,
    },
    {
        "settlement_date": "2024-01-31",
        "ticker": "AAPL",
        "short_interest": 99_244_672,
        "avg_daily_volume": 51_000_000,
        "days_to_cover": 1.95,
    },
    {
        "settlement_date": "2024-02-15",
        "ticker": "AAPL",
        "short_interest": 97_665_956,
        "avg_daily_volume": 49_500_000,
        "days_to_cover": 1.97,
    },
]


def _mock_polygon_client(*, rows: list[dict] | None = None) -> MagicMock:
    """Build a mock PolygonClient that returns ``rows`` from get_short_interest."""
    client = MagicMock()
    client.get_short_interest.return_value = rows if rows is not None else _SAMPLE_AAPL_ROWS
    return client


class TestPolygonShortInterestClient(unittest.TestCase):
    def test_fetch_ticker_parses_response(self):
        from alphalens_pipeline.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        with tempfile.TemporaryDirectory() as tmp:
            polygon_client = _mock_polygon_client()
            client = PolygonShortInterestClient(
                cache_dir=Path(tmp),
                polygon_client=polygon_client,
            )
            df = client.fetch_ticker("AAPL")

            self.assertEqual(len(df), 3)
            self.assertEqual(
                list(df.columns), ["short_interest", "avg_daily_volume", "days_to_cover"]
            )
            self.assertEqual(df.index.name, "settlement_date")
            # settlement_date should be parsed to datetime
            self.assertEqual(df.index[0], __import__("pandas").Timestamp("2024-01-12"))
            self.assertEqual(int(df.iloc[0]["short_interest"]), 101_263_039)
            self.assertAlmostEqual(float(df.iloc[2]["days_to_cover"]), 1.97, places=4)

    def test_fetch_ticker_caches_to_parquet(self):
        from alphalens_pipeline.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        with tempfile.TemporaryDirectory() as tmp:
            polygon_client = _mock_polygon_client()
            client = PolygonShortInterestClient(
                cache_dir=Path(tmp),
                polygon_client=polygon_client,
            )

            df1 = client.fetch_ticker("AAPL")
            self.assertEqual(polygon_client.get_short_interest.call_count, 1)
            df2 = client.fetch_ticker("AAPL")  # second call hits cache
            self.assertEqual(polygon_client.get_short_interest.call_count, 1)
            self.assertTrue(df1.equals(df2))
            self.assertTrue((Path(tmp) / "AAPL.parquet").exists())

    def test_fetch_ticker_follows_pagination(self):
        """Pagination now happens inside PolygonClient; the wrapper just receives
        a flat list of rows. Test that >1 page worth of rows are accepted."""
        from alphalens_pipeline.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        paginated_rows = [
            *_SAMPLE_AAPL_ROWS,
            {
                "settlement_date": "2024-02-29",
                "ticker": "AAPL",
                "short_interest": 95000000,
                "avg_daily_volume": 50000000,
                "days_to_cover": 1.9,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            polygon_client = _mock_polygon_client(rows=paginated_rows)
            client = PolygonShortInterestClient(
                cache_dir=Path(tmp),
                polygon_client=polygon_client,
            )
            df = client.fetch_ticker("AAPL")
            self.assertEqual(len(df), 4)
            # Wrapper makes exactly one call to PolygonClient — pagination is
            # the canonical client's responsibility now.
            self.assertEqual(polygon_client.get_short_interest.call_count, 1)

    def test_fetch_ticker_empty_results(self):
        from alphalens_pipeline.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        with tempfile.TemporaryDirectory() as tmp:
            polygon_client = _mock_polygon_client(rows=[])
            client = PolygonShortInterestClient(
                cache_dir=Path(tmp),
                polygon_client=polygon_client,
            )
            df = client.fetch_ticker("BOGUSXYZ")
            self.assertEqual(len(df), 0)
            self.assertEqual(
                list(df.columns), ["short_interest", "avg_daily_volume", "days_to_cover"]
            )

    def test_fetch_ticker_401_raises(self):
        """401 now surfaces as ``PolygonAuthError`` from the canonical client;
        the wrapper re-exports ``PolygonShortInterestAuthError`` as an alias so
        existing ``except`` clauses keep working."""
        from alphalens_pipeline.data.alt_data.polygon_client import PolygonAuthError
        from alphalens_pipeline.data.alt_data.polygon_short_interest import (
            PolygonShortInterestAuthError,
            PolygonShortInterestClient,
        )

        # Confirm the historical alias points at the canonical exception
        self.assertIs(PolygonShortInterestAuthError, PolygonAuthError)

        with tempfile.TemporaryDirectory() as tmp:
            polygon_client = MagicMock()
            polygon_client.get_short_interest.side_effect = PolygonAuthError(
                "Polygon 401: API key rejected"
            )
            client = PolygonShortInterestClient(
                cache_dir=Path(tmp),
                polygon_client=polygon_client,
            )
            with self.assertRaises(PolygonShortInterestAuthError):
                client.fetch_ticker("AAPL")


class TestPITContract(unittest.TestCase):
    """8 trading-day dissemination lag (FINRA Rule 4560)."""

    def test_available_at_blocks_within_lag_window(self):
        from alphalens_pipeline.data.alt_data.polygon_short_interest import _is_available_at

        # Settlement Fri 2024-01-12. +8 BD (no holidays in window):
        # Mon 1/15(=+1), Tue 1/16(+2), Wed 1/17(+3), Thu 1/18(+4), Fri 1/19(+5),
        # Mon 1/22(+6), Tue 1/23(+7), Wed 1/24(+8). Available from Wed 2024-01-24.
        self.assertFalse(_is_available_at(asof=date(2024, 1, 22), settlement=date(2024, 1, 12)))
        self.assertFalse(_is_available_at(asof=date(2024, 1, 23), settlement=date(2024, 1, 12)))
        self.assertTrue(_is_available_at(asof=date(2024, 1, 24), settlement=date(2024, 1, 12)))
        self.assertTrue(_is_available_at(asof=date(2024, 1, 25), settlement=date(2024, 1, 12)))

    def test_available_at_handles_month_end_settlement(self):
        from alphalens_pipeline.data.alt_data.polygon_short_interest import _is_available_at

        # FINRA settlement Wed 2024-01-31 (last BD of January 2024). +8 BD:
        # Thu 2/1(=+1), Fri 2/2(+2), Mon 2/5(+3), Tue 2/6(+4), Wed 2/7(+5),
        # Thu 2/8(+6), Fri 2/9(+7), Mon 2/12(+8). Available from Mon 2024-02-12.
        self.assertFalse(_is_available_at(asof=date(2024, 2, 11), settlement=date(2024, 1, 31)))
        self.assertTrue(_is_available_at(asof=date(2024, 2, 12), settlement=date(2024, 1, 31)))
        self.assertTrue(_is_available_at(asof=date(2024, 2, 13), settlement=date(2024, 1, 31)))


class TestFeaturesAsOf(unittest.TestCase):
    def test_features_as_of_returns_most_recent_eligible(self):
        from alphalens_pipeline.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        with tempfile.TemporaryDirectory() as tmp:
            polygon_client = _mock_polygon_client()
            client = PolygonShortInterestClient(
                cache_dir=Path(tmp),
                polygon_client=polygon_client,
            )

            # 1/12 +8 BD = 1/24, 1/31 +8 BD = 2/12, 2/15 +8 BD = 2/27.
            # asof 2024-02-11: only 1/12 settlement is eligible.
            rec = client.features_as_of("AAPL", date(2024, 2, 11))
            self.assertIsNotNone(rec)
            self.assertEqual(rec.settlement_date, date(2024, 1, 12))

            # asof 2024-02-12: 1/31 settlement just became eligible — most recent.
            rec = client.features_as_of("AAPL", date(2024, 2, 12))
            self.assertIsNotNone(rec)
            self.assertEqual(rec.settlement_date, date(2024, 1, 31))

            # asof 2024-02-26: 2/15 not yet eligible; latest is still 1/31.
            rec = client.features_as_of("AAPL", date(2024, 2, 26))
            self.assertIsNotNone(rec)
            self.assertEqual(rec.settlement_date, date(2024, 1, 31))

    def test_features_as_of_returns_none_pre_history(self):
        from alphalens_pipeline.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        with tempfile.TemporaryDirectory() as tmp:
            polygon_client = _mock_polygon_client()
            client = PolygonShortInterestClient(
                cache_dir=Path(tmp),
                polygon_client=polygon_client,
            )

            # Before any settlement+8BD is reachable
            rec = client.features_as_of("AAPL", date(2023, 12, 31))
            self.assertIsNone(rec)

    def test_features_as_of_returns_none_for_missing_ticker(self):
        from alphalens_pipeline.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        with tempfile.TemporaryDirectory() as tmp:
            polygon_client = _mock_polygon_client(rows=[])
            client = PolygonShortInterestClient(
                cache_dir=Path(tmp),
                polygon_client=polygon_client,
            )
            rec = client.features_as_of("BOGUSXYZ", date(2024, 6, 1))
            self.assertIsNone(rec)


class TestFeaturesAsOfStalenessRefresh(unittest.TestCase):
    """#1269: the no-TTL disk cache needs an opt-in freshness rule.

    Without it, the first fetch freezes a ticker's history forever and the
    stamped si_settlement_date goes stale ~2 weeks after first touch.
    """

    _ASOF = date(2024, 6, 1)
    _STALE_ROWS = [
        {
            "settlement_date": "2024-02-15",
            "ticker": "AAPL",
            "short_interest": 97_665_956,
            "avg_daily_volume": 49_500_000,
            "days_to_cover": 1.97,
        }
    ]
    _FRESH_ROW = {
        "settlement_date": "2024-05-15",
        "ticker": "AAPL",
        "short_interest": 95_000_000,
        "avg_daily_volume": 48_000_000,
        "days_to_cover": 1.98,
    }

    def _client(self, polygon_client):
        from alphalens_pipeline.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return PolygonShortInterestClient(
            cache_dir=Path(self.tmp.name), polygon_client=polygon_client
        )

    def test_fresh_cache_is_not_refetched(self):
        polygon_client = _mock_polygon_client(rows=[*self._STALE_ROWS, self._FRESH_ROW])
        client = self._client(polygon_client)
        client.fetch_ticker("AAPL")  # seed the cache (1 HTTP call)

        rec = client.features_as_of("AAPL", self._ASOF, refresh_if_stale=True)

        self.assertEqual(polygon_client.get_short_interest.call_count, 1)
        self.assertEqual(rec.settlement_date, date(2024, 5, 15))

    def test_stale_cache_triggers_exactly_one_refresh(self):
        polygon_client = _mock_polygon_client()
        polygon_client.get_short_interest.side_effect = [
            self._STALE_ROWS,  # seed: newest settlement > 30d before asof
            [*self._STALE_ROWS, self._FRESH_ROW],  # refresh sees the new print
        ]
        client = self._client(polygon_client)
        client.fetch_ticker("AAPL")

        rec = client.features_as_of("AAPL", self._ASOF, refresh_if_stale=True)

        self.assertEqual(polygon_client.get_short_interest.call_count, 2)
        self.assertEqual(rec.settlement_date, date(2024, 5, 15))

    def test_refresh_yielding_nothing_returns_none_without_looping(self):
        polygon_client = _mock_polygon_client(rows=[])
        client = self._client(polygon_client)
        client.fetch_ticker("AAPL")

        rec = client.features_as_of("AAPL", self._ASOF, refresh_if_stale=True)

        self.assertIsNone(rec)
        self.assertEqual(polygon_client.get_short_interest.call_count, 2)

    def test_default_never_refreshes(self):
        # refresh_if_stale defaults to False — the pre-registered v2 research
        # features keep their frozen-cache reproducibility.
        polygon_client = _mock_polygon_client(rows=self._STALE_ROWS)
        client = self._client(polygon_client)
        client.fetch_ticker("AAPL")

        rec = client.features_as_of("AAPL", self._ASOF)

        self.assertEqual(polygon_client.get_short_interest.call_count, 1)
        self.assertEqual(rec.settlement_date, date(2024, 2, 15))


if __name__ == "__main__":
    unittest.main()
