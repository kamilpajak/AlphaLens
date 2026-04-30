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
        from alphalens.data.macro.fred_client import FREDClient

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
        from alphalens.data.macro.fred_client import FREDAuthError, FREDClient

        with self.assertRaises(FREDAuthError):
            FREDClient(api_key="", cache_dir=Path("/tmp"))
        with self.assertRaises(FREDAuthError):
            FREDClient(api_key=None, cache_dir=Path("/tmp"))  # type: ignore[arg-type]

    def test_caches_response_to_disk(self):
        from alphalens.data.macro.fred_client import FREDClient

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
        from alphalens.data.macro.fred_client import FREDClient

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
        from alphalens.data.macro.fred_client import FREDClient, FREDError

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
        from alphalens.data.macro.fred_client import FREDClient, FREDError

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
        from alphalens.data.macro.fred_client import FREDAuthError, FREDClient

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


if __name__ == "__main__":
    unittest.main()
