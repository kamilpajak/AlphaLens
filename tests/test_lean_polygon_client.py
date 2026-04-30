import unittest
from unittest.mock import MagicMock


def _response(status: int, body: dict | None = None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body or {}
    resp.text = text or (str(body) if body else "")
    return resp


class TestPolygonClientInit(unittest.TestCase):
    def test_rejects_empty_api_key(self):
        from alphalens.archive.screeners.lean.polygon_client import PolygonClient

        with self.assertRaises(ValueError):
            PolygonClient(api_key="")


class TestGroupedDaily(unittest.TestCase):
    def setUp(self):
        from alphalens.archive.screeners.lean.polygon_client import PolygonClient

        self.session = MagicMock()
        self.sleep = MagicMock()
        self.client = PolygonClient(
            api_key="test",
            rate_limit_per_min=60,  # effectively no throttle for tests
            session=self.session,
            sleep=self.sleep,
        )

    def test_parses_rows_into_grouped_bars(self):
        self.session.get.return_value = _response(
            200,
            {
                "results": [
                    {
                        "T": "AAPL",
                        "o": 100.0,
                        "h": 102.0,
                        "l": 99.0,
                        "c": 101.0,
                        "v": 12345,
                        "t": 1700000000000,
                    },
                    {
                        "T": "MSFT",
                        "o": 200.0,
                        "h": 205.0,
                        "l": 199.0,
                        "c": 204.0,
                        "v": 6789,
                        "t": 1700000000000,
                    },
                ]
            },
        )

        bars = self.client.grouped_daily("2026-04-01")

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].ticker, "AAPL")
        self.assertEqual(bars[0].close, 101.0)
        self.assertEqual(bars[0].volume, 12345)
        self.assertEqual(bars[1].ticker, "MSFT")

    def test_empty_results_when_market_closed(self):
        self.session.get.return_value = _response(200, {"results": []})

        self.assertEqual(self.client.grouped_daily("2026-04-04"), [])  # Saturday

    def test_passes_adjusted_flag(self):
        self.session.get.return_value = _response(200, {"results": []})

        self.client.grouped_daily("2026-04-01", adjusted=False)

        _, kwargs = self.session.get.call_args
        self.assertEqual(kwargs["params"]["adjusted"], "false")

    def test_appends_api_key_to_params(self):
        self.session.get.return_value = _response(200, {"results": []})

        self.client.grouped_daily("2026-04-01")

        _, kwargs = self.session.get.call_args
        self.assertEqual(kwargs["params"]["apiKey"], "test")

    def test_raises_on_4xx(self):
        from alphalens.archive.screeners.lean.polygon_client import PolygonError

        self.session.get.return_value = _response(403, text="Forbidden")

        with self.assertRaises(PolygonError):
            self.client.grouped_daily("2026-04-01")

    def test_retries_once_on_429(self):
        self.session.get.side_effect = [
            _response(429, text="rate limited"),
            _response(200, {"results": []}),
        ]

        self.client.grouped_daily("2026-04-01")

        self.assertEqual(self.session.get.call_count, 2)
        self.sleep.assert_any_call(60)  # backoff sleep after 429


class TestRateLimit(unittest.TestCase):
    def test_throttles_between_calls(self):
        from alphalens.archive.screeners.lean.polygon_client import PolygonClient

        session = MagicMock()
        session.get.return_value = _response(200, {"results": []})
        sleep = MagicMock()
        client = PolygonClient(
            api_key="test",
            rate_limit_per_min=5,  # 12 s interval
            session=session,
            sleep=sleep,
        )

        client.grouped_daily("2026-04-01")
        client.grouped_daily("2026-04-02")

        # Second call should trigger sleep (first call sets last_call_ts).
        self.assertTrue(sleep.called)


class TestTickerRange(unittest.TestCase):
    def setUp(self):
        from alphalens.archive.screeners.lean.polygon_client import PolygonClient

        self.session = MagicMock()
        self.client = PolygonClient(
            api_key="test",
            rate_limit_per_min=60,
            session=self.session,
            sleep=MagicMock(),
        )

    def test_parses_ticker_range_results(self):
        self.session.get.return_value = _response(
            200,
            {
                "results": [
                    {
                        "o": 100.0,
                        "h": 102.0,
                        "l": 99.0,
                        "c": 101.0,
                        "v": 1000,
                        "t": 1713484800000,
                    },
                    {
                        "o": 101.0,
                        "h": 103.0,
                        "l": 100.0,
                        "c": 102.5,
                        "v": 1100,
                        "t": 1713571200000,
                    },
                ],
            },
        )

        bars = self.client.ticker_range("SPY", "2026-04-17", "2026-04-18")

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].ticker, "SPY")
        self.assertAlmostEqual(bars[0].close, 101.0)

    def test_uses_asc_sort_and_adjusted_flag(self):
        self.session.get.return_value = _response(200, {"results": []})

        self.client.ticker_range("SPY", "2024-01-01", "2024-12-31", adjusted=False)

        _, kwargs = self.session.get.call_args
        self.assertEqual(kwargs["params"]["adjusted"], "false")
        self.assertEqual(kwargs["params"]["sort"], "asc")
        self.assertEqual(kwargs["params"]["limit"], 50000)

    def test_empty_range(self):
        self.session.get.return_value = _response(200, {"results": []})

        self.assertEqual(self.client.ticker_range("UNKNOWN", "2024-01-01", "2024-01-02"), [])


class TestPagination(unittest.TestCase):
    def setUp(self):
        from alphalens.archive.screeners.lean.polygon_client import PolygonClient

        self.session = MagicMock()
        self.client = PolygonClient(
            api_key="test",
            rate_limit_per_min=60,
            session=self.session,
            sleep=MagicMock(),
        )

    def test_splits_follows_next_url(self):
        self.session.get.side_effect = [
            _response(
                200,
                {
                    "results": [{"ticker": "AAPL", "execution_date": "2020-08-31"}],
                    "next_url": "https://api.polygon.io/v3/reference/splits?cursor=x",
                },
            ),
            _response(
                200,
                {
                    "results": [{"ticker": "AAPL", "execution_date": "2022-08-31"}],
                },
            ),
        ]

        results = list(self.client.splits(ticker="AAPL"))

        self.assertEqual(len(results), 2)
        self.assertEqual(self.session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
