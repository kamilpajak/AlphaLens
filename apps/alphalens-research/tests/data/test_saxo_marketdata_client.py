from __future__ import annotations

import unittest

from alphalens_pipeline.data.alt_data.saxo_marketdata_client import (
    LIVE_API_BASE_URL,
    SaxoMarketDataClient,
)


class _Resp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class _Session:
    """Records calls so the test asserts on URL and body, not on transport."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self._responses.pop(0)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)

    def patch(self, url, **kw):
        return self._next("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self._next("DELETE", url, **kw)


class _Tokens:
    def access_token(self):
        return "tok"


def _client(session):
    return SaxoMarketDataClient(token_provider=_Tokens(), session=session)


class TestElevateSession(unittest.TestCase):
    def test_patches_trade_level_and_reports_success_on_202(self):
        s = _Session(_Resp(202))
        self.assertTrue(_client(s).elevate_session())
        method, url, kw = s.calls[0]
        self.assertEqual(method, "PATCH")
        self.assertEqual(url, f"{LIVE_API_BASE_URL}/root/v1/sessions/capabilities")
        self.assertEqual(kw["json"], {"TradeLevel": "FullTradingAndChat"})

    def test_reports_failure_without_raising(self):
        """A failed elevation must degrade to 'not elevated', never crash the
        daemon tick."""
        self.assertFalse(_client(_Session(_Resp(403))).elevate_session())


class TestResolveUic(unittest.TestCase):
    def test_picks_the_exact_symbol_match(self):
        payload = {
            "Data": [
                {"Symbol": "AAPLX:xnas", "Identifier": 999},
                {"Symbol": "AAPL:xnas", "Identifier": 211},
            ]
        }
        self.assertEqual(_client(_Session(_Resp(200, payload))).resolve_uic("AAPL"), 211)

    def test_returns_none_when_no_exact_match(self):
        payload = {"Data": [{"Symbol": "AAPLX:xnas", "Identifier": 999}]}
        self.assertIsNone(_client(_Session(_Resp(200, payload))).resolve_uic("AAPL"))


class TestPriceSubscription(unittest.TestCase):
    def test_create_accepts_201_and_sends_the_measured_body(self):
        snapshot = {"RefreshRate": 1000, "Snapshot": {"Data": []}}
        s = _Session(_Resp(201, snapshot))
        got = _client(s).create_price_subscription(
            context_id="ctx", reference_id="px", uics=[211, 1249]
        )
        self.assertEqual(got["RefreshRate"], 1000)
        method, url, kw = s.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{LIVE_API_BASE_URL}/trade/v1/infoprices/subscriptions")
        body = kw["json"]
        self.assertEqual(body["ContextId"], "ctx")
        self.assertEqual(body["ReferenceId"], "px")
        self.assertEqual(body["Format"], "application/json")
        self.assertEqual(body["Arguments"]["Uics"], "211,1249")
        self.assertEqual(body["Arguments"]["AssetType"], "Stock")

    def test_delete_is_quiet_on_404(self):
        """Deleting an already-gone subscription is not an error."""
        _client(_Session(_Resp(404))).delete_price_subscription("ctx", "px")


if __name__ == "__main__":
    unittest.main()
