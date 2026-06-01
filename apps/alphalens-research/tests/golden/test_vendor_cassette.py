"""Unit tests for the non-LLM VendorCassette primitive (Phase 3b)."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from tests.golden.vendor_cassette import (
    RecordingVendor,
    VendorCassette,
    VendorCassetteMissError,
    vendor_key,
)

_START = dt.datetime(2026, 5, 17, tzinfo=dt.UTC)
_END = dt.datetime(2026, 5, 24, tzinfo=dt.UTC)

_NEWS = [{"id": "n1", "tickers": ["RGTI"], "title": "Quantum news", "published_utc": "2026-05-20"}]
_SUBMISSIONS = {"cik": "0001838359", "filings": {"recent": {"form": ["10-K"]}}}
_TEXT = "<html><body>Item 1. Business ... quantum computing ...</body></html>"
_TICKERS = {"0": {"cik_str": 1838359, "ticker": "RGTI", "title": "Rigetti Computing"}}

_SECRET = "sk-or-v1-DEADBEEFsecretkey0000000000000000"


class _FakeRealClient:
    """Stands in for both PolygonClient and SecEdgarClient; holds a 'secret'."""

    def __init__(self) -> None:
        self._api_key = _SECRET
        self.calls = 0

    def get_news_range(
        self,
        *,
        start,
        end,
        ticker=None,
        order="desc",
        sort="published_utc",
        limit=1000,
        max_items=1000,
        max_pages=200,
    ):
        self.calls += 1
        return _NEWS

    def fetch_submissions(self, cik):
        self.calls += 1
        return _SUBMISSIONS

    def fetch_company_tickers(self):
        self.calls += 1
        return _TICKERS

    def get_text(self, url, *, encoding="utf-8"):
        self.calls += 1
        return _TEXT


class TestVendorCassetteRoundTrip(unittest.TestCase):
    def _record_all(self, d: Path) -> _FakeRealClient:
        real = _FakeRealClient()
        rec = RecordingVendor(real, d)
        rec.get_news_range(start=_START, end=_END, ticker=None)
        rec.fetch_submissions("0001838359")
        rec.fetch_company_tickers()
        rec.get_text("https://www.sec.gov/Archives/edgar/data/1838359/x.htm")
        return real

    def test_record_then_replay_identity(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._record_all(d)
            cas = VendorCassette(d)
            self.assertEqual(cas.get_news_range(start=_START, end=_END, ticker=None), _NEWS)
            self.assertEqual(cas.fetch_submissions("0001838359"), _SUBMISSIONS)
            self.assertEqual(cas.fetch_company_tickers(), _TICKERS)
            self.assertEqual(
                cas.get_text("https://www.sec.gov/Archives/edgar/data/1838359/x.htm"), _TEXT
            )
            self.assertEqual(len(cas), 4)

    def test_replay_does_not_call_real_client(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._record_all(d)
            cas = VendorCassette(d)
            # No real client involved at replay — purely cassette-backed.
            self.assertEqual(cas.fetch_submissions("0001838359"), _SUBMISSIONS)

    def test_pagination_knobs_do_not_change_key(self):
        # max_items / max_pages bound iteration, not the logical request — a
        # cassette recorded with the defaults must serve a call with different
        # knobs (recent_press never varies them, but the key must be robust).
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._record_all(d)
            cas = VendorCassette(d)
            self.assertEqual(
                cas.get_news_range(start=_START, end=_END, ticker=None, max_pages=5, max_items=10),
                _NEWS,
            )

    def test_miss_is_fail_loud(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._record_all(d)
            cas = VendorCassette(d)
            with self.assertRaises(VendorCassetteMissError):
                cas.fetch_submissions("9999999999")  # never recorded
            with self.assertRaises(VendorCassetteMissError):
                cas.get_news_range(start=_START, end=_END, ticker="OTHER")

    def test_miss_returns_none_when_not_fail_loud(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._record_all(d)
            cas = VendorCassette(d, fail_on_miss=False)
            self.assertIsNone(cas.fetch_submissions("9999999999"))

    def test_no_secrets_in_cassettes(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            real = self._record_all(d)
            self.assertEqual(real._api_key, _SECRET)  # the secret lives on the client
            for path in d.glob("*.json"):
                self.assertNotIn(_SECRET, path.read_text())
                self.assertNotIn("Authorization", path.read_text())

    def test_vendor_key_canonicalises_datetime_stably(self):
        # The key built from a tz-aware datetime is stable across calls.
        k1 = vendor_key(
            vendor="polygon",
            method="get_news_range",
            args={"start": _START.isoformat(), "ticker": None},
        )
        k2 = vendor_key(
            vendor="polygon",
            method="get_news_range",
            args={"start": _START.isoformat(), "ticker": None},
        )
        self.assertEqual(k1, k2)

    def test_cassette_record_carries_human_readable_args(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._record_all(d)
            blobs = [json.loads(p.read_text()) for p in d.glob("*.json")]
            news = next(b for b in blobs if b["method"] == "get_news_range")
            self.assertEqual(news["vendor"], "polygon")
            self.assertEqual(news["payload_kind"], "json")
            self.assertIn("start", news["args"])
            self.assertNotIn("max_pages", news["args"])  # pagination excluded
            text = next(b for b in blobs if b["method"] == "get_text")
            self.assertEqual(text["payload_kind"], "text")


if __name__ == "__main__":
    unittest.main()
