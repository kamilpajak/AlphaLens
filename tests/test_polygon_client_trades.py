"""TDD for PolygonClient.trades() — v3/trades endpoint with pagination."""

import unittest
from datetime import date
from unittest.mock import MagicMock


def _response(status: int, body: dict | None = None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body or {}
    resp.text = text or (str(body) if body else "")
    return resp


def _make_client():
    from alphalens.screeners.lean.polygon_client import PolygonClient

    session = MagicMock()
    sleep = MagicMock()
    client = PolygonClient(
        api_key="test", rate_limit_per_min=60, session=session, sleep=sleep
    )
    return client, session, sleep


class TestTradesEndpoint(unittest.TestCase):
    def test_single_page_returns_list(self):
        client, session, _ = _make_client()
        session.get.return_value = _response(
            200,
            {
                "results": [
                    {
                        "sip_timestamp": 1711972800000000000,
                        "price": 100.5,
                        "size": 200,
                        "conditions": [12, 37],
                        "exchange": 4,
                        "trf_id": 202,
                    },
                    {
                        "sip_timestamp": 1711972801000000000,
                        "price": 100.6,
                        "size": 100,
                        "conditions": [12],
                        "exchange": 11,
                        "trf_id": None,
                    },
                ]
            },
        )

        trades = client.trades("AAPL", date(2024, 4, 1))

        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0].ticker, "AAPL")
        self.assertAlmostEqual(trades[0].price, 100.5)
        self.assertEqual(trades[0].size, 200)
        self.assertEqual(trades[0].conditions, [12, 37])
        self.assertEqual(trades[0].exchange, 4)
        self.assertEqual(trades[0].trf_id, 202)
        self.assertIsNone(trades[1].trf_id)

    def test_pagination_follows_next_url(self):
        client, session, _ = _make_client()
        session.get.side_effect = [
            _response(
                200,
                {
                    "results": [
                        {
                            "sip_timestamp": 1,
                            "price": 1.0,
                            "size": 1,
                            "conditions": [12],
                            "exchange": 4,
                            "trf_id": 1,
                        }
                    ],
                    "next_url": "https://api.polygon.io/v3/trades/AAPL?cursor=abc",
                },
            ),
            _response(
                200,
                {
                    "results": [
                        {
                            "sip_timestamp": 2,
                            "price": 2.0,
                            "size": 2,
                            "conditions": [12],
                            "exchange": 4,
                            "trf_id": None,
                        }
                    ],
                },
            ),
        ]

        trades = client.trades("AAPL", date(2024, 4, 1))

        self.assertEqual(len(trades), 2)
        self.assertEqual(session.get.call_count, 2)

    def test_empty_day(self):
        client, session, _ = _make_client()
        session.get.return_value = _response(200, {"results": []})

        trades = client.trades("AAPL", date(2024, 4, 6))  # Saturday
        self.assertEqual(trades, [])

    def test_retries_on_429(self):
        client, session, sleep = _make_client()
        session.get.side_effect = [
            _response(429, text="rate limited"),
            _response(200, {"results": []}),
        ]

        client.trades("AAPL", date(2024, 4, 1))

        self.assertEqual(session.get.call_count, 2)
        sleep.assert_any_call(60)

    def test_timestamp_range_params_cover_full_day(self):
        client, session, _ = _make_client()
        session.get.return_value = _response(200, {"results": []})

        client.trades("AAPL", date(2024, 4, 1))

        _, kwargs = session.get.call_args
        params = kwargs["params"]
        # timestamp.gte and timestamp.lt expressed in nanoseconds since epoch.
        # 2024-04-01 UTC midnight.
        gte = int(params["timestamp.gte"])
        lt = int(params["timestamp.lt"])
        self.assertEqual(gte, 1711929600000000000)
        self.assertEqual(lt, 1712016000000000000)
        self.assertEqual(params["order"], "asc")

    def test_missing_optional_fields_default_to_none_or_empty(self):
        client, session, _ = _make_client()
        session.get.return_value = _response(
            200,
            {
                "results": [
                    {
                        "sip_timestamp": 1,
                        "price": 99.0,
                        "size": 50,
                    }
                ]
            },
        )

        trades = client.trades("AAPL", date(2024, 4, 1))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].conditions, [])
        self.assertIsNone(trades[0].trf_id)


if __name__ == "__main__":
    unittest.main()
