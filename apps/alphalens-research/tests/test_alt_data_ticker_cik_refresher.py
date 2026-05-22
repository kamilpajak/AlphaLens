import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import yaml

SAMPLE_SEC_PAYLOAD = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corporation"},
    "2": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA Corporation"},
}


class TestParseSecTickers(unittest.TestCase):
    def test_parses_ticker_cik_pairs(self):
        from alphalens_research.data.alt_data.ticker_cik_refresher import parse_sec_company_tickers

        result = parse_sec_company_tickers(SAMPLE_SEC_PAYLOAD)

        self.assertEqual(result["AAPL"], 320193)
        self.assertEqual(result["MSFT"], 789019)
        self.assertEqual(result["NVDA"], 1045810)

    def test_empty_payload_returns_empty(self):
        from alphalens_research.data.alt_data.ticker_cik_refresher import parse_sec_company_tickers

        self.assertEqual(parse_sec_company_tickers({}), {})

    def test_missing_cik_field_raises(self):
        from alphalens_research.data.alt_data.ticker_cik_refresher import parse_sec_company_tickers

        with self.assertRaises(ValueError):
            parse_sec_company_tickers({"0": {"ticker": "AAPL"}})

    def test_missing_ticker_field_raises(self):
        from alphalens_research.data.alt_data.ticker_cik_refresher import parse_sec_company_tickers

        with self.assertRaises(ValueError):
            parse_sec_company_tickers({"0": {"cik_str": 320193}})

    def test_upper_cases_tickers(self):
        from alphalens_research.data.alt_data.ticker_cik_refresher import parse_sec_company_tickers

        result = parse_sec_company_tickers({"0": {"cik_str": 1, "ticker": "aapl"}})

        self.assertEqual(result, {"AAPL": 1})

    def test_duplicate_ticker_last_wins(self):
        """Defensive — SEC shouldn't publish duplicates, but if they do we don't want to crash."""
        from alphalens_research.data.alt_data.ticker_cik_refresher import parse_sec_company_tickers

        payload = {
            "0": {"cik_str": 111, "ticker": "AAPL"},
            "1": {"cik_str": 222, "ticker": "AAPL"},
        }

        result = parse_sec_company_tickers(payload)

        self.assertEqual(result["AAPL"], 222)


class TestRefreshWritesYaml(unittest.TestCase):
    def test_writes_yaml_that_tickercikmap_can_load(self):
        from alphalens_research.data.alt_data.ticker_cik_map import TickerCikMap
        from alphalens_research.data.alt_data.ticker_cik_refresher import refresh_ticker_cik_map

        edgar = MagicMock()
        edgar.fetch_company_tickers.return_value = SAMPLE_SEC_PAYLOAD

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "map.yaml"

            count = refresh_ticker_cik_map(edgar, out)

            self.assertEqual(count, 3)
            self.assertTrue(out.exists())

            m = TickerCikMap.load(out)

        self.assertEqual(m.lookup("AAPL"), "0000320193")
        self.assertEqual(m.lookup("MSFT"), "0000789019")
        self.assertEqual(m.lookup("NVDA"), "0001045810")

    def test_refresh_on_empty_payload_writes_empty_map(self):
        from alphalens_research.data.alt_data.ticker_cik_refresher import refresh_ticker_cik_map

        edgar = MagicMock()
        edgar.fetch_company_tickers.return_value = {}

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "map.yaml"

            count = refresh_ticker_cik_map(edgar, out)

            self.assertEqual(count, 0)
            parsed = yaml.safe_load(out.read_text())
        self.assertEqual(parsed or {}, {})

    def test_refresh_creates_parent_dirs(self):
        from alphalens_research.data.alt_data.ticker_cik_refresher import refresh_ticker_cik_map

        edgar = MagicMock()
        edgar.fetch_company_tickers.return_value = SAMPLE_SEC_PAYLOAD

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "nested" / "deeper" / "map.yaml"

            refresh_ticker_cik_map(edgar, out)

            self.assertTrue(out.exists())


class TestSecEdgarClientCompanyTickers(unittest.TestCase):
    def test_fetches_from_correct_url(self):
        from alphalens_research.data.alt_data.sec_edgar_client import SecEdgarClient

        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = SAMPLE_SEC_PAYLOAD
        session.get.return_value = resp
        sleep = MagicMock()

        client = SecEdgarClient(
            user_agent="AlphaLens test@example.com",
            rate_limit_per_sec=10,
            session=session,
            sleep=sleep,
        )

        data = client.fetch_company_tickers()

        self.assertEqual(data, SAMPLE_SEC_PAYLOAD)
        url = session.get.call_args[0][0]
        self.assertIn("sec.gov/files/company_tickers.json", url)


if __name__ == "__main__":
    unittest.main()
