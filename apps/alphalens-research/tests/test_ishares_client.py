"""Tests for the canonical :class:`ISharesClient`.

A thin single-host HTTP wrapper for the iShares holdings-CSV AJAX endpoint. The
refreshers own the fallback resilience, so the client RAISES on failure rather
than swallowing — these tests pin that contract with an injected fake session.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import requests
from alphalens_pipeline.data.alt_data import ishares_client as ic


def _resp(status_code: int, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    if status_code >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status_code}")
    else:
        r.raise_for_status.return_value = None
    return r


class TestFetchHoldingsCsv(unittest.TestCase):
    def test_returns_csv_text_and_sends_user_agent(self):
        sess = MagicMock()
        sess.get.return_value = _resp(200, "Ticker,Name\nUPST,Upstart\n")
        text = ic.ISharesClient(session=sess).fetch_holdings_csv(
            "https://www.ishares.com/us/products/239710/x/1.ajax?fileType=csv"
        )
        self.assertIn("UPST", text)
        headers = sess.get.call_args.kwargs["headers"]
        self.assertIn("AlphaLens", headers["User-Agent"])

    def test_non_2xx_raises_so_caller_can_fall_back(self):
        sess = MagicMock()
        sess.get.return_value = _resp(503)
        with self.assertRaises(requests.HTTPError):
            ic.ISharesClient(session=sess).fetch_holdings_csv("https://www.ishares.com/x.ajax")

    def test_network_error_propagates(self):
        sess = MagicMock()
        sess.get.side_effect = requests.ConnectionError("dns down")
        with self.assertRaises(requests.ConnectionError):
            ic.ISharesClient(session=sess).fetch_holdings_csv("https://www.ishares.com/x.ajax")


class TestSingleton(unittest.TestCase):
    def setUp(self):
        ic._reset_default_client_for_tests()

    def tearDown(self):
        ic._reset_default_client_for_tests()

    def test_default_is_cached(self):
        self.assertIs(ic.get_default_ishares_client(), ic.get_default_ishares_client())

    def test_reset_clears_singleton(self):
        first = ic.get_default_ishares_client()
        ic._reset_default_client_for_tests()
        self.assertIsNot(first, ic.get_default_ishares_client())


if __name__ == "__main__":
    unittest.main()
