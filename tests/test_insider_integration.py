"""Phase 2 P5 integration smoke tests — opt-in, hits real public APIs.

Enable with::

    ALPHALENS_INTEGRATION=1 .venv/bin/python -m unittest tests.test_insider_integration -v

These tests intentionally depend on external services (SEC EDGAR, iShares)
and will fail when the network is unavailable or endpoints drift. They
exist to catch contract breakage that unit tests (which mock HTTP) cannot.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

_ENABLED = os.environ.get("ALPHALENS_INTEGRATION") == "1"
_USER_AGENT = os.environ.get("SEC_EDGAR_USER_AGENT", "AlphaLens research@example.com")


@unittest.skipUnless(_ENABLED, "set ALPHALENS_INTEGRATION=1 to enable")
class TestSecCompanyTickersEndpoint(unittest.TestCase):
    def test_endpoint_returns_large_ticker_universe(self):
        from alphalens.data.alt_data.sec_edgar_client import SecEdgarClient

        client = SecEdgarClient(user_agent=_USER_AGENT)

        payload = client.fetch_company_tickers()

        self.assertGreater(len(payload), 5000, "SEC master list should exceed 5000 entries")
        # Spot-check a well-known filer.
        found_aapl = any(entry.get("ticker") == "AAPL" for entry in payload.values())
        self.assertTrue(found_aapl)


@unittest.skipUnless(_ENABLED, "set ALPHALENS_INTEGRATION=1 to enable")
class TestEdgarSubmissionsApple(unittest.TestCase):
    def test_apple_submissions_contain_form_4(self):
        from alphalens.data.alt_data.sec_edgar_client import SecEdgarClient

        client = SecEdgarClient(user_agent=_USER_AGENT)

        submissions = client.fetch_submissions("0000320193")  # Apple
        recent = submissions.get("filings", {}).get("recent", {})
        forms = recent.get("form") or []

        self.assertIn("4", forms, "AAPL should have Form 4 filings in recent history")


@unittest.skipUnless(_ENABLED, "set ALPHALENS_INTEGRATION=1 to enable")
class TestIsharesAjaxEndpoint(unittest.TestCase):
    def test_iwm_fetch_returns_large_equity_universe(self):
        from alphalens.data.alt_data.iwm_refresher import refresh_iwm_current

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "iwm.yaml"

            count = refresh_iwm_current(out)

        self.assertGreater(count, 1500, f"IWM should hold 1500+ equity tickers; got {count}")


@unittest.skipUnless(_ENABLED, "set ALPHALENS_INTEGRATION=1 to enable")
class TestFeaturesAsOfEndToEnd(unittest.TestCase):
    def test_apple_features_as_of_does_not_raise(self):
        """Full pipeline: real EDGAR → parser → filter → cluster detection.

        AAPL likely won't trigger a ≥3-officer cluster in 30 days (large
        filers rarely cluster) so ``features_as_of`` may return None — but
        the pipeline must complete without errors.
        """
        from alphalens.archive.screeners.insider.scorer import InsiderScorer
        from alphalens.data.alt_data.sec_edgar_client import SecEdgarClient
        from alphalens.data.alt_data.ticker_cik_map import TickerCikMap

        client = SecEdgarClient(user_agent=_USER_AGENT)

        with tempfile.TemporaryDirectory() as td:
            map_path = Path(td) / "map.yaml"
            map_path.write_text("AAPL: 320193\n")
            cik_map = TickerCikMap.load(map_path)

            scorer = InsiderScorer(
                edgar_client=client,
                ticker_cik_map=cik_map,
                cache_dir=Path(td) / "cache",
            )

            features = scorer.features_as_of("AAPL", date(2024, 6, 30))

        # Either None (no cluster) or a dict with expected keys — both are OK.
        if features is not None:
            self.assertIn("insider_count", features)
            self.assertIn("aggregate_dollar", features)
            self.assertIn("asof", features)


if __name__ == "__main__":
    unittest.main()
