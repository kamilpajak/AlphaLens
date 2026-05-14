"""Tests for the Alpha Vantage EARNINGS bulk-fetch client.

Covers cache-hit short-circuit, rate-limit retry-after-sleep, schema
validation, throttling, batch resumability, and per-ticker failure
isolation. Network-side `urlopen` is never hit — all tests inject a
fake fetcher callable.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


def _good_payload(ticker: str = "AAPL") -> dict:
    """Minimal valid AV EARNINGS response."""
    return {
        "symbol": ticker,
        "annualEarnings": [
            {"fiscalDateEnding": "2024-09-30", "reportedEPS": "6.08"},
        ],
        "quarterlyEarnings": [
            {
                "fiscalDateEnding": "2024-09-30",
                "reportedDate": "2024-10-31",
                "reportedEPS": "1.64",
                "estimatedEPS": "1.60",
                "surprise": "0.04",
                "surprisePercentage": "2.5",
                "reportTime": "post-market",
            },
            {
                "fiscalDateEnding": "2017-12-30",
                "reportedDate": "2018-02-01",
                "reportedEPS": "0.9725",
                "estimatedEPS": "0.965",
                "surprise": "0.0075",
                "surprisePercentage": "0.7772",
                "reportTime": "post-market",
            },
        ],
    }


class TestFetchEarningsCaching(unittest.TestCase):
    def test_writes_cache_when_missing(self):
        from alphalens.data.alt_data.av_earnings_client import fetch_earnings

        fetcher = MagicMock(return_value=_good_payload("AAPL"))

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            result = fetch_earnings("AAPL", cache, fetcher=fetcher)

            cache_file = cache / "earnings_AAPL.json"
            self.assertTrue(cache_file.exists())
            self.assertEqual(result["symbol"], "AAPL")
            # Round-trip check: on-disk JSON matches returned dict.
            with cache_file.open() as f:
                self.assertEqual(json.load(f), result)
            fetcher.assert_called_once_with("AAPL")

    def test_returns_cached_when_present_no_api_call(self):
        from alphalens.data.alt_data.av_earnings_client import fetch_earnings

        fetcher = MagicMock(return_value=_good_payload("AAPL"))

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            cache_file = cache / "earnings_AAPL.json"
            cache_file.write_text(json.dumps(_good_payload("AAPL")))

            result = fetch_earnings("AAPL", cache, fetcher=fetcher)

            self.assertEqual(result["symbol"], "AAPL")
            fetcher.assert_not_called()

    def test_ticker_uppercased_in_cache_path(self):
        """Cache filename must canonicalize to upper-case so 'aapl' and 'AAPL'
        don't double-fetch into different files.

        macOS APFS is case-insensitive so `Path.exists()` answers True for
        both spellings; inspect actual directory entry names to verify the
        canonical form was used.
        """
        from alphalens.data.alt_data.av_earnings_client import fetch_earnings

        fetcher = MagicMock(return_value=_good_payload("AAPL"))

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            fetch_earnings("aapl", cache, fetcher=fetcher)
            names = [p.name for p in cache.iterdir()]
            self.assertIn("earnings_AAPL.json", names)
            self.assertNotIn("earnings_aapl.json", names)


class TestRateLimitRetry(unittest.TestCase):
    def test_retries_once_after_rate_limit_sleep(self):
        from alphalens.data.alt_data.av_earnings_client import (
            AVRateLimitError,
            fetch_earnings,
        )

        # First call raises, second call succeeds.
        fetcher = MagicMock(side_effect=[AVRateLimitError("rate limit hit"), _good_payload("AAPL")])
        sleep_fn = MagicMock()

        with tempfile.TemporaryDirectory() as td:
            result = fetch_earnings(
                "AAPL",
                Path(td),
                fetcher=fetcher,
                rate_limit_sleep=60.0,
                sleep_fn=sleep_fn,
            )

        self.assertEqual(result["symbol"], "AAPL")
        self.assertEqual(fetcher.call_count, 2)
        sleep_fn.assert_called_once_with(60.0)

    def test_retry_exhausted_raises(self):
        from alphalens.data.alt_data.av_earnings_client import (
            AVRateLimitError,
            fetch_earnings,
        )

        fetcher = MagicMock(side_effect=AVRateLimitError("rate limit hit"))
        sleep_fn = MagicMock()

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(AVRateLimitError):
                fetch_earnings(
                    "AAPL",
                    Path(td),
                    fetcher=fetcher,
                    rate_limit_sleep=60.0,
                    sleep_fn=sleep_fn,
                )

        # 1 initial + 1 retry = 2 attempts before giving up.
        self.assertEqual(fetcher.call_count, 2)


class TestSchemaValidation(unittest.TestCase):
    def test_missing_quarterly_earnings_rejected(self):
        from alphalens.data.alt_data.av_earnings_client import (
            AVSchemaError,
            fetch_earnings,
        )

        fetcher = MagicMock(return_value={"symbol": "AAPL", "annualEarnings": []})

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            with self.assertRaises(AVSchemaError):
                fetch_earnings("AAPL", cache, fetcher=fetcher)
            # Invalid payload must NOT be cached (so a re-run can retry).
            self.assertFalse((cache / "earnings_AAPL.json").exists())

    def test_empty_quarterly_earnings_rejected(self):
        from alphalens.data.alt_data.av_earnings_client import (
            AVSchemaError,
            fetch_earnings,
        )

        fetcher = MagicMock(return_value={"symbol": "AAPL", "quarterlyEarnings": []})

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(AVSchemaError):
                fetch_earnings("AAPL", Path(td), fetcher=fetcher)

    def test_entry_missing_required_field_rejected(self):
        from alphalens.data.alt_data.av_earnings_client import (
            AVSchemaError,
            fetch_earnings,
        )

        bad = {
            "symbol": "AAPL",
            "quarterlyEarnings": [
                # Missing reportedEPS.
                {
                    "fiscalDateEnding": "2024-09-30",
                    "reportedDate": "2024-10-31",
                    "estimatedEPS": "1.60",
                    "reportTime": "post-market",
                }
            ],
        }
        fetcher = MagicMock(return_value=bad)

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(AVSchemaError):
                fetch_earnings("AAPL", Path(td), fetcher=fetcher)

    def test_entry_missing_reportTime_is_tolerated(self):
        """reportTime drives PEAD entry_offset (pre/post-market) but AV
        historical data pre-2010 legitimately lacks it. Rejecting the entire
        ticker payload because a single old quarter is missing reportTime
        would drop otherwise-valid IS-window data. Downstream PEAD scorer
        must default missing reportTime to post-market on a per-event basis."""
        from alphalens.data.alt_data.av_earnings_client import fetch_earnings

        legacy = {
            "symbol": "AAPL",
            "quarterlyEarnings": [
                # Modern entry: has reportTime.
                {
                    "fiscalDateEnding": "2024-09-30",
                    "reportedDate": "2024-10-31",
                    "reportedEPS": "1.64",
                    "estimatedEPS": "1.60",
                    "reportTime": "post-market",
                },
                # Legacy entry: missing reportTime (pre-2010 historical).
                {
                    "fiscalDateEnding": "2003-09-30",
                    "reportedDate": "2003-10-31",
                    "reportedEPS": "0.05",
                    "estimatedEPS": "0.04",
                },
            ],
        }
        fetcher = MagicMock(return_value=legacy)

        with tempfile.TemporaryDirectory() as td:
            result = fetch_earnings("AAPL", Path(td), fetcher=fetcher)
            self.assertEqual(result["symbol"], "AAPL")
            self.assertEqual(len(result["quarterlyEarnings"]), 2)

    def test_cached_invalid_payload_re_validates(self):
        """If a previously-written cache file is malformed (e.g. partial write
        before crash), reading it must surface the schema error rather than
        silently propagate the corruption."""
        from alphalens.data.alt_data.av_earnings_client import (
            AVSchemaError,
            fetch_earnings,
        )

        fetcher = MagicMock(return_value=_good_payload("AAPL"))

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            (cache / "earnings_AAPL.json").write_text('{"symbol": "AAPL"}')

            with self.assertRaises(AVSchemaError):
                fetch_earnings("AAPL", cache, fetcher=fetcher)


class TestBatchThrottling(unittest.TestCase):
    def test_batch_throttles_between_uncached_fetches(self):
        from alphalens.data.alt_data.av_earnings_client import fetch_earnings_batch

        fetcher = MagicMock(side_effect=lambda t: _good_payload(t))
        sleep_fn = MagicMock()

        with tempfile.TemporaryDirectory() as td:
            statuses = fetch_earnings_batch(
                ["AAPL", "MSFT", "GOOGL"],
                Path(td),
                fetcher=fetcher,
                throttle_seconds=1.5,
                sleep_fn=sleep_fn,
            )

        # Three real fetches, three throttle sleeps (post-call).
        self.assertEqual(fetcher.call_count, 3)
        # sleep_fn called once per fetch with throttle_seconds.
        sleep_args = [c.args[0] for c in sleep_fn.call_args_list]
        self.assertEqual(sleep_args, [1.5, 1.5, 1.5])
        self.assertEqual(statuses, {"AAPL": "fetched", "MSFT": "fetched", "GOOGL": "fetched"})

    def test_batch_resumable_skips_cached(self):
        from alphalens.data.alt_data.av_earnings_client import fetch_earnings_batch

        fetcher = MagicMock(side_effect=lambda t: _good_payload(t))
        sleep_fn = MagicMock()

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            # Pre-populate AAPL.
            (cache / "earnings_AAPL.json").write_text(json.dumps(_good_payload("AAPL")))

            statuses = fetch_earnings_batch(
                ["AAPL", "MSFT"],
                cache,
                fetcher=fetcher,
                throttle_seconds=1.5,
                sleep_fn=sleep_fn,
            )

        # AAPL cached — no fetch; MSFT fetched once.
        self.assertEqual(fetcher.call_count, 1)
        self.assertEqual(statuses, {"AAPL": "cached", "MSFT": "fetched"})
        # Throttle slept only for the one real fetch.
        self.assertEqual(sleep_fn.call_count, 1)

    def test_batch_continues_on_individual_schema_failure(self):
        """One bad ticker must not abort the rest of the batch."""
        from alphalens.data.alt_data.av_earnings_client import fetch_earnings_batch

        def fetcher(ticker: str) -> dict:
            if ticker == "BAD":
                return {"symbol": "BAD"}  # missing quarterlyEarnings
            return _good_payload(ticker)

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            statuses = fetch_earnings_batch(
                ["GOOD", "BAD", "ALSO_GOOD"],
                cache,
                fetcher=fetcher,
                throttle_seconds=0,
                sleep_fn=lambda s: None,
            )

            self.assertEqual(statuses["GOOD"], "fetched")
            self.assertEqual(statuses["BAD"], "failed")
            self.assertEqual(statuses["ALSO_GOOD"], "fetched")
            self.assertTrue((cache / "earnings_GOOD.json").exists())
            self.assertFalse((cache / "earnings_BAD.json").exists())
            self.assertTrue((cache / "earnings_ALSO_GOOD.json").exists())

    def test_batch_aborts_on_rate_limit_after_retry(self):
        """If rate-limit persists past retry on one ticker, abort the batch
        rather than keep hammering the API (preserves remaining quota)."""
        from alphalens.data.alt_data.av_earnings_client import (
            AVRateLimitError,
            fetch_earnings_batch,
        )

        def fetcher(ticker: str) -> dict:
            if ticker == "MSFT":
                raise AVRateLimitError("quota exhausted")
            return _good_payload(ticker)

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            with self.assertRaises(AVRateLimitError):
                fetch_earnings_batch(
                    ["AAPL", "MSFT", "GOOGL"],
                    cache,
                    fetcher=fetcher,
                    throttle_seconds=0,
                    rate_limit_sleep=0,
                    sleep_fn=lambda s: None,
                )

            # AAPL succeeded before MSFT exhausted; GOOGL never attempted.
            self.assertTrue((cache / "earnings_AAPL.json").exists())
            self.assertFalse((cache / "earnings_MSFT.json").exists())
            self.assertFalse((cache / "earnings_GOOGL.json").exists())

    def test_batch_continues_on_network_error(self):
        """A single transient URLError mid-batch must NOT abort the run —
        a 3-week overnight 510-ticker backfill cannot be derailed by a
        single Comcast blip. Status='failed', continue to next ticker."""
        import urllib.error

        from alphalens.data.alt_data.av_earnings_client import fetch_earnings_batch

        def fetcher(ticker: str) -> dict:
            if ticker == "FLAKY":
                raise urllib.error.URLError("Connection reset by peer")
            return _good_payload(ticker)

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            statuses = fetch_earnings_batch(
                ["GOOD", "FLAKY", "ALSO_GOOD"],
                cache,
                fetcher=fetcher,
                throttle_seconds=0,
                sleep_fn=lambda s: None,
            )

            self.assertEqual(statuses["GOOD"], "fetched")
            self.assertEqual(statuses["FLAKY"], "failed")
            self.assertEqual(statuses["ALSO_GOOD"], "fetched")
            self.assertTrue((cache / "earnings_GOOD.json").exists())
            self.assertTrue((cache / "earnings_ALSO_GOOD.json").exists())

    def test_batch_fails_fast_on_http_auth_error(self):
        """HTTP 401/403 indicates a permanent auth failure (e.g. revoked API
        key). The batch must abort rather than silently mark all 510 tickers
        as 'failed', which would consume an entire daily quota window."""
        import urllib.error

        from alphalens.data.alt_data.av_earnings_client import fetch_earnings_batch

        def fetcher(ticker: str) -> dict:
            if ticker == "MSFT":
                raise urllib.error.HTTPError(
                    url="https://www.alphavantage.co/query",
                    code=403,
                    msg="Forbidden",
                    hdrs={},  # type: ignore[arg-type]
                    fp=None,
                )
            return _good_payload(ticker)

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            with self.assertRaises(urllib.error.HTTPError):
                fetch_earnings_batch(
                    ["AAPL", "MSFT", "GOOGL"],
                    cache,
                    fetcher=fetcher,
                    throttle_seconds=0,
                    sleep_fn=lambda s: None,
                )

            # AAPL fetched before MSFT raised; GOOGL never reached.
            self.assertTrue((cache / "earnings_AAPL.json").exists())
            self.assertFalse((cache / "earnings_GOOGL.json").exists())


class TestAtomicCacheWrite(unittest.TestCase):
    def test_no_tmp_file_left_after_successful_write(self):
        """tmp+rename pattern must clean up the .tmp file on success so a
        directory listing of the cache shows only canonical earnings_*.json
        entries."""
        from alphalens.data.alt_data.av_earnings_client import fetch_earnings

        fetcher = MagicMock(return_value=_good_payload("AAPL"))

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            fetch_earnings("AAPL", cache, fetcher=fetcher)
            names = [p.name for p in cache.iterdir()]

            self.assertEqual(names, ["earnings_AAPL.json"])
            self.assertFalse(any(n.endswith(".tmp") for n in names))


class TestLoadEarnings(unittest.TestCase):
    def test_load_returns_none_when_missing(self):
        from alphalens.data.alt_data.av_earnings_client import load_earnings

        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(load_earnings("NOPE", Path(td)))

    def test_load_returns_parsed_dict_when_present(self):
        from alphalens.data.alt_data.av_earnings_client import load_earnings

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            payload = _good_payload("AAPL")
            (cache / "earnings_AAPL.json").write_text(json.dumps(payload))

            loaded = load_earnings("AAPL", cache)
            self.assertEqual(loaded, payload)


if __name__ == "__main__":
    unittest.main()
