"""Tests for iShares index-membership refresher (universes module)."""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from alphalens_research.data.universes.ishares_refresher import (
    ETF_URLS,
    refresh_ishares_snapshot,
)

_FAKE_IJH_CSV = """\
"iShares Core S&P Mid-Cap ETF"
"Fund Holdings as of May 02 2026"
""
"Ticker","Name","Sector","Asset Class","Weight (%)","Price","Shares","Market Value","Notional Value"
"AAA","Acme Anvil Corp","Industrials","Equity","0.5","100","1000","100000","100000"
"BBB","Beta Bonds Inc","Financial","Equity","0.3","50","2000","100000","100000"
"-","USD Cash","--","Cash","0.1","--","--","--","--"
"""


class TestRefreshIsharesSnapshot(unittest.TestCase):
    def test_writes_snapshot_with_as_of_source_tickers(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "ijh.yaml"
            count = refresh_ishares_snapshot(
                etf_symbol="IJH",
                output_path=output,
                as_of=date(2026, 5, 3),
                csv_text_fetcher=lambda url: _FAKE_IJH_CSV,
            )

            self.assertEqual(count, 2)
            self.assertTrue(output.exists())

            data = yaml.safe_load(output.read_text())
            self.assertEqual(data["as_of"], "2026-05-03")
            self.assertEqual(data["tickers"], ["AAA", "BBB"])
            self.assertIn("IJH", data["source"])

    def test_includes_optional_notes(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "ijh.yaml"
            refresh_ishares_snapshot(
                etf_symbol="IJH",
                output_path=output,
                as_of=date(2026, 5, 3),
                notes="survivorship caveat: ~150bps/y",
                csv_text_fetcher=lambda url: _FAKE_IJH_CSV,
            )
            data = yaml.safe_load(output.read_text())
            self.assertEqual(data["notes"], "survivorship caveat: ~150bps/y")

    def test_uses_correct_etf_url(self) -> None:
        seen_urls: list[str] = []

        def capture_url(url: str) -> str:
            seen_urls.append(url)
            return _FAKE_IJH_CSV

        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "ijr.yaml"
            refresh_ishares_snapshot(
                etf_symbol="IJR",
                output_path=output,
                as_of=date(2026, 5, 3),
                csv_text_fetcher=capture_url,
            )

            self.assertEqual(len(seen_urls), 1)
            self.assertEqual(seen_urls[0], ETF_URLS["IJR"])

    def test_unknown_etf_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "x.yaml"
            with self.assertRaises(ValueError):
                refresh_ishares_snapshot(
                    etf_symbol="UNKNOWN",
                    output_path=output,
                    as_of=date(2026, 5, 3),
                )

    def test_fetch_failure_uses_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fallback = tmp_path / "fallback.yaml"
            fallback.write_text(
                yaml.safe_dump(
                    {"as_of": "2024-01-01", "source": "fb", "tickers": ["X", "Y"]},
                    sort_keys=False,
                )
            )
            output = tmp_path / "out.yaml"

            def boom(url: str) -> str:
                raise RuntimeError("network unreachable")

            count = refresh_ishares_snapshot(
                etf_symbol="IJH",
                output_path=output,
                as_of=date(2026, 5, 3),
                csv_text_fetcher=boom,
                fallback_path=fallback,
            )

            self.assertEqual(count, 2)
            data = yaml.safe_load(output.read_text())
            self.assertEqual(data["tickers"], ["X", "Y"])

    def test_fetch_failure_without_fallback_propagates(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.yaml"

            def boom(url: str) -> str:
                raise RuntimeError("network unreachable")

            with self.assertRaises(RuntimeError):
                refresh_ishares_snapshot(
                    etf_symbol="IJH",
                    output_path=output,
                    as_of=date(2026, 5, 3),
                    csv_text_fetcher=boom,
                )

    def test_etf_urls_known_set(self) -> None:
        self.assertEqual(set(ETF_URLS), {"IJH", "IJR", "IVV"})


if __name__ == "__main__":
    unittest.main()
