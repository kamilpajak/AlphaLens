"""Tests for the scuttlebutt layer (#507 PR-7a) — qualitative-only Perplexity.

Scuttlebutt fetches web-grounded NARRATIVE context (competitive position,
customer/supplier concentration, management reputation) per candidate via the
canonical PerplexityClient. It is qual-only: the result is raw prose that feeds
the qualitative prompt as context — it is NEVER parsed for numbers, and the
Scuttlebutt dataclass carries no numeric field. Fail-soft: any client error or
empty response degrades to ``ok=False`` with empty text (never raises).

All tests are hermetic — the PerplexityClient is a MagicMock; no network call.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from alphalens_pipeline.buffett.scuttlebutt import (
    Scuttlebutt,
    build_scuttlebutt_query,
    fetch_scuttlebutt,
)
from alphalens_pipeline.literature_scanner.perplexity_client import PerplexityClient


def _stub_client(return_value: str = "Competitor X is gaining share.") -> MagicMock:
    client = MagicMock(spec=PerplexityClient)
    client.ask.return_value = return_value
    return client


class TestBuildScuttlebuttQuery(unittest.TestCase):
    def test_query_asks_qualitative_signal_only(self):
        q = build_scuttlebutt_query("ACME", "Acme Corp").lower()
        self.assertIn("acme", q)
        # The three scuttlebutt dimensions.
        self.assertTrue("competit" in q, "query must ask about competitive position")
        self.assertTrue(
            "supplier" in q or "customer" in q or "concentration" in q,
            "query must ask about customer/supplier concentration",
        )
        self.assertTrue(
            "management" in q or "reputation" in q,
            "query must ask about management reputation",
        )

    def test_query_discourages_precise_figures(self):
        # Doctrine: numbers come from authoritative sources, not Perplexity.
        q = build_scuttlebutt_query("ACME", None).lower()
        self.assertTrue(
            ("figure" in q or "number" in q) and ("avoid" in q or "not" in q or "no " in q),
            "query must steer Perplexity away from precise figures",
        )

    def test_query_handles_missing_company_name(self):
        q = build_scuttlebutt_query("ACME", None)
        self.assertIn("ACME", q)


class TestFetchScuttlebutt(unittest.TestCase):
    def test_routes_through_client_with_recency_and_context(self):
        client = _stub_client("Acme faces intensifying competition from larger rivals.")
        result = fetch_scuttlebutt("ACME", client=client, company_name="Acme Corp")
        self.assertIsInstance(result, Scuttlebutt)
        self.assertTrue(result.ok)
        self.assertIn("intensifying competition", result.text)
        self.assertEqual(result.ticker, "ACME")
        # The grounded-search knobs are passed.
        kwargs = client.ask.call_args.kwargs
        self.assertIn("search_recency_filter", kwargs)
        self.assertIn("search_context_size", kwargs)

    def test_fail_soft_on_client_exception(self):
        client = MagicMock(spec=PerplexityClient)
        client.ask.side_effect = RuntimeError("network down")
        result = fetch_scuttlebutt("ACME", client=client)
        self.assertFalse(result.ok)
        self.assertEqual(result.text, "")
        self.assertEqual(result.ticker, "ACME")

    def test_fail_soft_on_empty_response(self):
        client = _stub_client("   ")
        result = fetch_scuttlebutt("ACME", client=client)
        self.assertFalse(result.ok)
        self.assertEqual(result.text, "")


class TestScuttlebuttDataclass(unittest.TestCase):
    def test_only_narrative_fields_no_numbers(self):
        s = Scuttlebutt(ticker="ACME", text="prose", ok=True)
        # Doctrine guard: the dataclass exposes only ticker/text/ok — no numeric
        # field can leak a Perplexity figure into a quant path.
        self.assertEqual(set(s.__dataclass_fields__), {"ticker", "text", "ok"})
        types = {name: f.type for name, f in s.__dataclass_fields__.items()}
        self.assertNotIn("int", str(types).lower().replace("ticker", ""))
        self.assertNotIn("float", str(types).lower())


if __name__ == "__main__":
    unittest.main()
