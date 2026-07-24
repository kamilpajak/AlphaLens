"""SIM-only structural rail for the streaming client (mirrors
``tests/brokers/test_saxo_sim_only_rail.py``).

Two independent locks so no single edit quietly opens a LIVE stream:

(a) the constructor refuses every LIVE streaming host marker AND any host that
    is not exactly :data:`SIM_STREAMING_BASE_URL` (equality guard, not just a
    blocklist — a typo'd or proxied LIVE URL is refused too);
(b) streaming REQUIRES the OAuth provider — under a :class:`StaticTokenProvider`
    the client refuses to ``start()`` (a fixed 24h token cannot be
    PUT-reauthorized in place), logs once, and the daemon stays poll-only.

The design memo's never-worse-than-poll guarantee means a refused start is
always safe: ``wake_event`` stays absent and the loop is byte-identical to
today's blocking poll.
"""

from __future__ import annotations

import unittest
from typing import Any

from alphalens_pipeline.brokers.saxo.errors import SaxoLiveEnvironmentBlockedError
from alphalens_pipeline.brokers.saxo.streaming import (
    _LIVE_STREAMING_MARKERS,
    SIM_STREAMING_BASE_URL,
    SaxoStreamingClient,
    StreamMessage,
)
from alphalens_pipeline.brokers.saxo.tokens import StaticTokenProvider


class _AnyTokenProvider:
    """OAuth-like provider (NOT a StaticTokenProvider) — passes the start gate."""

    def get_access_token(self) -> str:
        return "tok"

    def invalidate(self) -> None:
        pass


class _FakeSubscriber:
    def get_client_info(self) -> dict[str, Any]:
        return {"ClientKey": "CK-1"}

    def create_positions_subscription(self, **_: Any) -> tuple[int, dict[str, Any]]:
        return 201, {}

    def create_orders_subscription(self, **_: Any) -> tuple[int, dict[str, Any]]:
        return 201, {}

    def delete_all_subscriptions(self, _context_id: str) -> list[tuple[int, dict[str, Any]]]:
        return [(202, {}), (202, {})]


def _make(streaming_base_url: str, token_provider: Any) -> SaxoStreamingClient:
    return SaxoStreamingClient(
        token_provider,
        _FakeSubscriber(),
        context_id="almgr-1-2",
        on_trigger=lambda: None,
        on_heartbeat=lambda _ts: None,
        streaming_base_url=streaming_base_url,
    )


class TestStreamingSimRail(unittest.TestCase):
    def test_live_streaming_host_raises_live_blocked(self):
        for marker in _LIVE_STREAMING_MARKERS:
            live_url = f"wss://{marker}.saxobank.com/oapi/streaming/ws/connect"
            with self.subTest(streaming_base_url=live_url):
                with self.assertRaises(SaxoLiveEnvironmentBlockedError):
                    _make(live_url, _AnyTokenProvider())

    def test_any_non_sim_streaming_host_raises(self):
        with self.assertRaises(SaxoLiveEnvironmentBlockedError):
            _make("wss://example.com/oapi/streaming/ws/connect", _AnyTokenProvider())

    def test_sim_streaming_base_url_is_accepted(self):
        client = _make(SIM_STREAMING_BASE_URL, _AnyTokenProvider())
        self.assertIsInstance(client, SaxoStreamingClient)

    def test_static_token_provider_refuses_to_start_streaming(self):
        client = _make(SIM_STREAMING_BASE_URL, StaticTokenProvider("static-24h-token"))
        started = client.start()
        self.assertFalse(started, "StaticTokenProvider must not start a stream")
        self.assertFalse(client.is_started)

    def test_markers_positive_control(self):
        """The refusal scan passes vacuously if the marker tuple rots to empty —
        pin that it still names both LIVE streaming hosts and never matches SIM."""
        self.assertEqual(len(_LIVE_STREAMING_MARKERS), 2)
        self.assertTrue(any("live-streaming" in m for m in _LIVE_STREAMING_MARKERS))
        self.assertTrue(any("logonvalidation" in m for m in _LIVE_STREAMING_MARKERS))
        for marker in _LIVE_STREAMING_MARKERS:
            self.assertNotIn(marker, SIM_STREAMING_BASE_URL)

    def test_routing_still_works_under_sim_rail(self):
        """Positive control that a SIM-rail client is a functioning client."""
        triggered: list[bool] = []
        client = SaxoStreamingClient(
            _AnyTokenProvider(),
            _FakeSubscriber(),
            context_id="almgr-1-2",
            on_trigger=lambda: triggered.append(True),
            on_heartbeat=lambda _ts: None,
        )
        client._route_message(StreamMessage(1, "pos", b"{}"))
        self.assertEqual(len(triggered), 1)


if __name__ == "__main__":
    unittest.main()
