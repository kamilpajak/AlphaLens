import datetime as dt
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


class TestLazyBuilders(unittest.TestCase):
    """Cover the lazy-init bodies for the CIK fallback tiers."""

    def setUp(self):
        tenk_grep._get_cik_loader.cache_clear()
        tenk_grep._get_yaml_snapshot.cache_clear()

    def tearDown(self):
        tenk_grep._get_cik_loader.cache_clear()
        tenk_grep._get_yaml_snapshot.cache_clear()

    def test_get_cik_loader_builds_and_loads(self):
        from alphalens.watchdog.sources import cik_loader as cl

        with patch.object(cl.CIKLoader, "load") as mock_load:
            loader = tenk_grep._get_cik_loader()
        mock_load.assert_called_once()
        self.assertIsInstance(loader, cl.CIKLoader)

    def test_get_cik_loader_swallows_load_exception(self):
        # Network errors during load() must not propagate — the loader
        # object still returns (with empty mapping) so _resolve_cik can
        # proceed to the next tier.
        from alphalens.watchdog.sources import cik_loader as cl

        with patch.object(cl.CIKLoader, "load", side_effect=RuntimeError("SEC down")):
            loader = tenk_grep._get_cik_loader()
        self.assertIsInstance(loader, cl.CIKLoader)
        self.assertIsNone(loader.get_cik("NVDA"))

    def test_get_yaml_snapshot_returns_none_when_path_missing(self):
        with patch.object(tenk_grep, "TICKER_CIK_YAML_PATH", Path("/nonexistent/path.yaml")):
            self.assertIsNone(tenk_grep._get_yaml_snapshot())

    def test_get_yaml_snapshot_loads_real_file(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("NVDA: 1045810\nAAPL: 320193\n")
            yaml_path = Path(f.name)
        try:
            with patch.object(tenk_grep, "TICKER_CIK_YAML_PATH", yaml_path):
                snap = tenk_grep._get_yaml_snapshot()
            self.assertIsNotNone(snap)
            self.assertEqual(snap.lookup("NVDA"), "0001045810")
        finally:
            yaml_path.unlink()

    def test_get_yaml_snapshot_swallows_load_exception(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("not: valid: yaml: content")
            yaml_path = Path(f.name)
        try:
            with patch.object(tenk_grep, "TICKER_CIK_YAML_PATH", yaml_path):
                # Malformed file — load raises inside TickerCikMap.load,
                # _get_yaml_snapshot returns None.
                self.assertIsNone(tenk_grep._get_yaml_snapshot())
        finally:
            yaml_path.unlink()


class TestFetchTenKReturnsNoneOnNoRecent10K(unittest.TestCase):
    def test_returns_none_when_submissions_has_no_10k(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(tenk_grep, "_resolve_cik", return_value="0001045810"),
                patch.object(
                    tenk_grep,
                    "_fetch_submissions_json",
                    return_value={
                        "filings": {
                            "recent": {
                                "form": ["10-Q", "8-K"],
                                "accessionNumber": ["a", "b"],
                                "filingDate": ["2025-01-01", "2025-02-01"],
                                "primaryDocument": ["x.htm", "y.htm"],
                            }
                        }
                    },
                ),
            ):
                self.assertIsNone(tenk_grep.fetch_10k_text(ticker="UNKN", cache_dir=cache_dir))


class TestPrimaryTierSwallowsError(unittest.TestCase):
    def test_load_ticker_to_cik_returns_empty_on_network_error(self):
        from alphalens.data.alt_data import sec_edgar_client as sec_mod

        tenk_grep._load_ticker_to_cik.cache_clear()
        sec_mod._reset_default_client_for_tests()
        try:
            client = sec_mod.get_default_sec_client()
            with patch.object(
                client, "fetch_company_tickers", side_effect=RuntimeError("SEC unreachable")
            ):
                self.assertEqual(tenk_grep._load_ticker_to_cik(), {})
        finally:
            tenk_grep._load_ticker_to_cik.cache_clear()
            sec_mod._reset_default_client_for_tests()


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

    def test_resolve_cik_falls_back_when_primary_tier_raises(self):
        # SEC live company_tickers.json fetch raising must NOT bubble — the
        # whole point of the fallback chain is to survive primary-tier
        # outages. Wrap in try/except returning {} so CIKLoader + YAML get
        # their turn. Standalone callers (CLI debug, direct tests of
        # _resolve_cik) must not crash.
        from alphalens.data.alt_data import sec_edgar_client as sec_mod
        from alphalens.watchdog.sources import cik_loader as cl

        fake_loader = cl.CIKLoader.__new__(cl.CIKLoader)
        fake_loader._mapping = {"NVDA": "0001045810"}

        # Clear @lru_cache before patching so the bad-network call is what
        # the lru actually executes.
        tenk_grep._load_ticker_to_cik.cache_clear()
        sec_mod._reset_default_client_for_tests()
        try:
            client = sec_mod.get_default_sec_client()
            with (
                patch.object(
                    client, "fetch_company_tickers", side_effect=RuntimeError("SEC unreachable")
                ),
                patch.object(tenk_grep, "_get_cik_loader", return_value=fake_loader),
            ):
                self.assertEqual(tenk_grep._resolve_cik("NVDA"), "0001045810")
        finally:
            tenk_grep._load_ticker_to_cik.cache_clear()
            sec_mod._reset_default_client_for_tests()

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


class TestFetch10kPITPath(unittest.TestCase):
    """``asof`` selects the latest cached 10-K whose ``{TICKER}_{filing_date}``
    suffix is ≤ asof. Symmetric to ETF PIT path. When asof is None or
    >= today, current 'always pick newest' behaviour preserved.
    """

    def _seed_cache(self, cache_dir: Path, ticker: str, filing_date: str, body: str) -> None:
        (cache_dir / f"{ticker.upper()}_{filing_date}.txt").write_text(body)

    def test_find_cached_picks_latest_on_or_before_asof(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self._seed_cache(cache_dir, "NVDA", "2024-02-21", "v2024 text")
            self._seed_cache(cache_dir, "NVDA", "2025-02-21", "v2025 text")
            self._seed_cache(cache_dir, "NVDA", "2026-02-21", "v2026 text")
            path = tenk_grep._find_cached("NVDA", cache_dir, asof=dt.date(2025, 6, 1))
            self.assertIsNotNone(path)
            self.assertEqual(path.name, "NVDA_2025-02-21.txt")

    def test_find_cached_returns_none_when_no_file_on_or_before_asof(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self._seed_cache(cache_dir, "NVDA", "2026-02-21", "v2026 text")
            self.assertIsNone(tenk_grep._find_cached("NVDA", cache_dir, asof=dt.date(2025, 1, 1)))

    def test_find_cached_asof_none_preserves_legacy_latest_pick(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self._seed_cache(cache_dir, "NVDA", "2024-02-21", "v2024")
            self._seed_cache(cache_dir, "NVDA", "2026-02-21", "v2026")
            path = tenk_grep._find_cached("NVDA", cache_dir, asof=None)
            self.assertEqual(path.name, "NVDA_2026-02-21.txt")

    def test_find_cached_handles_underscore_in_ticker(self):
        """Tickers with underscores (e.g. BRK_B share-class variants) must not
        shift the date slice. Regression for zen review LOW finding."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self._seed_cache(cache_dir, "BRK_B", "2025-02-21", "old")
            self._seed_cache(cache_dir, "BRK_B", "2026-02-21", "new")
            path = tenk_grep._find_cached("BRK_B", cache_dir, asof=dt.date(2025, 6, 1))
            self.assertIsNotNone(path)
            self.assertEqual(path.name, "BRK_B_2025-02-21.txt")

    def test_find_cached_skips_unparseable_filenames(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self._seed_cache(cache_dir, "NVDA", "2025-02-21", "good")
            (cache_dir / "NVDA_garbage.txt").write_text("bad date")
            path = tenk_grep._find_cached("NVDA", cache_dir, asof=dt.date(2026, 1, 1))
            self.assertEqual(path.name, "NVDA_2025-02-21.txt")

    def test_fetch_10k_text_does_not_fetch_when_historical_asof_uncached(self):
        # Past asof + cold cache → return None, do NOT touch the SEC fetchers.
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    tenk_grep,
                    "_resolve_cik",
                    side_effect=AssertionError("no cik resolve under historical asof"),
                ),
                patch.object(
                    tenk_grep,
                    "_fetch_submissions_json",
                    side_effect=AssertionError("no submissions fetch"),
                ),
                patch.object(
                    tenk_grep,
                    "_fetch_filing_html",
                    side_effect=AssertionError("no html fetch"),
                ),
            ):
                text = tenk_grep.fetch_10k_text(
                    ticker="NVDA", cache_dir=cache_dir, asof=dt.date(2024, 6, 1)
                )
        self.assertIsNone(text)

    def test_has_theme_keywords_in_10k_uses_pit_filing(self):
        # Older 10-K mentions quantum; newer doesn't. asof=mid → match older.
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            self._seed_cache(cache_dir, "NVDA", "2024-02-21", "quantum computing roadmap")
            self._seed_cache(cache_dir, "NVDA", "2026-02-21", "graphics products only")
            self.assertTrue(
                tenk_grep.has_theme_keywords_in_10k(
                    ticker="NVDA",
                    keywords=["quantum"],
                    cache_dir=cache_dir,
                    asof=dt.date(2025, 1, 1),
                )
            )
            self.assertFalse(
                tenk_grep.has_theme_keywords_in_10k(
                    ticker="NVDA",
                    keywords=["quantum"],
                    cache_dir=cache_dir,
                    asof=dt.date.today(),
                )
            )

    def test_fetch_10k_text_live_asof_still_primes_cold_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    tenk_grep, "_fetch_submissions_json", return_value=FIXTURE_FILING_INDEX
                ),
                patch.object(tenk_grep, "_fetch_filing_html", return_value=FIXTURE_10K_HTML),
                patch.object(tenk_grep, "_resolve_cik", return_value="0001045810"),
            ):
                text = tenk_grep.fetch_10k_text(
                    ticker="NVDA", cache_dir=cache_dir, asof=dt.date.today()
                )
        self.assertIn("CUDA", text)

    def test_fetch_10k_text_yesterday_asof_primes_cold_cache(self):
        # The daily systemd timer runs at 06:30 UTC with asof = today - 1 day.
        # The PIT guard must allow priming in that operational window;
        # otherwise the cache never warms and the 10-K gate is permanently
        # gates_unknown for every candidate (the 2026-05-22 audit bug).
        yesterday = dt.date.today() - dt.timedelta(days=1)
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    tenk_grep, "_fetch_submissions_json", return_value=FIXTURE_FILING_INDEX
                ),
                patch.object(tenk_grep, "_fetch_filing_html", return_value=FIXTURE_10K_HTML),
                patch.object(tenk_grep, "_resolve_cik", return_value="0001045810"),
            ):
                text = tenk_grep.fetch_10k_text(ticker="NVDA", cache_dir=cache_dir, asof=yesterday)
            self.assertIsNotNone(text)
            self.assertIn("CUDA", text)
            cached = list(cache_dir.glob("NVDA_*.txt"))
            self.assertEqual(len(cached), 1)

    def test_fetch_10k_text_caches_but_returns_none_when_filing_date_after_asof(self):
        # Edge case the relaxed guard creates: a 10-K filed TODAY shouldn't
        # bleed into yesterday's verification verdict. Cache it for future
        # runs, return None for the current asof.
        yesterday = dt.date.today() - dt.timedelta(days=1)
        today = dt.date.today()
        future_filing_index = {
            "filings": {
                "recent": {
                    "form": ["10-K"],
                    "accessionNumber": ["0001045810-99-999999"],
                    "filingDate": [today.isoformat()],
                    "primaryDocument": ["nvda-today.htm"],
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            with (
                patch.object(
                    tenk_grep, "_fetch_submissions_json", return_value=future_filing_index
                ),
                patch.object(tenk_grep, "_fetch_filing_html", return_value=FIXTURE_10K_HTML),
                patch.object(tenk_grep, "_resolve_cik", return_value="0001045810"),
            ):
                text = tenk_grep.fetch_10k_text(ticker="NVDA", cache_dir=cache_dir, asof=yesterday)
            self.assertIsNone(text)
            cached = list(cache_dir.glob(f"NVDA_{today.isoformat()}.txt"))
            self.assertEqual(len(cached), 1)


if __name__ == "__main__":
    unittest.main()
