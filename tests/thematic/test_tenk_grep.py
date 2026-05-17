import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alphalens.thematic.verification import tenk_grep

FIXTURE_10K_HTML = """
<html><head><title>NVDA 10-K</title></head>
<body>
<h1>Item 1. Business</h1>
<p>NVIDIA is a leader in <b>accelerated computing</b>, with platforms across
data center, gaming, automotive, and visualization markets.</p>
<p>Our CUDA platform powers a broad set of applications in artificial
intelligence, machine learning, and quantum computing simulation. We
collaborate with leading quantum hardware vendors via our CUDA-Q toolkit.</p>
<p>Our products are used in cybersecurity workloads through partnerships
with leading vendors.</p>
</body></html>
"""

FIXTURE_FILING_INDEX = {
    "filings": {
        "recent": {
            "form": ["10-K", "10-Q", "8-K"],
            "accessionNumber": [
                "0001045810-25-000001",
                "0001045810-25-000002",
                "0001045810-25-000003",
            ],
            "filingDate": ["2025-02-21", "2025-08-21", "2026-01-15"],
            "primaryDocument": ["nvda-20250131.htm", "nvda-q2.htm", "nvda-8k.htm"],
        }
    }
}


class TestExtractText(unittest.TestCase):
    def test_strips_html_to_plain_text(self):
        text = tenk_grep.extract_text(FIXTURE_10K_HTML)
        self.assertIn("accelerated computing", text)
        self.assertIn("CUDA platform", text)
        self.assertNotIn("<p>", text)
        self.assertNotIn("<h1>", text)


class TestGrepKeywords(unittest.TestCase):
    def test_finds_keywords_case_insensitive(self):
        text = "Our CUDA-Q toolkit accelerates Quantum Computing simulation."
        hits = tenk_grep.grep_keywords(text, ["quantum computing", "AI accelerator"])
        self.assertEqual(hits, ["quantum computing"])

    def test_multiple_hits_returns_all(self):
        text = "We work on cybersecurity, AI, and quantum computing."
        hits = tenk_grep.grep_keywords(
            text, ["cybersecurity", "AI", "quantum computing", "biotech"]
        )
        self.assertEqual(set(hits), {"cybersecurity", "AI", "quantum computing"})

    def test_no_hits_returns_empty_list(self):
        text = "We do banking."
        hits = tenk_grep.grep_keywords(text, ["quantum", "biotech"])
        self.assertEqual(hits, [])


class TestFindLatest10K(unittest.TestCase):
    def test_picks_most_recent_10k_from_filing_index(self):
        rec = tenk_grep.find_latest_10k(FIXTURE_FILING_INDEX)
        self.assertEqual(rec["accession"], "0001045810-25-000001")
        self.assertEqual(rec["filing_date"], "2025-02-21")
        self.assertEqual(rec["primary_doc"], "nvda-20250131.htm")

    def test_returns_none_when_no_10k(self):
        idx = {
            "filings": {
                "recent": {
                    "form": ["10-Q", "8-K"],
                    "accessionNumber": ["a", "b"],
                    "filingDate": ["2025-01-01", "2025-02-01"],
                    "primaryDocument": ["x.htm", "y.htm"],
                }
            }
        }
        self.assertIsNone(tenk_grep.find_latest_10k(idx))


class TestFetchAndCache(unittest.TestCase):
    def test_fetch_10k_text_caches_to_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    tenk_grep, "_fetch_submissions_json", return_value=FIXTURE_FILING_INDEX
                ),
                patch.object(tenk_grep, "_fetch_filing_html", return_value=FIXTURE_10K_HTML),
                patch.object(tenk_grep, "_resolve_cik", return_value="0001045810"),
            ):
                text = tenk_grep.fetch_10k_text(ticker="NVDA", cache_dir=cache_dir)
            self.assertIn("CUDA", text)
            cached = list(cache_dir.glob("NVDA_*.txt"))
            self.assertEqual(len(cached), 1)

    def test_fetch_10k_text_reuses_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    tenk_grep, "_fetch_submissions_json", return_value=FIXTURE_FILING_INDEX
                ),
                patch.object(tenk_grep, "_fetch_filing_html", return_value=FIXTURE_10K_HTML),
                patch.object(tenk_grep, "_resolve_cik", return_value="0001045810"),
            ):
                tenk_grep.fetch_10k_text(ticker="NVDA", cache_dir=cache_dir)
            with patch.object(tenk_grep, "_fetch_filing_html", side_effect=AssertionError("no")):
                text2 = tenk_grep.fetch_10k_text(ticker="NVDA", cache_dir=cache_dir)
            self.assertIn("CUDA", text2)


class TestVerificationGate(unittest.TestCase):
    def test_has_theme_keywords_in_10k_true_on_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            # Pre-seed cached text
            (cache_dir / "NVDA_2025-02-21.txt").write_text(
                "We work on quantum computing and AI accelerators."
            )
            self.assertTrue(
                tenk_grep.has_theme_keywords_in_10k(
                    ticker="NVDA",
                    keywords=["quantum computing"],
                    cache_dir=cache_dir,
                )
            )

    def test_has_theme_keywords_in_10k_false_on_miss(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            (cache_dir / "NVDA_2025-02-21.txt").write_text("We sell potatoes.")
            self.assertFalse(
                tenk_grep.has_theme_keywords_in_10k(
                    ticker="NVDA",
                    keywords=["quantum computing", "biotech"],
                    cache_dir=cache_dir,
                )
            )

    def test_has_theme_keywords_returns_none_on_fetch_failure(self):
        # Network errors / missing 10-K are NOT the same as "ran and said no".
        # Gate returns None (unknown) so the orchestrator can record the
        # distinction in `gates_unknown` instead of silently failing closed.
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with patch.object(tenk_grep, "fetch_10k_text", side_effect=RuntimeError("network")):
                self.assertIsNone(
                    tenk_grep.has_theme_keywords_in_10k(
                        ticker="UNKN",
                        keywords=["quantum"],
                        cache_dir=cache_dir,
                    )
                )

    def test_has_theme_keywords_returns_none_when_cik_unresolvable(self):
        # Foreign / recent-IPO tickers absent from every CIK source -> None.
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with patch.object(tenk_grep, "_resolve_cik", return_value=None):
                self.assertIsNone(
                    tenk_grep.has_theme_keywords_in_10k(
                        ticker="XYZ_FOREIGN",
                        keywords=["quantum"],
                        cache_dir=cache_dir,
                    )
                )


class TestCikFallbackChain(unittest.TestCase):
    def test_resolve_cik_returns_primary_hit(self):
        with patch.object(tenk_grep, "_load_ticker_to_cik", return_value={"NVDA": "0001045810"}):
            self.assertEqual(tenk_grep._resolve_cik("nvda"), "0001045810")

    def test_resolve_cik_falls_back_to_cik_loader(self):
        # Primary (SEC live company_tickers.json) misses; CIKLoader (TTL'd
        # cache reused from watchdog) has the ticker.
        from alphalens.watchdog.sources import cik_loader as cl

        fake_loader = cl.CIKLoader.__new__(cl.CIKLoader)
        fake_loader._mapping = {"FOREIGN": "0001234567"}
        with (
            patch.object(tenk_grep, "_load_ticker_to_cik", return_value={}),
            patch.object(tenk_grep, "_get_cik_loader", return_value=fake_loader),
        ):
            self.assertEqual(tenk_grep._resolve_cik("FOREIGN"), "0001234567")

    def test_resolve_cik_falls_back_to_yaml_snapshot(self):
        from alphalens.data.alt_data.ticker_cik_map import TickerCikMap

        snap = TickerCikMap(_by_ticker={"FOREIGN": "0009876543"})
        empty_loader = type("L", (), {"get_cik": lambda self, t: None})()
        with (
            patch.object(tenk_grep, "_load_ticker_to_cik", return_value={}),
            patch.object(tenk_grep, "_get_cik_loader", return_value=empty_loader),
            patch.object(tenk_grep, "_get_yaml_snapshot", return_value=snap),
        ):
            self.assertEqual(tenk_grep._resolve_cik("FOREIGN"), "0009876543")

    def test_resolve_cik_returns_none_on_full_chain_miss(self):
        empty_loader = type("L", (), {"get_cik": lambda self, t: None})()
        with (
            patch.object(tenk_grep, "_load_ticker_to_cik", return_value={}),
            patch.object(tenk_grep, "_get_cik_loader", return_value=empty_loader),
            patch.object(tenk_grep, "_get_yaml_snapshot", return_value=None),
        ):
            self.assertIsNone(tenk_grep._resolve_cik("XYZ"))


class TestExtractTextScriptStripping(unittest.TestCase):
    def test_strips_script_and_style_inner_content(self):
        html = """
        <html><head>
        <style>.s { color: quantum_red; }</style>
        <script>var x = "biotech_secret";</script>
        </head><body>
        <p>Real text about <b>quantum computing</b>.</p>
        </body></html>
        """
        text = tenk_grep.extract_text(html)
        self.assertNotIn("quantum_red", text)
        self.assertNotIn("biotech_secret", text)
        self.assertIn("quantum computing", text)

    def test_user_agent_env_override(self):
        import os

        os.environ["THEMATIC_USER_AGENT"] = "TestAgent override@test"
        try:
            self.assertEqual(tenk_grep._user_agent(), "TestAgent override@test")
        finally:
            del os.environ["THEMATIC_USER_AGENT"]
        self.assertEqual(tenk_grep._user_agent(), tenk_grep.DEFAULT_USER_AGENT)


if __name__ == "__main__":
    unittest.main()
