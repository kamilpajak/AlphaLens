"""Tests for Polygon /stocks/v1/short-interest client.

PIT contract: at asof t, only settlements with (settlement_date + 8 BD) <= t are
visible. Disk cache at ~/.alphalens/polygon_short_interest/{ticker}.parquet.

Locked into v4 v2 pre-reg per
docs/research/preregistration/params_alt_data_screener_v2_2026_04_30.json:
features short_interest_pct_float_change_60d, rank_short_interest_pct_float,
log1p_days_to_cover all source from this client.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock


def _response(status: int, body=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body or {}
    resp.text = json.dumps(body) if body else ""
    return resp


_SAMPLE_AAPL_PAGE = {
    "status": "OK",
    "request_id": "test",
    "results": [
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
    ],
}


class TestPolygonShortInterestClient(unittest.TestCase):
    def test_fetch_ticker_parses_response(self):
        from alphalens.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            session.get.return_value = _response(200, _SAMPLE_AAPL_PAGE)
            client = PolygonShortInterestClient(
                api_key="test-key",
                cache_dir=Path(tmp),
                session=session,
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
        from alphalens.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            session.get.return_value = _response(200, _SAMPLE_AAPL_PAGE)
            client = PolygonShortInterestClient(
                api_key="test-key",
                cache_dir=Path(tmp),
                session=session,
            )

            df1 = client.fetch_ticker("AAPL")
            self.assertEqual(session.get.call_count, 1)
            df2 = client.fetch_ticker("AAPL")  # second call hits cache
            self.assertEqual(session.get.call_count, 1)
            self.assertTrue(df1.equals(df2))
            self.assertTrue((Path(tmp) / "AAPL.parquet").exists())

    def test_fetch_ticker_follows_pagination(self):
        from alphalens.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        page1 = dict(_SAMPLE_AAPL_PAGE)
        page1["next_url"] = "https://api.polygon.io/stocks/v1/short-interest?cursor=p2"
        page2 = {
            "status": "OK",
            "results": [
                {
                    "settlement_date": "2024-02-29",
                    "ticker": "AAPL",
                    "short_interest": 95_000_000,
                    "avg_daily_volume": 50_000_000,
                    "days_to_cover": 1.90,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            session.get.side_effect = [_response(200, page1), _response(200, page2)]
            client = PolygonShortInterestClient(
                api_key="test-key",
                cache_dir=Path(tmp),
                session=session,
            )
            df = client.fetch_ticker("AAPL")
            self.assertEqual(len(df), 4)
            self.assertEqual(session.get.call_count, 2)

    def test_fetch_ticker_empty_results(self):
        from alphalens.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            session.get.return_value = _response(200, {"status": "OK", "results": []})
            client = PolygonShortInterestClient(
                api_key="test-key",
                cache_dir=Path(tmp),
                session=session,
            )
            df = client.fetch_ticker("BOGUSXYZ")
            self.assertEqual(len(df), 0)
            self.assertEqual(
                list(df.columns), ["short_interest", "avg_daily_volume", "days_to_cover"]
            )

    def test_fetch_ticker_401_raises(self):
        from alphalens.data.alt_data.polygon_short_interest import (
            PolygonShortInterestAuthError,
            PolygonShortInterestClient,
        )

        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            session.get.return_value = _response(401, {"error": "unauthorized"})
            client = PolygonShortInterestClient(
                api_key="bad-key",
                cache_dir=Path(tmp),
                session=session,
            )
            with self.assertRaises(PolygonShortInterestAuthError):
                client.fetch_ticker("AAPL")


class TestPITContract(unittest.TestCase):
    """8 trading-day dissemination lag (FINRA Rule 4560)."""

    def test_available_at_blocks_within_lag_window(self):
        from alphalens.data.alt_data.polygon_short_interest import _is_available_at

        # Settlement Fri 2024-01-12. +8 BD (no holidays in window):
        # Mon 1/15(=+1), Tue 1/16(+2), Wed 1/17(+3), Thu 1/18(+4), Fri 1/19(+5),
        # Mon 1/22(+6), Tue 1/23(+7), Wed 1/24(+8). Available from Wed 2024-01-24.
        self.assertFalse(_is_available_at(asof=date(2024, 1, 22), settlement=date(2024, 1, 12)))
        self.assertFalse(_is_available_at(asof=date(2024, 1, 23), settlement=date(2024, 1, 12)))
        self.assertTrue(_is_available_at(asof=date(2024, 1, 24), settlement=date(2024, 1, 12)))
        self.assertTrue(_is_available_at(asof=date(2024, 1, 25), settlement=date(2024, 1, 12)))

    def test_available_at_handles_month_end_settlement(self):
        from alphalens.data.alt_data.polygon_short_interest import _is_available_at

        # FINRA settlement Wed 2024-01-31 (last BD of January 2024). +8 BD:
        # Thu 2/1(=+1), Fri 2/2(+2), Mon 2/5(+3), Tue 2/6(+4), Wed 2/7(+5),
        # Thu 2/8(+6), Fri 2/9(+7), Mon 2/12(+8). Available from Mon 2024-02-12.
        self.assertFalse(_is_available_at(asof=date(2024, 2, 11), settlement=date(2024, 1, 31)))
        self.assertTrue(_is_available_at(asof=date(2024, 2, 12), settlement=date(2024, 1, 31)))
        self.assertTrue(_is_available_at(asof=date(2024, 2, 13), settlement=date(2024, 1, 31)))


class TestFeaturesAsOf(unittest.TestCase):
    def test_features_as_of_returns_most_recent_eligible(self):
        from alphalens.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            session.get.return_value = _response(200, _SAMPLE_AAPL_PAGE)
            client = PolygonShortInterestClient(
                api_key="test-key",
                cache_dir=Path(tmp),
                session=session,
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
        from alphalens.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            session.get.return_value = _response(200, _SAMPLE_AAPL_PAGE)
            client = PolygonShortInterestClient(
                api_key="test-key",
                cache_dir=Path(tmp),
                session=session,
            )

            # Before any settlement+8BD is reachable
            rec = client.features_as_of("AAPL", date(2023, 12, 31))
            self.assertIsNone(rec)

    def test_features_as_of_returns_none_for_missing_ticker(self):
        from alphalens.data.alt_data.polygon_short_interest import (
            PolygonShortInterestClient,
        )

        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            session.get.return_value = _response(200, {"status": "OK", "results": []})
            client = PolygonShortInterestClient(
                api_key="test-key",
                cache_dir=Path(tmp),
                session=session,
            )
            rec = client.features_as_of("BOGUSXYZ", date(2024, 6, 1))
            self.assertIsNone(rec)


if __name__ == "__main__":
    unittest.main()
