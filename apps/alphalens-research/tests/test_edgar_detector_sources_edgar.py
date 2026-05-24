import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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


def _make_sec_client(text=SAMPLE_ATOM):
    """Mock SecEdgarClient that returns ``text`` from get_text for every URL."""
    from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient

    client = MagicMock(spec=SecEdgarClient)
    client.get_text.return_value = text
    return client


def _make_source(
    tickers=None,
    config_overrides=None,
    store=None,
    ticker_to_cik=None,
    sec_client=None,
):
    from alphalens_pipeline.edgar_detector.config import DETECTOR_DEFAULTS
    from alphalens_pipeline.edgar_detector.sources.edgar import SECEdgarSource

    cfg = dict(DETECTOR_DEFAULTS)
    if config_overrides:
        cfg.update(config_overrides)

    return SECEdgarSource(
        tickers=tickers or ["AAPL"],
        config=cfg,
        store=store,
        ticker_to_cik=ticker_to_cik or {"AAPL": "0000320193"},
        sec_client=sec_client or _make_sec_client(),
    )


class TestSECEdgarSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "seen.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self):
        from alphalens_pipeline.edgar_detector.storage import SeenEventStore

        return SeenEventStore(self.db_path)

    def test_all_http_routes_through_sec_client(self):
        """SECEdgarSource must NEVER call requests.get directly — every URL
        goes through SecEdgarClient.get_text so the global 10 req/s throttle
        and User-Agent contract are honoured."""
        sec = _make_sec_client()
        source = _make_source(store=self._store(), sec_client=sec)
        source.detect()

        self.assertTrue(sec.get_text.called)
        url = sec.get_text.call_args[0][0]
        self.assertIn("cgi-bin/browse-edgar", url)
        self.assertIn("CIK=0000320193", url)

    def test_detect_parses_atom_entries_into_events(self):
        from alphalens_pipeline.edgar_detector.types import FormType

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

    def test_detect_filters_by_form_type(self):
        from alphalens_pipeline.edgar_detector.types import FormType

        source = _make_source(
            store=self._store(),
            config_overrides={"form_filter": [FormType.FORM_8K]},
        )
        events = source.detect()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].form_type, FormType.FORM_8K)

    def test_detect_deduplicates_via_store(self):
        from alphalens_pipeline.edgar_detector.types import FormType

        store = self._store()
        source = _make_source(
            store=store,
            config_overrides={"form_filter": [FormType.FORM_8K, FormType.FORM_4]},
        )
        first = source.detect()
        self.assertEqual(len(first), 2)

        # Second call should see 0 (already seen)
        second = source.detect()
        self.assertEqual(second, [])

    def test_no_direct_time_sleep_between_tickers(self):
        """The per-ticker time.sleep(rate_limit_seconds) is gone — the global
        SecEdgarClient throttle subsumes it. This guards against re-introducing
        a double-cap (per-ticker sleep on top of canonical 10 req/s)."""
        import alphalens_pipeline.edgar_detector.sources.edgar as edgar_mod

        self.assertFalse(hasattr(edgar_mod, "time"))

    def test_detect_returns_empty_on_sec_client_error(self):
        from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarError

        sec = _make_sec_client()
        sec.get_text.side_effect = SecEdgarError("network down")
        source = _make_source(store=self._store(), sec_client=sec)
        events = source.detect()

        self.assertEqual(events, [])

    def test_detect_returns_empty_on_malformed_xml(self):
        sec = _make_sec_client(text="<not-xml<>>")
        source = _make_source(store=self._store(), sec_client=sec)
        events = source.detect()

        self.assertEqual(events, [])

    def test_skips_ticker_without_cik_mapping(self):
        sec = _make_sec_client()
        source = _make_source(
            tickers=["AAPL", "UNKNOWN_TICKER"],
            store=self._store(),
            ticker_to_cik={"AAPL": "0000320193"},
            sec_client=sec,
        )
        source.detect()

        # Only one Atom fetch (for AAPL); UNKNOWN_TICKER skipped pre-fetch.
        self.assertEqual(sec.get_text.call_count, 1)


if __name__ == "__main__":
    unittest.main()
