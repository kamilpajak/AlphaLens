"""ADR 0017 composition root — the ``env == live`` branch of
``control_loop.build_default_deps`` (design memo §2 / §6).

Complements ``tests/brokers/test_saxo_live_daemon_rail.py`` (the LIVE factory
and its refusal paths in isolation) and the existing D4/D7 coverage in
``test_control_loop.py::TestBuildDefaultDepsStateGuards`` (which pins that the
D7 hard-raise is gone and a rail-unset LIVE boot is refused by
``assert_live_rails`` instead). This module covers what only the composition
root itself can prove:

- the patched LIVE factory is the SOLE broker-construction path for
  ``env=live`` — ``get_default_broker`` (the SIM registry) is NEVER reached;
- the ``SessionKeeper`` is wired over the EXACT ``LiveOrderTokenProvider``
  instance the factory returned, never a second adapter over the same
  underlying chain;
- the order-WS streaming subscriber is STRUCTURALLY skipped for ``env=live``
  even when ``ALPHALENS_BROKER_STREAMING_ENABLED=1`` — the memo pins the flag
  to ``0`` on the LIVE unit, but the code must not depend on that pin.

Every test here patches ``create_saxo_broker_live_from_env`` (never the real
factory with real credentials) and uses sentinel/mock objects only — no
network, no real Saxo auth chain, matching the SIM rail-test hermetic pattern.
"""

from __future__ import annotations

import contextlib
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from alphalens_pipeline.brokers.automanager import control_loop as cl
from alphalens_pipeline.brokers.automanager import state_paths
from alphalens_pipeline.brokers.automanager.live_rails import (
    DAILY_LOSS_LIMIT_R_ENV,
    EXIT_POLICY_ENV,
    MAX_FEE_BPS_ENV,
    MAX_OPEN_ENV,
    PORTFOLIO_GROSS_FRAC_ENV,
    SIZING_EQUITY_ENV,
)
from broker_contract.contract import BrokerCapabilityError

# A fully in-bounds §3 boot-assert env, mirroring test_saxo_live_daemon_rail's
# _VALID_RAIL_ENV so a rails-pass/rails-fail test only ever differs on the one
# variable each case is pinning.
_VALID_RAIL_ENV: dict[str, str] = {
    MAX_OPEN_ENV: "1",
    PORTFOLIO_GROSS_FRAC_ENV: "0.25",
    DAILY_LOSS_LIMIT_R_ENV: "1.0",
    SIZING_EQUITY_ENV: "10000",
    EXIT_POLICY_ENV: "trailing_atr",
    MAX_FEE_BPS_ENV: "100",
}

_LIVE_ACCOUNT_KEY = "LIVE-COMPOSITION-ROOT-SENTINEL-ACCOUNT"

_LIVE_GRANT_ENV: dict[str, str] = {
    "SAXO_LIVE_ACCOUNT_KEY": _LIVE_ACCOUNT_KEY,
    "ALPHALENS_SAXO_LIVE_STANDING": _LIVE_ACCOUNT_KEY,
}

_FULL_VALID_LIVE_ENV: dict[str, str] = {
    "ALPHALENS_BROKER_ENVIRONMENT": "live",
    **_VALID_RAIL_ENV,
    **_LIVE_GRANT_ENV,
}


@contextlib.contextmanager
def _isolated_home():
    """Patch ``Path.home()`` to a fresh, empty temp directory — mirrors
    ``test_control_loop.py::_isolated_home`` so ``build_default_deps``'s D4
    legacy-layout guard never sees a developer machine's real
    ``~/.alphalens/broker_orders/`` tree."""
    with (
        TemporaryDirectory() as home_dir,
        mock.patch("pathlib.Path.home", return_value=Path(home_dir)),
    ):
        yield Path(home_dir)


def _live_broker_stub() -> mock.Mock:
    """A mock satisfying every runtime_checkable Broker capability Protocol
    the composition root probes (SupportsStandaloneStop / SupportsOcoExit /
    SupportsAmendStop — the last is required because ``_VALID_RAIL_ENV``
    pins ``EXIT_POLICY_ENV=trailing_atr``, which needs the AmendStop rail).

    Each capability method is set EXPLICITLY (not left to Mock's
    ``__getattr__`` auto-creation): CPython 3.12+'s runtime-checkable
    ``isinstance`` check uses ``inspect.getattr_static``, which does not see
    attributes ``__getattr__`` fabricates on first access — an unconfigured
    ``mock.Mock()`` silently fails every capability ``isinstance`` check."""
    broker = mock.Mock(name="LiveSaxoBrokerStub")
    broker.name = "saxo"
    broker.place_standalone_stop = mock.Mock(name="place_standalone_stop")
    broker.place_oco_exit = mock.Mock(name="place_oco_exit")
    broker.amend_stop_amount = mock.Mock(name="amend_stop_amount")
    return broker


def _live_provider_stub() -> mock.Mock:
    provider = mock.Mock(name="LiveOrderTokenProviderStub")
    provider.get_access_token.return_value = "live-tok"
    return provider


class TestLiveBranchNeverReachesSimRegistry(unittest.TestCase):
    """env=live must NEVER construct a broker through get_default_broker —
    the SIM registry path stays structurally unreachable regardless of
    whether the LIVE factory itself succeeds or refuses."""

    def test_rails_unset_refused_by_assert_live_rails_never_touches_registry(self) -> None:
        """The real (unpatched) LIVE factory runs — a rails-unset LIVE boot
        is refused by assert_live_rails naming the missing rails, exactly as
        design memo §3 / ADR 0017 point 4 requires, and the SIM registry is
        never consulted."""
        env = {"ALPHALENS_BROKER_ENVIRONMENT": "live"}
        with (
            _isolated_home(),
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker"
            ) as mock_get_default_broker,
        ):
            with self.assertRaises(BrokerCapabilityError) as ctx:
                cl.build_default_deps(notify=lambda _msg: None, chain_loss_notify=lambda _msg: None)
        message = str(ctx.exception)
        self.assertIn("ADR 0017", message)
        for var in _VALID_RAIL_ENV:
            self.assertIn(var, message, f"{var} must be named in the boot-assert failure")
        mock_get_default_broker.assert_not_called()

    def test_valid_rails_and_grant_patched_factory_never_touches_registry(self) -> None:
        """The happy path — the LIVE factory is patched (no real Saxo auth
        chain), deps are built from its return, and get_default_broker is
        never called."""
        broker_stub = _live_broker_stub()
        provider_stub = _live_provider_stub()
        chain_loss_sink = mock.Mock()
        with (
            _isolated_home(),
            mock.patch.dict("os.environ", _FULL_VALID_LIVE_ENV, clear=True),
            mock.patch(
                "alphalens_pipeline.brokers.saxo.broker.create_saxo_broker_live_from_env",
                return_value=(broker_stub, provider_stub),
            ) as mock_factory,
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker"
            ) as mock_get_default_broker,
        ):
            deps = cl.build_default_deps(
                notify=lambda _msg: None, chain_loss_notify=chain_loss_sink
            )

        mock_factory.assert_called_once_with(alert=chain_loss_sink)
        mock_get_default_broker.assert_not_called()
        self.assertIs(deps.broker, broker_stub)


class TestLiveSessionKeeperReusesTheFactoryAdapter(unittest.TestCase):
    """The composition root must build SessionKeeper over the SAME
    LiveOrderTokenProvider instance the factory returned — never construct a
    second adapter over the same underlying chain (design memo §2: two
    adapters would be two independent dead-latches that could disagree)."""

    def test_ensure_alive_calls_the_exact_factory_provider_instance(self) -> None:
        broker_stub = _live_broker_stub()
        provider_stub = _live_provider_stub()
        with (
            _isolated_home(),
            mock.patch.dict("os.environ", _FULL_VALID_LIVE_ENV, clear=True),
            mock.patch(
                "alphalens_pipeline.brokers.saxo.broker.create_saxo_broker_live_from_env",
                return_value=(broker_stub, provider_stub),
            ),
        ):
            deps = cl.build_default_deps(
                notify=lambda _msg: None, chain_loss_notify=lambda _msg: None
            )

        provider_stub.get_access_token.assert_not_called()
        status = deps.ensure_alive()
        self.assertTrue(status.alive)
        provider_stub.get_access_token.assert_called_once()

    def test_dead_chain_on_the_factory_provider_surfaces_through_ensure_alive(self) -> None:
        """If ensure_alive read a DIFFERENT (second) adapter, this dead-chain
        signal on the real one would never surface — proving identity, not
        just that SOME provider answers."""
        from broker_contract.contract import BrokerAuthError

        broker_stub = _live_broker_stub()
        provider_stub = _live_provider_stub()
        provider_stub.get_access_token.side_effect = BrokerAuthError("live chain lost")
        with (
            _isolated_home(),
            mock.patch.dict("os.environ", _FULL_VALID_LIVE_ENV, clear=True),
            mock.patch(
                "alphalens_pipeline.brokers.saxo.broker.create_saxo_broker_live_from_env",
                return_value=(broker_stub, provider_stub),
            ),
        ):
            deps = cl.build_default_deps(
                notify=lambda _msg: None, chain_loss_notify=lambda _msg: None
            )
        status = deps.ensure_alive()
        self.assertFalse(status.alive)
        self.assertIn("live chain lost", status.reason or "")


class TestLiveStreamingStructurallySkipped(unittest.TestCase):
    """The order-WS streaming subscriber is a SIM-rail SaxoClient
    (_build_streaming_subscriber); env=live must never build it, regardless
    of ALPHALENS_BROKER_STREAMING_ENABLED (design memo §3 pins the flag to 0
    on the LIVE unit, but the code must not depend on that pin)."""

    def test_streaming_flag_on_is_still_skipped_for_live_with_info_log(self) -> None:
        broker_stub = _live_broker_stub()
        provider_stub = _live_provider_stub()
        env = dict(_FULL_VALID_LIVE_ENV, ALPHALENS_BROKER_STREAMING_ENABLED="1")
        with (
            _isolated_home(),
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch(
                "alphalens_pipeline.brokers.saxo.broker.create_saxo_broker_live_from_env",
                return_value=(broker_stub, provider_stub),
            ),
            mock.patch.object(cl, "_build_streaming_subscriber") as mock_subscriber,
            self.assertLogs(cl.logger, level="INFO") as captured,
        ):
            deps = cl.build_default_deps(
                notify=lambda _msg: None, chain_loss_notify=lambda _msg: None
            )

        mock_subscriber.assert_not_called()
        self.assertIsNone(deps.wake_event)
        self.assertIsNone(deps.stream_tick)
        self.assertIsNone(deps.stream_trigger)
        self.assertTrue(
            any("stream" in line.lower() and "live" in line.lower() for line in captured.output),
            captured.output,
        )

    def test_streaming_flag_off_is_skipped_for_live_too(self) -> None:
        broker_stub = _live_broker_stub()
        provider_stub = _live_provider_stub()
        with (
            _isolated_home(),
            mock.patch.dict("os.environ", _FULL_VALID_LIVE_ENV, clear=True),
            mock.patch(
                "alphalens_pipeline.brokers.saxo.broker.create_saxo_broker_live_from_env",
                return_value=(broker_stub, provider_stub),
            ),
            mock.patch.object(cl, "_build_streaming_subscriber") as mock_subscriber,
        ):
            deps = cl.build_default_deps(
                notify=lambda _msg: None, chain_loss_notify=lambda _msg: None
            )
        mock_subscriber.assert_not_called()
        self.assertIsNone(deps.wake_event)


class TestSimBranchByteIdenticalUnderLiveComposition(unittest.TestCase):
    """env=sim (the default) must keep using the SIM registry + the SIM
    OAuth provider path, exactly as before this PR — a regression here would
    mean the LIVE branch leaked into the SIM path."""

    def test_sim_still_uses_get_default_broker_and_default_oauth_provider(self) -> None:
        from broker_contract.contract import PlacedOrder

        class _StopOnlyBroker:
            name = "stoponly"

            def place_standalone_stop(
                self,
                uic: int,
                side: str,
                qty: float,
                stop_price: float,
                request_id: str | None = None,
            ) -> PlacedOrder:
                return PlacedOrder(entry_order_id="S-1", exit_order_ids=())

        env = {k: v for k, v in os.environ.items() if not k.startswith("ALPHALENS_BROKER_")}
        with (
            _isolated_home(),
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch(
                "alphalens_pipeline.brokers.registry.get_default_broker",
                return_value=_StopOnlyBroker(),
            ) as mock_get_default_broker,
            mock.patch(
                "alphalens_pipeline.brokers.saxo.broker.create_saxo_broker_live_from_env"
            ) as mock_live_factory,
            mock.patch.object(
                cl, "_default_oauth_provider", return_value=mock.Mock()
            ) as oauth_factory,
        ):
            deps = cl.build_default_deps(
                notify=lambda _msg: None, chain_loss_notify=lambda _msg: None
            )
        mock_get_default_broker.assert_called_once_with()
        mock_live_factory.assert_not_called()
        oauth_factory.assert_called_once_with(alert=mock.ANY)
        self.assertEqual(deps.broker.name, "stoponly")
        self.assertEqual(state_paths.broker_environment(), state_paths.ENV_SIM)


if __name__ == "__main__":
    unittest.main()
