import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>EDGAR Filings for Apple</title>
  <entry>
    <title>8-K - Current report (0000320193-26-000010)</title>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/320193/000032019326000010/0000320193-26-000010-index.htm"/>
    <updated>2026-04-17T12:30:00-04:00</updated>
    <category scheme="https://www.sec.gov/" label="form type" term="8-K"/>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000010</id>
  </entry>
  <entry>
    <title>4 - Statement of changes in beneficial ownership</title>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/320193/000032019326000009/0000320193-26-000009-index.htm"/>
    <updated>2026-04-17T09:00:00-04:00</updated>
    <category scheme="https://www.sec.gov/" label="form type" term="4"/>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000009</id>
  </entry>
  <entry>
    <title>10-K - Annual report</title>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/320193/000032019326000008/0000320193-26-000008-index.htm"/>
    <updated>2026-04-15T16:00:00-04:00</updated>
    <category scheme="https://www.sec.gov/" label="form type" term="10-K"/>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000008</id>
  </entry>
</feed>
"""


def _make_response(text: str, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


def _make_source(tickers=None, config_overrides=None, store=None, ticker_to_cik=None):
    from tradingagents.watchdog.config import WATCHDOG_DEFAULTS
    from tradingagents.watchdog.sources.edgar import SECEdgarSource

    cfg = dict(WATCHDOG_DEFAULTS)
    cfg["user_agent"] = "AlphaLens Test test@example.com"
    if config_overrides:
        cfg.update(config_overrides)

    return SECEdgarSource(
        tickers=tickers or ["AAPL"],
        config=cfg,
        store=store,
        ticker_to_cik=ticker_to_cik or {"AAPL": "0000320193"},
    )


class TestSECEdgarSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "seen.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self):
        from tradingagents.watchdog.storage import SeenEventStore

        return SeenEventStore(self.db_path)

    @patch("tradingagents.watchdog.sources.edgar.requests.get")
    def test_user_agent_header_is_sent(self, mock_get):
        mock_get.return_value = _make_response(SAMPLE_ATOM)
        source = _make_source(store=self._store())
        source.detect()

        self.assertTrue(mock_get.called)
        _, kwargs = mock_get.call_args
        self.assertIn("headers", kwargs)
        self.assertEqual(kwargs["headers"]["User-Agent"], "AlphaLens Test test@example.com")

    def test_missing_user_agent_in_config_raises(self):
        from tradingagents.watchdog.config import WATCHDOG_DEFAULTS
        from tradingagents.watchdog.sources.edgar import SECEdgarSource

        cfg = dict(WATCHDOG_DEFAULTS)  # user_agent=None
        with self.assertRaises(ValueError):
            SECEdgarSource(
                tickers=["AAPL"],
                config=cfg,
                store=self._store(),
                ticker_to_cik={"AAPL": "0000320193"},
            )

    @patch("tradingagents.watchdog.sources.edgar.requests.get")
    def test_detect_parses_atom_entries_into_events(self, mock_get):
        from tradingagents.watchdog.types import FormType

        mock_get.return_value = _make_response(SAMPLE_ATOM)
        # filter=None → accept all forms for this test
        source = _make_source(
            store=self._store(),
            config_overrides={"form_filter": [FormType.FORM_8K, FormType.FORM_4]},
        )
        events = source.detect()

        self.assertEqual(len(events), 2)
        accs = {e.accession_number for e in events}
        self.assertIn("0000320193-26-000010", accs)
        self.assertIn("0000320193-26-000009", accs)
        evt_8k = next(e for e in events if e.form_type == FormType.FORM_8K)
        self.assertEqual(evt_8k.ticker, "AAPL")
        self.assertIn("sec.gov", evt_8k.url)
        self.assertIsNotNone(evt_8k.filed_at.tzinfo)

    @patch("tradingagents.watchdog.sources.edgar.requests.get")
    def test_detect_filters_by_form_type(self, mock_get):
        from tradingagents.watchdog.types import FormType

        mock_get.return_value = _make_response(SAMPLE_ATOM)
        source = _make_source(
            store=self._store(),
            config_overrides={"form_filter": [FormType.FORM_8K]},
        )
        events = source.detect()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].form_type, FormType.FORM_8K)

    @patch("tradingagents.watchdog.sources.edgar.requests.get")
    def test_detect_deduplicates_via_store(self, mock_get):
        from tradingagents.watchdog.types import FormType

        mock_get.return_value = _make_response(SAMPLE_ATOM)
        store = self._store()
        source = _make_source(
            store=store,
            config_overrides={"form_filter": [FormType.FORM_8K, FormType.FORM_4]},
        )
        first = source.detect()
        self.assertEqual(len(first), 2)

        # Second call with fresh mock should see 0 (already seen)
        second = source.detect()
        self.assertEqual(second, [])

    @patch("tradingagents.watchdog.sources.edgar.time.sleep")
    @patch("tradingagents.watchdog.sources.edgar.requests.get")
    def test_rate_limit_sleeps_between_ticker_requests(self, mock_get, mock_sleep):
        mock_get.return_value = _make_response(SAMPLE_ATOM)
        source = _make_source(
            tickers=["AAPL", "MSFT"],
            store=self._store(),
            ticker_to_cik={"AAPL": "0000320193", "MSFT": "0000789019"},
            config_overrides={"rate_limit_seconds": 0.15},
        )
        source.detect()

        self.assertTrue(mock_sleep.called)
        slept = [args[0] for args, _ in mock_sleep.call_args_list]
        self.assertTrue(all(s >= 0.15 for s in slept))

    @patch("tradingagents.watchdog.sources.edgar.requests.get")
    def test_detect_returns_empty_on_network_error(self, mock_get):
        import requests

        mock_get.side_effect = requests.ConnectionError("network down")
        source = _make_source(store=self._store())
        events = source.detect()

        self.assertEqual(events, [])

    @patch("tradingagents.watchdog.sources.edgar.requests.get")
    def test_detect_returns_empty_on_malformed_xml(self, mock_get):
        mock_get.return_value = _make_response("<not-xml<>>")
        source = _make_source(store=self._store())
        events = source.detect()

        self.assertEqual(events, [])

    @patch("tradingagents.watchdog.sources.edgar.requests.get")
    def test_skips_ticker_without_cik_mapping(self, mock_get):
        mock_get.return_value = _make_response(SAMPLE_ATOM)
        source = _make_source(
            tickers=["AAPL", "UNKNOWN_TICKER"],
            store=self._store(),
            ticker_to_cik={"AAPL": "0000320193"},
        )
        source.detect()

        self.assertEqual(mock_get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
