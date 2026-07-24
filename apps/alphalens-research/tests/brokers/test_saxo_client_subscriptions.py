"""Streaming subscription REST on the canonical SaxoClient (design memo
``saxo_streaming_design_2026_07_24.md`` — "Streaming client" + "Files touched").

The WebSocket reader NEVER makes its own HTTP calls: subscription create/delete
goes through :class:`SaxoClient` so it inherits the SIM-only rail, the Bearer
discipline, the shared 0.5s throttle (prevents self-inflicted 429), and stays
inside the one-canonical-client doctrine (``test_no_raw_saxo_http`` green).

CONFIRMED PROTOCOL (exploratory SIM probe, 2026-07-24):
- POST ``/port/v1/{positions,orders}/subscriptions`` body
  ``{ContextId, ReferenceId, Arguments:{ClientKey}}`` -> HTTP 201 + the
  ``{ContextId, Format, InactivityTimeout, ReferenceId, RefreshRate, Snapshot,
  State}`` envelope (``Snapshot.Data`` = current rows).
- DELETE ``/port/v1/{positions,orders}/subscriptions/{contextId}`` -> HTTP 202.
"""

from __future__ import annotations

import unittest
from typing import Any

from alphalens_pipeline.brokers.saxo.client import SIM_BASE_URL, SaxoClient


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        text: str = "",
    ):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.headers = headers or {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingSession:
    """Returns queued responses in order; repeats the last one when drained."""

    def __init__(self, responses: list[_FakeResponse] | None = None):
        self.responses = list(responses or [_FakeResponse()])
        self.calls: list[dict[str, Any]] = []

    def _next(self, call: dict[str, Any]) -> _FakeResponse:
        self.calls.append(call)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def post(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        return self._next(
            {
                "method": "post",
                "url": url,
                "headers": dict(headers or {}),
                "params": params,
                "json": json,
                "timeout": timeout,
            }
        )

    def delete(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        return self._next(
            {
                "method": "delete",
                "url": url,
                "headers": dict(headers or {}),
                "params": params,
                "timeout": timeout,
            }
        )


class _StubTokenProvider:
    def __init__(self, token: str = "tok-static"):
        self._token = token
        self.invalidations = 0

    def get_access_token(self) -> str:
        return self._token

    def invalidate(self) -> None:
        self.invalidations += 1


def _make_client(
    session: _RecordingSession,
) -> tuple[SaxoClient, list[float]]:
    sleeps: list[float] = []
    client = SaxoClient(
        _StubTokenProvider(),
        session=session,  # type: ignore[arg-type]
        sleep=sleeps.append,
    )
    return client, sleeps


_SUB_ENVELOPE = {
    "ContextId": "almgr-123-456",
    "Format": "application/json",
    "InactivityTimeout": 30,
    "ReferenceId": "pos",
    "RefreshRate": 1000,
    "Snapshot": {"Data": [{"PositionId": "P-1"}]},
    "State": "Active",
}


class TestCreatePositionsSubscription(unittest.TestCase):
    def test_posts_body_shape_and_endpoint(self):
        session = _RecordingSession([_FakeResponse(201, payload=_SUB_ENVELOPE)])
        client, _ = _make_client(session)

        status, parsed = client.create_positions_subscription(
            context_id="almgr-123-456", reference_id="pos", client_key="CK-1"
        )

        (call,) = session.calls
        self.assertEqual(call["method"], "post")
        self.assertEqual(call["url"], f"{SIM_BASE_URL}/port/v1/positions/subscriptions")
        self.assertEqual(
            call["json"],
            {
                "ContextId": "almgr-123-456",
                "ReferenceId": "pos",
                "Arguments": {"ClientKey": "CK-1"},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(parsed, _SUB_ENVELOPE)

    def test_bearer_header_present_token_not_in_url_or_body(self):
        session = _RecordingSession([_FakeResponse(201, payload=_SUB_ENVELOPE)])
        client, _ = _make_client(session)

        client.create_positions_subscription(
            context_id="ctx", reference_id="pos", client_key="CK-1"
        )

        (call,) = session.calls
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok-static")
        self.assertIn("x-request-id", call["headers"])
        self.assertNotIn("tok-static", call["url"])
        self.assertNotIn("tok-static", str(call["json"]))


class TestCreateOrdersSubscription(unittest.TestCase):
    def test_posts_body_shape_and_endpoint(self):
        envelope = dict(_SUB_ENVELOPE, ReferenceId="ord")
        session = _RecordingSession([_FakeResponse(201, payload=envelope)])
        client, _ = _make_client(session)

        status, parsed = client.create_orders_subscription(
            context_id="almgr-123-456", reference_id="ord", client_key="CK-1"
        )

        (call,) = session.calls
        self.assertEqual(call["url"], f"{SIM_BASE_URL}/port/v1/orders/subscriptions")
        self.assertEqual(
            call["json"],
            {
                "ContextId": "almgr-123-456",
                "ReferenceId": "ord",
                "Arguments": {"ClientKey": "CK-1"},
            },
        )
        self.assertEqual((status, parsed), (201, envelope))


class TestDeleteAllSubscriptions(unittest.TestCase):
    def test_deletes_both_positions_and_orders_on_context_id(self):
        session = _RecordingSession([_FakeResponse(202, payload={})])
        client, _ = _make_client(session)

        results = client.delete_all_subscriptions("almgr-123-456")

        urls = [call["url"] for call in session.calls]
        self.assertEqual(
            urls,
            [
                f"{SIM_BASE_URL}/port/v1/positions/subscriptions/almgr-123-456",
                f"{SIM_BASE_URL}/port/v1/orders/subscriptions/almgr-123-456",
            ],
        )
        for call in session.calls:
            self.assertEqual(call["method"], "delete")
        self.assertEqual([status for status, _ in results], [202, 202])

    def test_delete_goes_through_shared_throttle(self):
        session = _RecordingSession([_FakeResponse(202, payload={})])
        client, sleeps = _make_client(session)

        client.delete_all_subscriptions("ctx")

        # Two immediate DELETEs -> exactly one throttle sleep between them.
        throttle_sleeps = [s for s in sleeps if 0 < s <= 0.5]
        self.assertEqual(len(throttle_sleeps), 1)


class TestSubscriptionRestRoutesThroughCanonicalClient(unittest.TestCase):
    """Positive control that subscription REST uses the canonical write
    transport (so ``test_no_raw_saxo_http`` cannot regress): the calls land on
    the injected session with the SIM base URL joined and the x-request-id
    idempotency header attached."""

    def test_create_uses_send_write_transport(self):
        session = _RecordingSession([_FakeResponse(201, payload=_SUB_ENVELOPE)])
        client, _ = _make_client(session)

        client.create_positions_subscription(
            context_id="ctx", reference_id="pos", client_key="CK-1"
        )

        (call,) = session.calls
        self.assertTrue(call["url"].startswith(SIM_BASE_URL))
        self.assertEqual(call["timeout"], client._timeout)

    def test_subscription_methods_exist_on_client(self):
        for name in (
            "create_positions_subscription",
            "create_orders_subscription",
            "delete_all_subscriptions",
        ):
            self.assertTrue(callable(getattr(SaxoClient, name, None)), name)


if __name__ == "__main__":
    unittest.main()
