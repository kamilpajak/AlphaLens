"""Tests for MVP #2 enhancements to SECEdgarSource:

- 8-K item extraction from title
- Form 4 primary_doc.xml fetch + merge
- CIKLoader integration
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

SAMPLE_ATOM_8K_WITH_ITEMS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>8-K - Current report - Item 4.02 Non-reliance on previously issued financial statements, Item 2.02 Results of Operations</title>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/0000320193-26-000020-index.htm"/>
    <updated>2026-04-17T12:30:00-04:00</updated>
    <category scheme="https://www.sec.gov/" label="form type" term="8-K"/>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000020</id>
  </entry>
</feed>
"""

SAMPLE_ATOM_FORM4 = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>4 - Statement of changes in beneficial ownership of securities</title>
    <link rel="alternate" type="text/html"
      href="https://www.sec.gov/Archives/edgar/data/320193/000032019326000030/0000320193-26-000030-index.htm"/>
    <updated>2026-04-17T09:00:00-04:00</updated>
    <category scheme="https://www.sec.gov/" label="form type" term="4"/>
    <id>urn:tag:sec.gov,2008:accession-number=0000320193-26-000030</id>
  </entry>
</feed>
"""

SAMPLE_FORM4_PRIMARY_DOC = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>2000</value></transactionShares>
        <transactionPricePerShare><value>175.00</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

SAMPLE_INDEX_JSON = """{
  "directory": {
    "name": "/Archives/edgar/data/320193/000032019326000030/",
    "item": [
      {"name": "FilingSummary.xml", "type": "xml", "size": 100, "last-modified": "x"},
      {"name": "primary_doc.xml", "type": "xml", "size": 500, "last-modified": "x"}
    ]
  }
}"""


def _make_sec_client(*, atom_text, form4_xml_text=None, index_json_text=None):
    """Mock SecEdgarClient whose get_text routes by URL substring.

    Mirrors how the real client serves a single 8-K/Form-4 sweep — atom feeds,
    index.json directory listings, and Archives/*.xml all land in the same
    transport but produce different bodies.
    """
    from alphalens.data.alt_data.sec_edgar_client import SecEdgarClient

    index_json_text = index_json_text or SAMPLE_INDEX_JSON

    def _route(url, **_kwargs):
        if "index.json" in url:
            return index_json_text
        if url.endswith(".xml") and "Archives" in url:
            return form4_xml_text or ""
        return atom_text

    client = MagicMock(spec=SecEdgarClient)
    client.get_text.side_effect = _route
    return client


class TestEdgarEnhanced(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "seen.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _source(
        self,
        config_overrides=None,
        tickers=None,
        ticker_to_cik=None,
        cik_loader=None,
        sec_client=None,
    ):
        from alphalens.watchdog.config import WATCHDOG_DEFAULTS
        from alphalens.watchdog.sources.edgar import SECEdgarSource
        from alphalens.watchdog.storage import SeenEventStore

        cfg = dict(WATCHDOG_DEFAULTS)
        if config_overrides:
            cfg.update(config_overrides)

        return SECEdgarSource(
            tickers=tickers or ["AAPL"],
            config=cfg,
            store=SeenEventStore(self.db),
            ticker_to_cik=ticker_to_cik or {"AAPL": "0000320193"},
            cik_loader=cik_loader,
            sec_client=sec_client,
        )

    def test_8k_items_extracted_from_title(self):
        sec = _make_sec_client(atom_text=SAMPLE_ATOM_8K_WITH_ITEMS)
        source = self._source(sec_client=sec)
        events = source.detect()

        self.assertEqual(len(events), 1)
        self.assertIn("items", events[0].raw_data)
        items = set(events[0].raw_data["items"])
        self.assertIn("4.02", items)
        self.assertIn("2.02", items)

    def test_form4_details_fetched_when_enabled(self):
        sec = _make_sec_client(
            atom_text=SAMPLE_ATOM_FORM4,
            form4_xml_text=SAMPLE_FORM4_PRIMARY_DOC,
        )
        source = self._source(
            config_overrides={"fetch_form4_details": True},
            sec_client=sec,
        )
        events = source.detect()

        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt.raw_data.get("insider_action"), "BUY")
        self.assertAlmostEqual(evt.raw_data.get("transaction_value_usd", 0), 2000 * 175.0)

    def test_form4_details_not_fetched_when_disabled(self):
        sec = _make_sec_client(atom_text=SAMPLE_ATOM_FORM4)
        source = self._source(config_overrides={"fetch_form4_details": False}, sec_client=sec)
        source.detect()

        # Only the Atom feed call, no index.json or Form 4 XML fetch
        urls = [call.args[0] for call in sec.get_text.call_args_list]
        self.assertFalse(
            any("index.json" in u or ("Archives" in u and u.endswith(".xml")) for u in urls)
        )

    def test_form4_fetch_failure_does_not_break_detect(self):
        from alphalens.data.alt_data.sec_edgar_client import SecEdgarClient, SecEdgarError

        def _route(url, **_kwargs):
            if "index.json" in url:
                raise SecEdgarError("index down")
            return SAMPLE_ATOM_FORM4

        sec = MagicMock(spec=SecEdgarClient)
        sec.get_text.side_effect = _route

        source = self._source(
            config_overrides={"fetch_form4_details": True},
            sec_client=sec,
        )
        events = source.detect()

        self.assertEqual(len(events), 1)
        # insider_action absent is fine — base event still emitted
        self.assertNotIn("insider_action", events[0].raw_data)

    def test_cik_loader_takes_precedence_over_dict(self):
        from alphalens.watchdog.sources.cik_loader import CIKLoader

        loader = MagicMock(spec=CIKLoader)
        loader.get_cik.return_value = "0000789019"

        sec = _make_sec_client(atom_text=SAMPLE_ATOM_FORM4)
        source = self._source(
            tickers=["AAPL"],
            ticker_to_cik={"AAPL": "WRONG_CIK"},
            cik_loader=loader,
            sec_client=sec,
        )
        source.detect()

        loader.get_cik.assert_called_with("AAPL")
        # The HTTP call should encode the loader's CIK into the atom URL.
        atom_url = sec.get_text.call_args_list[0].args[0]
        self.assertIn("CIK=0000789019", atom_url)


if __name__ == "__main__":
    unittest.main()
