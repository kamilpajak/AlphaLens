import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _response(status: int, body=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body or {}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        from requests import HTTPError

        resp.raise_for_status.side_effect = HTTPError(f"{status}", response=resp)
    return resp


_SAMPLE_DGS10 = {
    "observations": [
        {"date": "2020-01-02", "value": "1.88"},
        {"date": "2020-01-03", "value": "1.80"},
        {"date": "2020-01-06", "value": "1.81"},
        {"date": "2020-01-07", "value": "."},  # FRED sentinel for missing
        {"date": "2020-01-08", "value": "1.87"},
    ]
}


class TestFREDClientFetch(unittest.TestCase):
    def test_fetches_dgs10_timeseries(self):
        from alphalens_pipeline.data.macro.fred_client import FREDClient

        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            session.get.return_value = _response(200, _SAMPLE_DGS10)
            client = FREDClient(api_key="test-key", cache_dir=Path(tmp), session=session)

            series = client.fetch_series("DGS10")

        self.assertEqual(len(series), 4)  # "." row dropped
        self.assertAlmostEqual(series.iloc[0], 1.88, places=6)
        session.get.assert_called_once()
        call_url = session.get.call_args.args[0]
        self.assertIn("series_id=DGS10", call_url)
        self.assertIn("api_key=test-key", call_url)
        self.assertIn("file_type=json", call_url)

    def test_missing_api_key_raises(self):
        from alphalens_pipeline.data.macro.fred_client import FREDAuthError, FREDClient

        with self.assertRaises(FREDAuthError):
            FREDClient(api_key="", cache_dir=Path("/tmp"))
        with self.assertRaises(FREDAuthError):
            FREDClient(api_key=None, cache_dir=Path("/tmp"))  # type: ignore[arg-type]

    def test_caches_response_to_disk(self):
        from alphalens_pipeline.data.macro.fred_client import FREDClient

        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            session.get.return_value = _response(200, _SAMPLE_DGS10)
            client = FREDClient(api_key="k", cache_dir=Path(tmp), session=session)

            client.fetch_series("DGS10")
            second = client.fetch_series("DGS10")

            self.assertEqual(session.get.call_count, 1)
            self.assertEqual(len(second), 4)
            cache_files = list(Path(tmp).glob("*.parquet"))
            self.assertEqual(len(cache_files), 1)
            self.assertIn("DGS10", cache_files[0].name)

    def test_retries_on_5xx_then_succeeds(self):
        from alphalens_pipeline.data.macro.fred_client import FREDClient

        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            session.get.side_effect = [
                _response(503),
                _response(200, _SAMPLE_DGS10),
            ]
            sleeper = MagicMock()
            client = FREDClient(
                api_key="k",
                cache_dir=Path(tmp),
                session=session,
                sleep=sleeper,
                max_retries=3,
            )

            series = client.fetch_series("DGS10")

        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(len(series), 4)
        sleeper.assert_called_once()

    def test_gives_up_after_max_retries(self):
        from alphalens_pipeline.data.macro.fred_client import FREDClient, FREDError

        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            session.get.return_value = _response(503)
            client = FREDClient(
                api_key="k",
                cache_dir=Path(tmp),
                session=session,
                sleep=MagicMock(),
                max_retries=2,
            )

            with self.assertRaises(FREDError):
                client.fetch_series("DGS10")

        # initial + 2 retries = 3 attempts
        self.assertEqual(session.get.call_count, 3)

    def test_raises_on_4xx_without_retry(self):
        from alphalens_pipeline.data.macro.fred_client import FREDClient, FREDError

        with tempfile.TemporaryDirectory() as tmp:
            session = MagicMock()
            session.get.return_value = _response(400)
            client = FREDClient(
                api_key="bad",
                cache_dir=Path(tmp),
                session=session,
                sleep=MagicMock(),
            )

            with self.assertRaises(FREDError):
                client.fetch_series("DGS10")

        self.assertEqual(session.get.call_count, 1)  # no retry

    def test_from_env_reads_api_key(self):
        from alphalens_pipeline.data.macro.fred_client import FREDAuthError, FREDClient

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"FRED_API_KEY": "env-key"}, clear=False),
        ):
            client = FREDClient.from_env(cache_dir=Path(tmp))

        self.assertEqual(client._api_key, "env-key")

        env = {k: v for k, v in os.environ.items() if k != "FRED_API_KEY"}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, env, clear=True),
            self.assertRaises(FREDAuthError),
        ):
            FREDClient.from_env(cache_dir=Path(tmp))


class TestTheCacheMustReachTheDateItIsAskedFor(unittest.TestCase):
    """#1524: the cache had no expiry, so a series that stopped updating was served
    forever.

    Measured on the VPS 2026-09-24: `FRED_VIXCLS.parquet` was written 2026-07-06,
    its last observation was 2026-07-01, and `market_state` stamped that same VIX
    on 79 brief dates. The PIT truncation `vix[vix.index <= asof]` is a no-op when
    the series ENDS BEFORE asof, so nothing downstream could notice.

    TWO thresholds, not one, because the two jobs pull opposite ways. Refetching
    is nearly free (FRED allows 120 req/min, the job runs 3x/day) so its trigger
    is tight. FAILING blanks a dashboard, so its threshold is loose. A single
    bound has to be wrong for one of them.

    Dates are real XNYS sessions. 2026-09-24 is a Thursday; one session before it
    is 2026-09-23 and five sessions before it is 2026-09-17. 2026-09-07 is Labor
    Day, so the session before Tuesday 2026-09-08 is Friday 2026-09-04.
    """

    _THROUGH = dt.date(2026, 9, 24)

    @staticmethod
    def _payload(last_date: str) -> dict:
        """A short series ending on ``last_date``. Only the last date matters."""
        return {
            "observations": [
                {"date": "2026-06-01", "value": "16.0"},
                {"date": last_date, "value": "17.5"},
            ]
        }

    def _client(self, tmp, last_date: str):
        from alphalens_pipeline.data.macro.fred_client import FREDClient

        session = MagicMock()
        session.get.return_value = _response(200, self._payload(last_date))
        client = FREDClient(api_key="k", cache_dir=Path(tmp), session=session)
        client.fetch_series("VIXCLS")  # seed the cache
        session.get.reset_mock()
        return client, session

    def test_a_cache_that_stops_before_the_date_is_refetched(self):
        # The bug, as a test. The cached series ends in July; the caller needs
        # September.
        with tempfile.TemporaryDirectory() as tmp:
            client, session = self._client(tmp, "2026-07-01")
            session.get.return_value = _response(200, self._payload("2026-09-23"))
            series = client.fetch_series("VIXCLS", through=self._THROUGH)
            self.assertEqual(session.get.call_count, 1)
            self.assertEqual(series.index[-1].date(), dt.date(2026, 9, 23))

    def test_a_cache_that_reaches_the_date_costs_no_request(self):
        # Positive control for the case above: a fresh cache must NOT refetch, or
        # the test proves nothing about the staleness check.
        with tempfile.TemporaryDirectory() as tmp:
            client, session = self._client(tmp, "2026-09-23")
            client.fetch_series("VIXCLS", through=self._THROUGH)
            self.assertEqual(session.get.call_count, 0)

    def test_one_session_of_publication_lag_is_normal(self):
        # FRED publishes a session's VIX the NEXT business morning, and all three
        # pipeline slots run before that. Measured 2026-09-24 19:00 UTC: the
        # newest observation was 2026-09-22. A rule demanding the asof print
        # itself would refetch on every healthy run and then fail.
        with tempfile.TemporaryDirectory() as tmp:
            client, session = self._client(tmp, "2026-09-23")
            client.fetch_series("VIXCLS", through=self._THROUGH)
            self.assertEqual(session.get.call_count, 0)

    def test_a_weekend_asof_accepts_the_prior_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            client, session = self._client(tmp, "2026-09-24")
            client.fetch_series("VIXCLS", through=dt.date(2026, 9, 26))  # Saturday
            self.assertEqual(session.get.call_count, 0)

    def test_the_session_after_a_holiday_accepts_the_prior_session(self):
        # 2026-09-07 is Labor Day. A calendar-day rule would see a four-day hole
        # here and refetch; a session rule does not.
        with tempfile.TemporaryDirectory() as tmp:
            client, session = self._client(tmp, "2026-09-04")
            client.fetch_series("VIXCLS", through=dt.date(2026, 9, 8))
            self.assertEqual(session.get.call_count, 0)

    def test_a_refetch_that_is_still_far_short_raises(self):
        from alphalens_pipeline.data.macro.fred_client import FREDStaleError

        with tempfile.TemporaryDirectory() as tmp:
            client, session = self._client(tmp, "2026-07-01")
            session.get.return_value = _response(200, self._payload("2026-07-01"))
            with self.assertRaises(FREDStaleError) as caught:
                client.fetch_series("VIXCLS", through=self._THROUGH)
            message = str(caught.exception)
            self.assertIn("VIXCLS", message)
            self.assertIn("2026-07-01", message)
            self.assertIn("2026-09-24", message)

    def test_the_refetch_trigger_and_the_failure_threshold_are_different(self):
        # The case that a single bound cannot express: the series is stale enough
        # to refetch (older than one session back) but not stale enough to fail
        # (newer than five sessions back, 2026-09-17). FRED itself is behind; a
        # display-only banner must not blank for that.
        with tempfile.TemporaryDirectory() as tmp:
            client, session = self._client(tmp, "2026-07-01")
            session.get.return_value = _response(200, self._payload("2026-09-18"))
            series = client.fetch_series("VIXCLS", through=self._THROUGH)
            self.assertEqual(session.get.call_count, 1)  # refetched
            self.assertEqual(series.index[-1].date(), dt.date(2026, 9, 18))  # did not raise

    def test_the_default_still_serves_a_frozen_cache(self):
        # `through=None` is today's behaviour byte-for-byte, and it is what the
        # research script relies on for a reproducible frozen series.
        with tempfile.TemporaryDirectory() as tmp:
            client, session = self._client(tmp, "2026-07-01")
            series = client.fetch_series("VIXCLS")
            self.assertEqual(session.get.call_count, 0)
            self.assertEqual(series.index[-1].date(), dt.date(2026, 7, 1))

    def test_an_out_of_order_response_is_not_mistaken_for_a_stale_one(self):
        # Found by probing, not by reading. Every freshness check here, and
        # `refresh_vix_cache`'s "last non-null observation", reads `.iloc[-1]` —
        # the last ROW, not the newest DATE. FRED returns ascending today, so
        # this is latent; trusting a latent property of an upstream feed is
        # exactly what cost three months in #1524. The parser now sorts, so the
        # assumption is true by construction.
        with tempfile.TemporaryDirectory() as tmp:
            from alphalens_pipeline.data.macro.fred_client import FREDClient

            session = MagicMock()
            session.get.return_value = _response(
                200,
                {
                    "observations": [
                        {"date": "2026-09-23", "value": "17.0"},
                        {"date": "2026-01-05", "value": "12.0"},
                    ]
                },
            )
            client = FREDClient(api_key="k", cache_dir=Path(tmp), session=session)
            series = client.fetch_series("VIXCLS", through=self._THROUGH)

        self.assertEqual(series.index[-1].date(), dt.date(2026, 9, 23))
        self.assertEqual(list(series.index), sorted(series.index))

    def test_the_sort_control_can_refute(self):
        # Without the sort the series would end on the older date, so the
        # assertion above could have failed.
        raw = [dt.date(2026, 9, 23), dt.date(2026, 1, 5)]
        self.assertNotEqual(raw[-1], sorted(raw)[-1])

    def test_the_cache_file_stays_group_and_world_readable(self):
        # `mkstemp` creates 0600 and `os.replace` preserves it, so the atomic
        # write silently tightened a file that used to be created at the umask
        # default. Same reason `observability/textfile.py` chmods its output.
        import stat

        with tempfile.TemporaryDirectory() as tmp:
            client, session = self._client(tmp, "2026-07-01")
            session.get.return_value = _response(200, self._payload("2026-09-23"))
            client.fetch_series("VIXCLS", through=self._THROUGH)
            mode = stat.S_IMODE((Path(tmp) / "FRED_VIXCLS.parquet").stat().st_mode)

        self.assertEqual(mode, 0o644, f"cache written {oct(mode)}, expected 0o644")

    def test_a_successful_write_leaves_no_temporary_file(self):
        # The parquet is now overwritten by a live pipeline while other processes
        # read it, and parquet keeps its metadata at the END of the file, so a
        # torn write is unreadable rather than short.
        with tempfile.TemporaryDirectory() as tmp:
            client, session = self._client(tmp, "2026-07-01")
            session.get.return_value = _response(200, self._payload("2026-09-23"))
            client.fetch_series("VIXCLS", through=self._THROUGH)
            leftovers = [p.name for p in Path(tmp).iterdir() if not p.name.endswith(".parquet")]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
