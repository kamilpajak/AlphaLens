from __future__ import annotations

import ast
import datetime as dt
import inspect
import unittest
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager.control_loop import (
    _default_live_exits_feed_factory,
    _saxo_live_prices_enabled,
)
from alphalens_pipeline.data.alt_data.saxo_price_stream import Quote

_LIVE_PRICES_ENV = {"ALPHALENS_SAXO_LIVE_PRICES": "1"}


class TestFeedSelection(unittest.TestCase):
    def test_flag_defaults_off(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(_saxo_live_prices_enabled())

    def test_flag_on_only_for_exactly_one(self):
        for value, expected in (("1", True), ("0", False), ("true", False), ("", False)):
            with mock.patch.dict("os.environ", {"ALPHALENS_SAXO_LIVE_PRICES": value}, clear=True):
                self.assertEqual(_saxo_live_prices_enabled(), expected, value)

    def test_factory_returns_a_vetoing_feed_when_the_flag_is_off(self):
        """Off means no prices at all - never a silent fall back to yfinance.

        The fixture is the tuple shape ``uic -> (ticker, exchange_mic)`` the
        factory actually receives in production - a bare ``uic -> str`` shape
        here (the pre-amendment plan's fixture) would never catch the ON path
        unpacking it wrong (fix round 2, finding 2)."""
        with mock.patch.dict("os.environ", {}, clear=True):
            feed = _default_live_exits_feed_factory({211: ("AAPL", "XNYS")}, scope="exits")
        self.assertIsNone(feed.latest(211))


class _FakeSharedStream:
    """Stand-in for the module-level SaxoPriceStream singleton -- exposes only
    the three methods _default_live_exits_feed_factory calls, no network."""

    def __init__(self, live_uic_map: dict[tuple[str, str], int]) -> None:
        self._live_uic_map = live_uic_map
        self.ensure_subscribed_calls: list[tuple[str, list[int]]] = []
        self.quotes: dict[int, Quote] = {}

    def live_uic_for(self, ticker: str, *, exchange_mic: str) -> int | None:
        return self._live_uic_map.get((ticker, exchange_mic))

    def ensure_subscribed(self, uics, *, scope: str) -> None:
        self.ensure_subscribed_calls.append((scope, sorted(uics)))

    def get(self, uic: int) -> Quote | None:
        return self.quotes.get(uic)


def _patched_shared_stream(fake_stream: _FakeSharedStream):
    return mock.patch(
        "alphalens_pipeline.data.alt_data.saxo_price_stream.get_shared_price_stream",
        return_value=fake_stream,
    )


class TestDefaultFactoryOnPathWiring(unittest.TestCase):
    """Fix round 2 (Task 7 review), finding 3: the ON path had zero hermetic
    coverage — nothing referenced live_uic_for or get_shared_price_stream
    outside their defining module. These hit the REAL
    _default_live_exits_feed_factory with a fake shared stream; no network,
    no real WebSocket, no live probe."""

    def test_ensure_subscribed_receives_only_the_resolved_live_uics(self):
        fake_stream = _FakeSharedStream({("AAPL", "XNYS"): 9001})
        with mock.patch.dict("os.environ", _LIVE_PRICES_ENV, clear=True):
            with _patched_shared_stream(fake_stream):
                _default_live_exits_feed_factory(
                    {211: ("AAPL", "XNYS"), 212: ("UNKNOWN", "ZZZZ")}, scope="exits"
                )
        # 212's (UNKNOWN, ZZZZ) never resolves -- it must NOT reach the
        # subscription request; only the resolved LIVE uic does.
        self.assertEqual(fake_stream.ensure_subscribed_calls, [("exits", [9001])])

    def test_the_callers_scope_reaches_ensure_subscribed_unchanged(self):
        """The scope names the caller's slice of the shared subscription
        (2026-08-18 churn fix): dropping it in transit would collapse every
        feed build back into ONE fighting scope."""
        fake_stream = _FakeSharedStream({("AAPL", "XNYS"): 9001})
        with mock.patch.dict("os.environ", _LIVE_PRICES_ENV, clear=True):
            with _patched_shared_stream(fake_stream):
                _default_live_exits_feed_factory({211: ("AAPL", "XNYS")}, scope="entry-watch")
        self.assertEqual(fake_stream.ensure_subscribed_calls, [("entry-watch", [9001])])

    def test_resolve_live_uic_is_keyed_on_the_callers_sim_uic_not_the_live_uic(self):
        fake_stream = _FakeSharedStream({("AAPL", "XNYS"): 9001})
        with mock.patch.dict("os.environ", _LIVE_PRICES_ENV, clear=True):
            with _patched_shared_stream(fake_stream):
                feed = _default_live_exits_feed_factory({211: ("AAPL", "XNYS")}, scope="exits")
        now = dt.datetime.now(dt.UTC)
        fake_stream.quotes[9001] = Quote(
            uic=9001, bid=100.0, ask=100.1, event_time=now, delayed_by_minutes=0, received_at=now
        )
        # 211 is the SIM uic the engine keys everything by; the stream itself
        # is keyed by the LIVE uic 9001. If resolve_live_uic were mistakenly
        # keyed by the LIVE uic, latest(211) would find nothing even though a
        # fresh quote exists at 9001.
        point = feed.latest(211)
        self.assertIsNotNone(point)
        self.assertEqual(point.uic, 211)
        # And the LIVE uic itself is never a valid caller key.
        self.assertIsNone(feed.latest(9001))


class TestSharedReaderSelection(unittest.TestCase):
    """#1172: with a reader socket configured, the daemon must read the ONE
    elevated session that process holds instead of opening its own. Saxo grants
    a single elevated session per LIVE login, so two in-process streams demote
    BOTH daemons to 15-minute quotes — silently."""

    _SOCKET_ENV = "ALPHALENS_SAXO_PRICE_READER_SOCKET"

    def test_the_socket_env_routes_the_feed_through_the_reader(self):
        remote = mock.MagicMock()
        remote.live_uic_for.return_value = 9001
        with (
            mock.patch.dict(
                "os.environ", {**_LIVE_PRICES_ENV, self._SOCKET_ENV: "/tmp/r.sock"}, clear=True
            ),
            mock.patch(
                "alphalens_pipeline.data.alt_data.price_reader_client.RemoteQuoteSource",
                return_value=remote,
            ) as remote_cls,
            mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_price_stream.get_shared_price_stream"
            ) as in_process,
        ):
            feed = _default_live_exits_feed_factory({211: ("AAPL", "XNYS")}, scope="exits")

        in_process.assert_not_called()
        self.assertEqual(str(remote_cls.call_args.args[0]), "/tmp/r.sock")
        remote.ensure_subscribed.assert_called_once()
        self.assertEqual(remote.ensure_subscribed.call_args.kwargs["scope"], "exits")
        self.assertIsNotNone(feed)

    def test_without_the_socket_the_daemon_keeps_its_own_stream(self):
        """Rollback lever: removing the drop-in restores today's behaviour
        exactly, with no code change."""
        with (
            mock.patch.dict("os.environ", _LIVE_PRICES_ENV, clear=True),
            mock.patch(
                "alphalens_pipeline.data.alt_data.price_reader_client.RemoteQuoteSource"
            ) as remote_cls,
            mock.patch(
                "alphalens_pipeline.data.alt_data.saxo_price_stream.get_shared_price_stream"
            ) as in_process,
        ):
            _default_live_exits_feed_factory({211: ("AAPL", "XNYS")}, scope="exits")

        remote_cls.assert_not_called()
        in_process.assert_called_once()

    def test_the_off_flag_still_wins_over_a_configured_socket(self):
        """``ALPHALENS_SAXO_LIVE_PRICES`` stays the master switch: a socket
        left in the environment must not resurrect prices on an instance whose
        feature is off."""
        with (
            mock.patch.dict("os.environ", {self._SOCKET_ENV: "/tmp/r.sock"}, clear=True),
            mock.patch(
                "alphalens_pipeline.data.alt_data.price_reader_client.RemoteQuoteSource"
            ) as remote_cls,
        ):
            feed = _default_live_exits_feed_factory({211: ("AAPL", "XNYS")}, scope="exits")

        remote_cls.assert_not_called()
        self.assertIsNone(feed.latest(211))

    def test_the_remote_source_is_reused_across_ticks(self):
        """The reader keys each client's touch-latch accumulator by CONNECTION.
        A per-tick client would reconnect every ~45 s and drain a fresh, always
        empty window — the latch would never see a touch."""
        with (
            mock.patch.dict(
                "os.environ", {**_LIVE_PRICES_ENV, self._SOCKET_ENV: "/tmp/r.sock"}, clear=True
            ),
            mock.patch(
                "alphalens_pipeline.data.alt_data.price_reader_client.RemoteQuoteSource"
            ) as remote_cls,
        ):
            cl._reset_remote_quote_source_for_tests()
            _default_live_exits_feed_factory({211: ("AAPL", "XNYS")}, scope="exits")
            _default_live_exits_feed_factory({211: ("AAPL", "XNYS")}, scope="entry-watch")
            cl._reset_remote_quote_source_for_tests()

        self.assertEqual(remote_cls.call_count, 1)


class TestSaxoImportsStayLazy(unittest.TestCase):
    """Fix round 2, minor 7: 'nothing is imported on the OFF path' was prose,
    not a test -- moving these two imports to module scope would keep every
    other test in this module green. Pins that both stay function-scoped
    inside _default_live_exits_feed_factory, not promoted to module level."""

    def test_saxo_live_feed_and_stream_imports_stay_function_scoped(self):
        tree = ast.parse(inspect.getsource(cl))
        top_level_modules = {
            node.module
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertNotIn(
            "alphalens_pipeline.brokers.automanager.saxo_live_price_feed", top_level_modules
        )
        self.assertNotIn("alphalens_pipeline.data.alt_data.saxo_price_stream", top_level_modules)


if __name__ == "__main__":
    unittest.main()
