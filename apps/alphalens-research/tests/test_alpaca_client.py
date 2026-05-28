"""Tests for the canonical AlpacaClient.

The alpaca-py SDK is mocked at module-load time via ``sys.modules`` so these
tests run with or without the real SDK installed — the canonical client owns
the import boundary, and the tests exercise wrapper logic, not the SDK.

Mirrors :mod:`tests.test_gemini_client` structurally (same fake-SDK pattern,
same singleton reset / sys.modules snapshot discipline).
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _install_fake_alpaca(target: dict) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Install a fake ``alpaca.trading.{client,requests,enums}`` into the given
    ``sys.modules`` snapshot so the canonical client's lazy import resolves to
    the mock. Returns (client_module, requests_module, enums_module) so each
    test can inspect what got constructed.
    """
    fake_alpaca = types.ModuleType("alpaca")
    fake_trading = types.ModuleType("alpaca.trading")
    fake_trading_client = types.ModuleType("alpaca.trading.client")
    fake_trading_requests = types.ModuleType("alpaca.trading.requests")
    fake_trading_enums = types.ModuleType("alpaca.trading.enums")

    fake_trading_client.TradingClient = MagicMock(name="TradingClient")
    # Each request dataclass is a MagicMock so the test can assert the kwargs
    # the wrapper passed through (symbol, qty, limit_price, side enum, etc.).
    fake_trading_requests.LimitOrderRequest = MagicMock(name="LimitOrderRequest")
    fake_trading_requests.MarketOrderRequest = MagicMock(name="MarketOrderRequest")
    fake_trading_requests.StopOrderRequest = MagicMock(name="StopOrderRequest")
    fake_trading_requests.TakeProfitRequest = MagicMock(name="TakeProfitRequest")
    fake_trading_requests.StopLossRequest = MagicMock(name="StopLossRequest")
    fake_trading_requests.GetOrdersRequest = MagicMock(name="GetOrdersRequest")

    class _OrderSide:
        BUY = "OrderSide.BUY"
        SELL = "OrderSide.SELL"

    class _OrderClass:
        SIMPLE = "OrderClass.SIMPLE"
        BRACKET = "OrderClass.BRACKET"
        OCO = "OrderClass.OCO"
        OTO = "OrderClass.OTO"

    class _TimeInForce:
        GTC = "TimeInForce.GTC"
        DAY = "TimeInForce.DAY"

    fake_trading_enums.OrderSide = _OrderSide
    fake_trading_enums.OrderClass = _OrderClass
    fake_trading_enums.TimeInForce = _TimeInForce

    target["alpaca"] = fake_alpaca
    target["alpaca.trading"] = fake_trading
    target["alpaca.trading.client"] = fake_trading_client
    target["alpaca.trading.requests"] = fake_trading_requests
    target["alpaca.trading.enums"] = fake_trading_enums
    fake_alpaca.trading = fake_trading
    fake_trading.client = fake_trading_client
    fake_trading.requests = fake_trading_requests
    fake_trading.enums = fake_trading_enums

    return fake_trading_client, fake_trading_requests, fake_trading_enums


class _FakeAlpacaTestCase(unittest.TestCase):
    """Base test case: snapshots ``sys.modules`` so the fake SDK installed for
    each test is restored on teardown. Resets the canonical client's lazy
    singleton + module-level SDK cache so each test starts clean.
    """

    def setUp(self):
        self._sys_modules_patcher = patch.dict("sys.modules")
        self._sys_modules_patcher.start()
        self.fake_client_mod, self.fake_requests_mod, self.fake_enums_mod = _install_fake_alpaca(
            sys.modules
        )
        from alphalens_pipeline.data.alt_data import alpaca_client as mod

        mod._reset_default_client_for_tests()
        mod._reset_sdk_cache_for_tests()

    def tearDown(self):
        from alphalens_pipeline.data.alt_data import alpaca_client as mod

        mod._reset_default_client_for_tests()
        mod._reset_sdk_cache_for_tests()
        self._sys_modules_patcher.stop()


class TestClientConstruction(_FakeAlpacaTestCase):
    def test_constructor_rejects_empty_api_key(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClient

        with self.assertRaises(ValueError):
            AlpacaClient(api_key="", secret_key="s")

    def test_constructor_rejects_empty_secret(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClient

        with self.assertRaises(ValueError):
            AlpacaClient(api_key="k", secret_key="")

    def test_construction_hardcodes_paper_true(self):
        """The SDK's TradingClient must be called with paper=True every time —
        the wrapper offers no constructor knob to flip it.
        """
        from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClient

        AlpacaClient(api_key="k", secret_key="s")
        # First positional + paper kwarg.
        self.fake_client_mod.TradingClient.assert_called_once_with("k", "s", paper=True)


class TestPaperBaseUrlGuard(_FakeAlpacaTestCase):
    def test_rejects_live_base_url(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import (
            BASE_URL_ENV,
            AlpacaClient,
            AlpacaClientError,
        )

        with (
            patch.dict("os.environ", {BASE_URL_ENV: "https://api.alpaca.markets"}, clear=False),
            self.assertRaises(AlpacaClientError),
        ):
            AlpacaClient(api_key="k", secret_key="s")

    def test_accepts_canonical_paper_url(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import (
            BASE_URL_ENV,
            PAPER_BASE_URL,
            AlpacaClient,
        )

        with patch.dict("os.environ", {BASE_URL_ENV: PAPER_BASE_URL}, clear=False):
            AlpacaClient(api_key="k", secret_key="s")  # no raise

    def test_accepts_v2_paper_url_variant(self):
        """Alpaca dashboard shows the URL as ``.../v2``; the guard must let
        that copy-paste form through. The SDK strips/re-adds the version
        itself, so functionally both forms behave identically."""
        from alphalens_pipeline.data.alt_data.alpaca_client import (
            BASE_URL_ENV,
            PAPER_BASE_URL,
            AlpacaClient,
        )

        with patch.dict("os.environ", {BASE_URL_ENV: f"{PAPER_BASE_URL}/v2"}, clear=False):
            AlpacaClient(api_key="k", secret_key="s")  # no raise

    def test_unset_url_uses_sdk_default(self):
        """When the env var is unset the SDK's own paper=True default kicks in;
        the wrapper must not synthesise a URL itself."""
        from alphalens_pipeline.data.alt_data.alpaca_client import BASE_URL_ENV, AlpacaClient

        # patch.dict with clear=False then explicit pop to ensure ENV is unset.
        env_without_url = {k: v for k, v in __import__("os").environ.items() if k != BASE_URL_ENV}
        with patch.dict("os.environ", env_without_url, clear=True):
            AlpacaClient(api_key="k", secret_key="s")  # no raise


class TestFromEnv(_FakeAlpacaTestCase):
    def test_from_env_reads_key_and_secret(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import (
            API_KEY_ENV,
            SECRET_ENV,
            AlpacaClient,
        )

        with patch.dict("os.environ", {API_KEY_ENV: "K", SECRET_ENV: "S"}, clear=False):
            AlpacaClient.from_env()
        self.fake_client_mod.TradingClient.assert_called_once_with("K", "S", paper=True)

    def test_from_env_raises_when_key_missing(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import (
            API_KEY_ENV,
            SECRET_ENV,
            AlpacaClient,
        )

        # Strip both env vars first, then set only the secret.
        env_no_key = {k: v for k, v in __import__("os").environ.items() if k != API_KEY_ENV}
        env_no_key[SECRET_ENV] = "S"
        with patch.dict("os.environ", env_no_key, clear=True), self.assertRaises(ValueError):
            AlpacaClient.from_env()

    def test_from_env_raises_when_secret_missing(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import (
            API_KEY_ENV,
            SECRET_ENV,
            AlpacaClient,
        )

        env_no_secret = {k: v for k, v in __import__("os").environ.items() if k != SECRET_ENV}
        env_no_secret[API_KEY_ENV] = "K"
        with patch.dict("os.environ", env_no_secret, clear=True), self.assertRaises(ValueError):
            AlpacaClient.from_env()

    def test_from_env_test_profile_reads_test_env_vars(self):
        """profile='test' reads ALPACA_TEST_API_KEY/SECRET, NOT the main ones —
        so a dev smoke-test never touches the production paper account."""
        import os

        from alphalens_pipeline.data.alt_data.alpaca_client import (
            API_KEY_ENV,
            SECRET_ENV,
            TEST_API_KEY_ENV,
            TEST_SECRET_ENV,
            AlpacaClient,
        )

        env = {k: v for k, v in os.environ.items() if k not in (API_KEY_ENV, SECRET_ENV)}
        env[TEST_API_KEY_ENV] = "TEST_K"
        env[TEST_SECRET_ENV] = "TEST_S"
        with patch.dict("os.environ", env, clear=True):
            AlpacaClient.from_env(profile="test")
        # SDK constructor called with the TEST credentials, not the main ones.
        self.fake_client_mod.TradingClient.assert_called_once_with("TEST_K", "TEST_S", paper=True)

    def test_from_env_main_profile_default_reads_main_env_vars(self):
        """profile='main' (default) reads ALPACA_API_KEY/SECRET and ignores
        ALPACA_TEST_API_KEY even if set."""
        from alphalens_pipeline.data.alt_data.alpaca_client import (
            API_KEY_ENV,
            SECRET_ENV,
            TEST_API_KEY_ENV,
            TEST_SECRET_ENV,
            AlpacaClient,
        )

        with patch.dict(
            "os.environ",
            {
                API_KEY_ENV: "MAIN_K",
                SECRET_ENV: "MAIN_S",
                TEST_API_KEY_ENV: "TEST_K",
                TEST_SECRET_ENV: "TEST_S",
            },
            clear=False,
        ):
            AlpacaClient.from_env()  # default profile="main"
        self.fake_client_mod.TradingClient.assert_called_once_with("MAIN_K", "MAIN_S", paper=True)

    def test_from_env_rejects_invalid_profile(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClient

        with self.assertRaises(ValueError) as cm:
            AlpacaClient.from_env(profile="prod")  # type: ignore[arg-type]
        self.assertIn("prod", str(cm.exception))

    def test_from_env_test_profile_raises_when_test_key_missing(self):
        import os

        from alphalens_pipeline.data.alt_data.alpaca_client import (
            API_KEY_ENV,
            SECRET_ENV,
            TEST_API_KEY_ENV,
            TEST_SECRET_ENV,
            AlpacaClient,
        )

        env = {
            k: v
            for k, v in os.environ.items()
            if k not in (TEST_API_KEY_ENV, TEST_SECRET_ENV, API_KEY_ENV, SECRET_ENV)
        }
        env[TEST_SECRET_ENV] = "S"  # only secret set; key missing
        with patch.dict("os.environ", env, clear=True), self.assertRaises(ValueError) as cm:
            AlpacaClient.from_env(profile="test")
        self.assertIn(TEST_API_KEY_ENV, str(cm.exception))


class TestSingleton(_FakeAlpacaTestCase):
    def test_singleton_caches_instance(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import (
            API_KEY_ENV,
            SECRET_ENV,
            get_default_alpaca_client,
        )

        with patch.dict("os.environ", {API_KEY_ENV: "K", SECRET_ENV: "S"}, clear=False):
            a = get_default_alpaca_client()
            b = get_default_alpaca_client()
        self.assertIs(a, b)
        # TradingClient only constructed once across the two get_default calls.
        self.assertEqual(self.fake_client_mod.TradingClient.call_count, 1)

    def test_reset_clears_singleton(self):
        from alphalens_pipeline.data.alt_data import alpaca_client as mod

        with patch.dict("os.environ", {mod.API_KEY_ENV: "K", mod.SECRET_ENV: "S"}, clear=False):
            a = mod.get_default_alpaca_client()
            mod._reset_default_client_for_tests()
            b = mod.get_default_alpaca_client()
        self.assertIsNot(a, b)

    def test_main_and_test_profiles_cache_separately(self):
        """Each profile gets its own singleton — calling get_default twice
        with profile='main' then profile='test' constructs TWO clients,
        not one. This prevents test-account creds from being used when
        the planner asks for the main account."""
        from alphalens_pipeline.data.alt_data import alpaca_client as mod

        with patch.dict(
            "os.environ",
            {
                mod.API_KEY_ENV: "MAIN_K",
                mod.SECRET_ENV: "MAIN_S",
                mod.TEST_API_KEY_ENV: "TEST_K",
                mod.TEST_SECRET_ENV: "TEST_S",
            },
            clear=False,
        ):
            main = mod.get_default_alpaca_client(profile="main")
            test = mod.get_default_alpaca_client(profile="test")
        self.assertIsNot(main, test)
        self.assertEqual(self.fake_client_mod.TradingClient.call_count, 2)
        # Profile-specific re-call returns the cached instance.
        with patch.dict(
            "os.environ",
            {
                mod.API_KEY_ENV: "MAIN_K",
                mod.SECRET_ENV: "MAIN_S",
                mod.TEST_API_KEY_ENV: "TEST_K",
                mod.TEST_SECRET_ENV: "TEST_S",
            },
            clear=False,
        ):
            self.assertIs(mod.get_default_alpaca_client(profile="test"), test)
        self.assertEqual(self.fake_client_mod.TradingClient.call_count, 2)

    def test_get_default_rejects_invalid_profile(self):
        from alphalens_pipeline.data.alt_data import alpaca_client as mod

        with self.assertRaises(ValueError):
            mod.get_default_alpaca_client(profile="prod")  # type: ignore[arg-type]

    def test_concurrent_first_call_constructs_only_one_client(self):
        """Two threads racing on the first ``get_default_alpaca_client`` call
        must NOT each construct a fresh client. Double-checked locking pattern
        guards the singleton; without it the planner + reconciler could end
        up with separate clients (separate SDK keepalive pools, separate
        quota counters) under concurrent first-call conditions."""
        import threading

        from alphalens_pipeline.data.alt_data import alpaca_client as mod

        instances: list[object] = []
        barrier = threading.Barrier(2)

        def race() -> None:
            barrier.wait()  # release both threads simultaneously
            instances.append(mod.get_default_alpaca_client())

        with patch.dict("os.environ", {mod.API_KEY_ENV: "K", mod.SECRET_ENV: "S"}, clear=False):
            t1 = threading.Thread(target=race)
            t2 = threading.Thread(target=race)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        self.assertEqual(len(instances), 2)
        self.assertIs(instances[0], instances[1])
        # SDK TradingClient was constructed exactly once across both threads.
        self.assertEqual(self.fake_client_mod.TradingClient.call_count, 1)


class TestOrderPrimitives(_FakeAlpacaTestCase):
    def _build_client(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClient

        return AlpacaClient(api_key="k", secret_key="s")

    def _trading_instance(self):
        # The MagicMock-backed TradingClient returns a MagicMock instance
        # when called — that's what the wrapper holds.
        return self.fake_client_mod.TradingClient.return_value

    def test_submit_limit_buy_constructs_correct_request(self):
        client = self._build_client()
        client.submit_limit_order(symbol="NVDA", qty=10, limit_price=125.50)

        self.fake_requests_mod.LimitOrderRequest.assert_called_once_with(
            symbol="NVDA",
            qty=10,
            side=self.fake_enums_mod.OrderSide.BUY,
            time_in_force=self.fake_enums_mod.TimeInForce.GTC,
            limit_price=125.50,
        )
        self._trading_instance().submit_order.assert_called_once_with(
            order_data=self.fake_requests_mod.LimitOrderRequest.return_value
        )

    def test_submit_limit_sell_passes_sell_side(self):
        client = self._build_client()
        client.submit_limit_order(symbol="NVDA", qty=10, limit_price=200.0, side="sell")

        self.fake_requests_mod.LimitOrderRequest.assert_called_once_with(
            symbol="NVDA",
            qty=10,
            side=self.fake_enums_mod.OrderSide.SELL,
            time_in_force=self.fake_enums_mod.TimeInForce.GTC,
            limit_price=200.0,
        )

    def test_submit_stop_order_uses_stop_request(self):
        client = self._build_client()
        client.submit_stop_order(symbol="NVDA", qty=10, stop_price=110.0)

        self.fake_requests_mod.StopOrderRequest.assert_called_once_with(
            symbol="NVDA",
            qty=10,
            side=self.fake_enums_mod.OrderSide.SELL,
            time_in_force=self.fake_enums_mod.TimeInForce.GTC,
            stop_price=110.0,
        )

    def test_submit_bracket_order_attaches_tp_and_sl_legs(self):
        """One-call BRACKET for the single-TP-tranche case. The reconciler's
        multi-tranche TP ladder still uses separate limit-sells + stop, but
        when ``brief_trade_setup`` ships exactly one TP this is the cleaner
        path with automatic OCO semantics on fill."""
        client = self._build_client()
        client.submit_bracket_order(
            symbol="NVDA",
            qty=10,
            limit_price=100.0,
            take_profit_price=120.0,
            stop_loss_price=90.0,
        )
        self.fake_requests_mod.TakeProfitRequest.assert_called_once_with(limit_price=120.0)
        self.fake_requests_mod.StopLossRequest.assert_called_once_with(stop_price=90.0)
        # The LimitOrderRequest was called with order_class=BRACKET + legs.
        kwargs = self.fake_requests_mod.LimitOrderRequest.call_args.kwargs
        self.assertEqual(kwargs["order_class"], self.fake_enums_mod.OrderClass.BRACKET)
        self.assertIn("take_profit", kwargs)
        self.assertIn("stop_loss", kwargs)
        self.assertEqual(kwargs["side"], self.fake_enums_mod.OrderSide.BUY)

    def test_submit_bracket_order_rejects_missing_take_profit(self):
        """Alpaca BRACKET requires BOTH legs; submitting with only SL fails
        Alpaca with HTTP 422. The wrapper guards locally with a clear
        ValueError so the failure is unambiguous at the call site instead of
        opaque at submission time. Per zen second-round review."""
        client = self._build_client()
        with self.assertRaises(ValueError) as cm:
            client.submit_bracket_order(
                symbol="NVDA", qty=10, limit_price=100.0, stop_loss_price=90.0
            )
        self.assertIn("BRACKET", str(cm.exception))

    def test_submit_bracket_order_rejects_missing_stop_loss(self):
        """Mirror of the above — only TP supplied also raises."""
        client = self._build_client()
        with self.assertRaises(ValueError):
            client.submit_bracket_order(
                symbol="NVDA", qty=10, limit_price=100.0, take_profit_price=120.0
            )

    def test_submit_market_order_defaults_day_tif(self):
        client = self._build_client()
        client.submit_market_order(symbol="NVDA", qty=10)

        self.fake_requests_mod.MarketOrderRequest.assert_called_once_with(
            symbol="NVDA",
            qty=10,
            side=self.fake_enums_mod.OrderSide.SELL,
            time_in_force=self.fake_enums_mod.TimeInForce.DAY,
        )

    def test_unsupported_tif_raises(self):
        client = self._build_client()
        with self.assertRaises(ValueError):
            client.submit_limit_order(symbol="X", qty=1, limit_price=1.0, time_in_force="ioc")

    def test_unsupported_side_raises(self):
        client = self._build_client()
        with self.assertRaises(ValueError):
            client.submit_limit_order(symbol="X", qty=1, limit_price=1.0, side="hold")


class TestPortfolioReads(_FakeAlpacaTestCase):
    def _build_client(self):
        from alphalens_pipeline.data.alt_data.alpaca_client import AlpacaClient

        return AlpacaClient(api_key="k", secret_key="s")

    def _trading_instance(self):
        return self.fake_client_mod.TradingClient.return_value

    def test_get_position_returns_position(self):
        sentinel = MagicMock(name="Position")
        self._trading_instance().get_open_position.return_value = sentinel

        client = self._build_client()
        result = client.get_position("NVDA")

        self.assertIs(result, sentinel)
        self._trading_instance().get_open_position.assert_called_once_with("NVDA")

    def test_get_position_returns_none_when_sdk_raises(self):
        """No-position is the 404 case; the SDK raises but we hide it so
        callers can use a clean ``if pos is None`` flow for dedup decisions."""
        self._trading_instance().get_open_position.side_effect = Exception(
            "position does not exist"
        )

        client = self._build_client()
        result = client.get_position("NVDA")

        self.assertIsNone(result)

    def test_get_position_returns_none_on_404_status_code(self):
        """The SDK's APIError carries a ``status_code`` attribute; the
        wrapper accepts 404 as the missing-position signal alongside the
        message-text path so both paths converge on returning ``None``."""
        exc = Exception("Some other 404 text not containing the keyword")
        exc.status_code = 404
        self._trading_instance().get_open_position.side_effect = exc

        client = self._build_client()
        self.assertIsNone(client.get_position("NVDA"))

    def test_get_position_reraises_on_non_missing_errors(self):
        """A timeout / 5xx / auth failure is NOT a missing-position signal.
        Swallowing it would let the planner treat the ticker as flat during
        an Alpaca-side incident and double-up the position when the service
        recovers — the exact opposite of the dedup intent. Re-raise so the
        caller decides whether to retry or alert."""
        exc = Exception("Connection timed out")
        exc.status_code = 500
        self._trading_instance().get_open_position.side_effect = exc

        client = self._build_client()
        with self.assertRaises(Exception) as cm:
            client.get_position("NVDA")
        self.assertIn("Connection timed out", str(cm.exception))

    def test_get_orders_without_status_uses_sdk_default(self):
        """No-arg call passes nothing to the SDK so its own default applies."""
        client = self._build_client()
        client.get_orders()
        self._trading_instance().get_orders.assert_called_once_with()

    def test_get_orders_with_status_wraps_in_get_orders_request(self):
        """``status='open'`` is routed through the cached SDK handle's
        GetOrdersRequest so the no-raw-http enforcement net stays intact."""
        client = self._build_client()
        client.get_orders(status="open")
        self.fake_requests_mod.GetOrdersRequest.assert_called_once_with(status="open")
        self._trading_instance().get_orders.assert_called_once_with(
            filter=self.fake_requests_mod.GetOrdersRequest.return_value
        )

    def test_get_all_positions_passes_through(self):
        self._trading_instance().get_all_positions.return_value = [MagicMock(), MagicMock()]
        client = self._build_client()
        result = client.get_all_positions()
        self.assertEqual(len(result), 2)

    def test_get_account_passes_through(self):
        sentinel = MagicMock(name="Account")
        self._trading_instance().get_account.return_value = sentinel
        client = self._build_client()
        self.assertIs(client.get_account(), sentinel)

    def test_cancel_order_passes_through(self):
        client = self._build_client()
        client.cancel_order("order-id-123")
        self._trading_instance().cancel_order_by_id.assert_called_once_with("order-id-123")


class TestSdkMissingImport(unittest.TestCase):
    """If alpaca-py is not installed, the wrapper must raise an actionable
    AlpacaClientError. Mirror of the gemini_client / polygon_client behaviour.

    We block the import by patching ``builtins.__import__`` rather than
    walking ``sys.meta_path`` — the latter relies on ``find_module`` which is
    a removed legacy API in Python 3.13+.
    """

    def test_missing_sdk_raises_with_actionable_message(self):
        import builtins

        from alphalens_pipeline.data.alt_data import alpaca_client as mod

        # Strip any cached alpaca-py modules so the next import goes through
        # ``__import__`` (the wrapper's lazy loader) and hits our patched one.
        with patch.dict("sys.modules") as _:
            for key in list(sys.modules):
                if key == "alpaca" or key.startswith("alpaca."):
                    del sys.modules[key]
            mod._reset_sdk_cache_for_tests()

            real_import = builtins.__import__

            def fake_import(name, *args, **kwargs):
                if name == "alpaca" or name.startswith("alpaca."):
                    raise ImportError(f"blocked import of {name} for test")
                return real_import(name, *args, **kwargs)

            try:
                with (
                    patch("builtins.__import__", side_effect=fake_import),
                    self.assertRaises(mod.AlpacaClientError) as cm,
                ):
                    mod.AlpacaClient(api_key="k", secret_key="s")
                self.assertIn("alpaca-py", str(cm.exception))
            finally:
                mod._reset_sdk_cache_for_tests()


if __name__ == "__main__":
    unittest.main()
