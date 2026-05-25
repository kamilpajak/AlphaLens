"""Tests for `scripts/pull_ivolatility_calendars.py`.

Cover the request/response state machine of iVolatility's two coverage
calendars (history-earnings + trading-calendar). Both endpoints expose
two response shapes:

1. **Inline** when ``recordsFound <= 500``: ``data`` is a list of dicts,
   ``urlForDetails`` is null.
2. **Async file mode** when ``recordsFound > 500``: ``data`` is empty,
   ``urlForDetails`` points at a JSON describing a gzipped CSV that
   must be downloaded separately from ``urlForDownload``.

The tests mock ``requests.get`` with controlled multi-call responses to
exercise both paths and assert the resulting DataFrame schema.
"""

from __future__ import annotations

import gzip
import json
import unittest
from unittest.mock import patch

import pandas as pd


def _make_mock_response(status_code: int, json_payload=None, raw_bytes=None, headers=None):
    """Build a minimal mock for ``requests.Response``."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.content = raw_bytes if raw_bytes is not None else b""
    if json_payload is not None:
        resp.json.return_value = json_payload
        resp.text = json.dumps(json_payload)
    else:
        resp.text = ""
    return resp


class TestInlineMode(unittest.TestCase):
    """Records-found ≤ 500 — data delivered in the first response."""

    def test_inline_response_returns_dataframe_with_expected_columns(self):
        from scripts.pull_ivolatility_calendars import fetch_calendar_endpoint

        payload = {
            "status": {
                "executionTime": 69,
                "recordsFound": 4,
                "code": "COMPLETE",
                "urlForDetails": None,
            },
            "query": {},
            "data": [
                {
                    "earning_date": "2024-02-01",
                    "stock_id": 799,
                    "symbol": "AAPL",
                    "market_cap": 2867431153489,
                    "time_of_day_code": "AMC",
                    "estimate": 2.11,
                    "reported_earning": 2.18,
                },
                {
                    "earning_date": "2024-05-02",
                    "stock_id": 799,
                    "symbol": "AAPL",
                    "market_cap": 3000000000000,
                    "time_of_day_code": "AMC",
                    "estimate": 1.50,
                    "reported_earning": 1.53,
                },
            ],
        }
        with patch("scripts.pull_ivolatility_calendars.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response(200, json_payload=payload)
            df = fetch_calendar_endpoint(
                endpoint="/equities/eod/history-earnings-calendar",
                params={"symbols": "AAPL", "from": "2024-01-01", "to": "2024-12-31"},
                api_key="fake-key",
            )

        self.assertEqual(len(df), 2)
        self.assertIn("symbol", df.columns)
        self.assertIn("earning_date", df.columns)
        self.assertEqual(df.iloc[0]["symbol"], "AAPL")

    def test_inline_zero_records_returns_empty_dataframe(self):
        from scripts.pull_ivolatility_calendars import fetch_calendar_endpoint

        payload = {
            "status": {"recordsFound": 0, "code": "COMPLETE", "urlForDetails": None},
            "data": [],
        }
        with patch("scripts.pull_ivolatility_calendars.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response(200, json_payload=payload)
            df = fetch_calendar_endpoint(
                endpoint="/equities/trading-calendar",
                params={"from": "2024-01-01", "to": "2024-01-02"},
                api_key="fake-key",
            )
        self.assertEqual(len(df), 0)


class TestAsyncMode(unittest.TestCase):
    """Records-found > 500 — three-call protocol: query → details → download."""

    def test_async_response_polls_details_and_downloads_gzip_csv(self):
        from scripts.pull_ivolatility_calendars import fetch_calendar_endpoint

        # 1) initial query response — empty data, urlForDetails set
        first_payload = {
            "status": {
                "recordsFound": 1984,
                "code": "COMPLETE",
                "urlForDetails": "https://restapi.ivolatility.com/data/info/abc-123",
            },
            "data": [],
        }
        # 2) details response — list with one job descriptor
        details_payload = [
            {
                "meta": {
                    "ruid": "abc-123",
                    "columns": (
                        "earning_date, stock_id, symbol, market_cap, "
                        "time_of_day_code, estimate, reported_earning"
                    ),
                    "recordsCount": 1984,
                    "status": "COMPLETE",
                },
                "data": [
                    {
                        "fileName": "abc-123.csv.gz",
                        "fileSize": 31545,
                        "urlForDownload": ("https://restapi.ivolatility.com/data/download/abc-123"),
                    }
                ],
            }
        ]
        # 3) download response — gzipped CSV bytes
        csv_text = (
            "earning_date,stock_id,symbol,market_cap,time_of_day_code,"
            "estimate,reported_earning\n"
            "2024-02-01,799,AAPL,2867431153489,AMC,2.11,2.18\n"
            "2024-05-02,799,AAPL,3000000000000,AMC,1.50,1.53\n"
        )
        gz_bytes = gzip.compress(csv_text.encode("utf-8"))

        responses = [
            _make_mock_response(200, json_payload=first_payload),
            _make_mock_response(200, json_payload=details_payload),
            _make_mock_response(200, raw_bytes=gz_bytes),
        ]

        with patch("scripts.pull_ivolatility_calendars.requests.get") as mock_get:
            mock_get.side_effect = responses
            df = fetch_calendar_endpoint(
                endpoint="/equities/eod/history-earnings-calendar",
                params={"stockGroup": "SP500_STOCKS", "from": "2024-01-01", "to": "2024-12-31"},
                api_key="fake-key",
            )

        self.assertEqual(len(df), 2)
        self.assertEqual(set(df.columns) >= {"symbol", "earning_date", "estimate"}, True)
        # Verify all three calls were made (query, details, download)
        self.assertEqual(mock_get.call_count, 3)

    def test_async_pending_then_complete_polls_until_ready(self):
        from scripts.pull_ivolatility_calendars import fetch_calendar_endpoint

        first = {
            "status": {
                "recordsFound": 1000,
                "code": "PENDING",
                "urlForDetails": "https://restapi.ivolatility.com/data/info/xyz",
            },
            "data": [],
        }
        # details: PENDING twice, then COMPLETE on third call
        pending_details = [
            {"meta": {"status": "PENDING"}, "data": []},
        ]
        complete_details = [
            {
                "meta": {"status": "COMPLETE", "columns": "a,b"},
                "data": [
                    {
                        "fileName": "xyz.csv.gz",
                        "urlForDownload": "https://restapi.ivolatility.com/data/download/xyz",
                    }
                ],
            }
        ]
        csv_bytes = gzip.compress(b"a,b\n1,2\n3,4\n")

        responses = [
            _make_mock_response(200, json_payload=first),
            _make_mock_response(200, json_payload=pending_details),
            _make_mock_response(200, json_payload=pending_details),
            _make_mock_response(200, json_payload=complete_details),
            _make_mock_response(200, raw_bytes=csv_bytes),
        ]

        with (
            patch("scripts.pull_ivolatility_calendars.requests.get") as mock_get,
            patch("scripts.pull_ivolatility_calendars.time.sleep"),  # speed up test
        ):
            mock_get.side_effect = responses
            df = fetch_calendar_endpoint(
                endpoint="/equities/trading-calendar",
                params={"from": "2024-01-01", "to": "2024-12-31"},
                api_key="fake-key",
                poll_interval=0,
            )

        self.assertEqual(len(df), 2)


class TestSaveToParquet(unittest.TestCase):
    def test_save_writes_parquet_to_path(self):
        import tempfile
        from pathlib import Path

        from scripts.pull_ivolatility_calendars import save_calendar_parquet

        df = pd.DataFrame(
            [
                {"symbol": "AAPL", "earning_date": "2024-02-01", "estimate": 2.11},
                {"symbol": "AAPL", "earning_date": "2024-05-02", "estimate": 1.50},
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "history_earnings.parquet"
            save_calendar_parquet(df, out)
            self.assertTrue(out.exists())
            roundtrip = pd.read_parquet(out)
            self.assertEqual(len(roundtrip), 2)
            self.assertEqual(roundtrip.iloc[0]["symbol"], "AAPL")


class TestErrorPaths(unittest.TestCase):
    def test_4xx_response_raises_helpful_error(self):
        from scripts.pull_ivolatility_calendars import (
            CalendarFetchError,
            fetch_calendar_endpoint,
        )

        with patch("scripts.pull_ivolatility_calendars.requests.get") as mock_get:
            mock_get.return_value = _make_mock_response(
                403, json_payload={"code": 403, "message": "Forbidden"}
            )
            with self.assertRaises(CalendarFetchError):
                fetch_calendar_endpoint(
                    endpoint="/equities/eod/upcoming-earnings-calendar",
                    params={"from": "2099-01-01"},
                    api_key="fake-key",
                )


if __name__ == "__main__":
    unittest.main()
