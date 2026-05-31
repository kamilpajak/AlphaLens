"""Entity resolution against feed-tagged tickers + an optional alias table.

PR-1 MVP: source-of-truth for tickers is the feed-side tagging from
``news_ingest`` (Polygon, EDGAR, RSS, GDELT all already populate
``tickers_raw``). The resolver normalizes those + applies an optional
alias table for cases where the body mentions a company name the feed
missed (e.g. an EDGAR 8-K subject company that wasn't pre-tagged).

Heavier mention-extraction / NER lives in a follow-up — this MVP unblocks
PR-2 hybrid integration without forcing a model dependency.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.thematic.extraction.templates.entity_resolver import (
    EntityResolver,
)
from alphalens_pipeline.thematic.extraction.templates.spec import Article


def _article(tickers: list[str] | None = None, body: str = "", title: str = "") -> Article:
    return Article(
        id="x",
        source="polygon",
        title=title,
        body=body,
        url="https://example.com/x",
        published_at=dt.datetime(2026, 5, 30, tzinfo=dt.UTC),
        tickers_raw=tickers or [],
    )


def _company_tickers_file(rows: list[dict]) -> Path:
    # Mirrors the SEC company_tickers.json shape (dict-of-dicts keyed by
    # row-index strings) so the resolver can be wired against the real
    # file with no shim.
    data = {str(i): row for i, row in enumerate(rows)}
    path = Path(tempfile.mkdtemp()) / "company_tickers.json"
    path.write_text(json.dumps(data))
    return path


class TestResolveFromFeedTags(unittest.TestCase):
    def setUp(self):
        self.tickers_file = _company_tickers_file(
            [
                {"cik_str": 320193, "ticker": "AAPL", "title": "APPLE INC"},
                {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
            ]
        )
        self.resolver = EntityResolver(company_tickers_path=self.tickers_file)

    def test_resolves_known_tagged_ticker(self):
        article = _article(tickers=["NVDA"])
        resolved = self.resolver.resolve(article)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].ticker, "NVDA")
        self.assertEqual(resolved[0].name, "NVIDIA CORP")
        self.assertEqual(resolved[0].role, "company")

    def test_normalizes_case(self):
        article = _article(tickers=["nvda"])
        resolved = self.resolver.resolve(article)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].ticker, "NVDA")

    def test_deduplicates_same_ticker_tagged_twice(self):
        article = _article(tickers=["NVDA", "NVDA", "nvda"])
        resolved = self.resolver.resolve(article)
        self.assertEqual(len(resolved), 1)

    def test_unknown_ticker_still_resolves_with_fallback_name(self):
        # A feed-tagged ticker that isn't in company_tickers.json must NOT
        # silently drop — the feed is the source of truth for "this article
        # is about $X". The resolver returns it with name=ticker as a
        # reasonable fallback.
        article = _article(tickers=["NEWLY_IPO"])
        resolved = self.resolver.resolve(article)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].ticker, "NEWLY_IPO")
        self.assertEqual(resolved[0].name, "NEWLY_IPO")

    def test_empty_tickers_returns_empty(self):
        article = _article(tickers=[])
        resolved = self.resolver.resolve(article)
        self.assertEqual(resolved, [])


class TestAliasTable(unittest.TestCase):
    def setUp(self):
        self.tickers_file = _company_tickers_file(
            [{"cik_str": 320193, "ticker": "AAPL", "title": "APPLE INC"}]
        )
        # Alias table maps body-text variants to tickers. Authored manually,
        # not auto-generated — see design memo §2.1 "common gotchas".
        aliases = {"the iPhone maker": "AAPL", "Apple Inc.": "AAPL"}
        alias_path = Path(tempfile.mkdtemp()) / "aliases.json"
        alias_path.write_text(json.dumps(aliases))
        self.resolver = EntityResolver(
            company_tickers_path=self.tickers_file,
            alias_path=alias_path,
        )

    def test_alias_in_body_resolves_when_feed_did_not_tag(self):
        article = _article(body="The iPhone maker reported strong sales.", tickers=[])
        resolved = self.resolver.resolve(article)
        tickers = [r.ticker for r in resolved]
        self.assertIn("AAPL", tickers)

    def test_alias_match_is_deduplicated_with_feed_tag(self):
        # Article tagged AAPL AND body mentions "Apple Inc." → still one
        # resolved entity for AAPL, not two.
        article = _article(body="Apple Inc. reported strong sales.", tickers=["AAPL"])
        resolved = self.resolver.resolve(article)
        tickers = [r.ticker for r in resolved]
        self.assertEqual(tickers.count("AAPL"), 1)


class TestPathDefaults(unittest.TestCase):
    def test_resolver_works_without_tickers_file(self):
        # If company_tickers.json is absent the resolver must still return
        # feed-tagged tickers (with fallback names). Otherwise a brand-new
        # operator workspace would never resolve anything until they run
        # the EDGAR detector once to populate the file.
        resolver = EntityResolver(company_tickers_path=Path("/nonexistent/x.json"))
        article = _article(tickers=["NVDA"])
        resolved = resolver.resolve(article)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].ticker, "NVDA")


if __name__ == "__main__":
    unittest.main()
