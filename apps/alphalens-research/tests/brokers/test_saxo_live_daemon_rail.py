"""Standing-LIVE authorization (ADR 0017) — the daemon-arm constructor capability.

ADR 0017 adds a SECOND, independent way to unlock the constructor guard
alongside the ADR 0015 attended day-bound unlock (``test_saxo_sim_only_rail.py``,
untouched): a keyword-only ``standing_live_authorized`` PLUS an account-bound
env-var grant (``ALPHALENS_SAXO_LIVE_STANDING`` == ``SAXO_LIVE_ACCOUNT_KEY``).
Neither the keyword alone nor the grant alone is a capability — both are
required simultaneously, and ``from_env`` never passes the keyword, so every
default construction path stays unconditionally SIM-only regardless of what
is in the environment.
"""

from __future__ import annotations

import datetime
import os
import unittest
from pathlib import Path
from unittest import mock

from alphalens_pipeline.brokers.automanager.live_rails import (
    DAILY_LOSS_LIMIT_R_ENV,
    ENTRY_TRAIL_BPS_ENV,
    ENTRY_WATCH_MAX_PICKS_ENV,
    EXIT_POLICY_ENV,
    MAX_FEE_BPS_ENV,
    MAX_OPEN_ENV,
    PORTFOLIO_GROSS_FRAC_ENV,
    SIZING_EQUITY_ENV,
    SIZING_EQUITY_MODE_ENV,
)
from alphalens_pipeline.brokers.saxo.client import (
    _LIVE_URL_MARKERS,
    LIVE_ACCOUNT_KEY_ENV,
    LIVE_ORDERS_UNLOCK_ENV,
    LIVE_STANDING_ENV,
    SIM_BASE_URL,
    SaxoClient,
    SaxoLiveEnvironmentBlockedError,
)
from alphalens_pipeline.brokers.saxo.tokens import TOKEN_ENV
from alphalens_pipeline.data.alt_data.saxo_marketdata_client import LIVE_API_BASE_URL

LIVE_URL = f"https://{_LIVE_URL_MARKERS[0]}"
LIVE_ACCOUNT_KEY = "STANDING-GRANT-SENTINEL-ACCOUNT-7c2e"

# A fully in-bounds §3 boot-assert env — the factory tests below layer the
# grant pair (LIVE_STANDING_ENV / LIVE_ACCOUNT_KEY_ENV) on top of this so
# rail failures never mask what each test is actually pinning.
_VALID_RAIL_ENV: dict[str, str] = {
    MAX_OPEN_ENV: "1",
    PORTFOLIO_GROSS_FRAC_ENV: "0.25",
    DAILY_LOSS_LIMIT_R_ENV: "1.0",
    SIZING_EQUITY_ENV: "10000",
    SIZING_EQUITY_MODE_ENV: "clamped",
    EXIT_POLICY_ENV: "trailing_atr",
    MAX_FEE_BPS_ENV: "100",
    ENTRY_TRAIL_BPS_ENV: "0",
    ENTRY_WATCH_MAX_PICKS_ENV: "2",
}


class _AnyTokenProvider:
    def get_access_token(self) -> str:
        return "tok"

    def invalidate(self) -> None:
        pass


class _SentinelTokenProvider:
    """Provider whose token value must NEVER appear in any log output."""

    TOKEN = "SECRET-LIVE-TOKEN-SENTINEL-9f3a"

    def get_access_token(self) -> str:
        return self.TOKEN

    def invalidate(self) -> None:
        pass


def _clear_live_env() -> None:
    for var in (LIVE_ORDERS_UNLOCK_ENV, LIVE_STANDING_ENV, LIVE_ACCOUNT_KEY_ENV):
        os.environ.pop(var, None)


def _utc_today() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")


class TestStandingGrantRefusalCases(unittest.TestCase):
    """keyword=True alone, or the grant alone, must never unlock LIVE."""

    def test_keyword_true_grant_absent_raises(self):
        with mock.patch.dict("os.environ"):
            _clear_live_env()
            with self.assertRaises(SaxoLiveEnvironmentBlockedError):
                SaxoClient(_AnyTokenProvider(), base_url=LIVE_URL, standing_live_authorized=True)

    def test_keyword_true_grant_mismatched_raises(self):
        with mock.patch.dict(
            "os.environ",
            {LIVE_STANDING_ENV: "wrong-value", LIVE_ACCOUNT_KEY_ENV: LIVE_ACCOUNT_KEY},
        ):
            os.environ.pop(LIVE_ORDERS_UNLOCK_ENV, None)
            with self.assertRaises(SaxoLiveEnvironmentBlockedError):
                SaxoClient(_AnyTokenProvider(), base_url=LIVE_URL, standing_live_authorized=True)

    def test_keyword_true_grant_pair_empty_strings_raises(self):
        with mock.patch.dict(
            "os.environ",
            {LIVE_STANDING_ENV: "", LIVE_ACCOUNT_KEY_ENV: ""},
        ):
            os.environ.pop(LIVE_ORDERS_UNLOCK_ENV, None)
            with self.assertRaises(SaxoLiveEnvironmentBlockedError):
                SaxoClient(_AnyTokenProvider(), base_url=LIVE_URL, standing_live_authorized=True)

    def test_keyword_false_grant_set_raises(self):
        """The grant alone, without the constructor keyword, is not a capability."""
        with mock.patch.dict(
            "os.environ",
            {LIVE_STANDING_ENV: LIVE_ACCOUNT_KEY, LIVE_ACCOUNT_KEY_ENV: LIVE_ACCOUNT_KEY},
        ):
            os.environ.pop(LIVE_ORDERS_UNLOCK_ENV, None)
            with self.assertRaises(SaxoLiveEnvironmentBlockedError):
                SaxoClient(_AnyTokenProvider(), base_url=LIVE_URL, standing_live_authorized=False)

    def test_refusal_message_never_echoes_grant_value(self):
        with mock.patch.dict(
            "os.environ",
            {LIVE_STANDING_ENV: "wrong-value", LIVE_ACCOUNT_KEY_ENV: LIVE_ACCOUNT_KEY},
        ):
            os.environ.pop(LIVE_ORDERS_UNLOCK_ENV, None)
            with self.assertRaises(SaxoLiveEnvironmentBlockedError) as captured:
                SaxoClient(_AnyTokenProvider(), base_url=LIVE_URL, standing_live_authorized=True)
        message = str(captured.exception)
        self.assertIn("ADR 0017", message)
        self.assertNotIn("wrong-value", message)
        self.assertNotIn(LIVE_ACCOUNT_KEY, message)


class TestStandingGrantUnlock(unittest.TestCase):
    """keyword=True + a matching grant pair — the standing capability."""

    def test_matching_grant_constructs_and_warns_without_grant_value(self):
        provider = _SentinelTokenProvider()
        with mock.patch.dict(
            "os.environ",
            {LIVE_STANDING_ENV: LIVE_ACCOUNT_KEY, LIVE_ACCOUNT_KEY_ENV: LIVE_ACCOUNT_KEY},
        ):
            os.environ.pop(LIVE_ORDERS_UNLOCK_ENV, None)
            with self.assertLogs(
                "alphalens_pipeline.brokers.saxo.client", level="WARNING"
            ) as captured:
                client = SaxoClient(provider, base_url=LIVE_URL, standing_live_authorized=True)
        self.assertEqual(client._base_url, LIVE_URL)
        joined = "\n".join(captured.output)
        self.assertIn(LIVE_URL, joined, "the unlock warning must name the base URL")
        self.assertIn("standing (ADR 0017)", joined)
        self.assertNotIn(
            _SentinelTokenProvider.TOKEN, joined, "token material must never be logged"
        )
        self.assertNotIn(LIVE_ACCOUNT_KEY, joined, "the grant value must never be logged")


class TestFromEnvUnconditionallySimEvenWithGrant(unittest.TestCase):
    """The grant pair being set never widens ``from_env`` — unconditional SIM immunity."""

    def test_from_env_ignores_standing_grant_still_sim_only(self):
        with (
            mock.patch.dict(
                "os.environ",
                {
                    TOKEN_ENV: "tok",
                    LIVE_STANDING_ENV: LIVE_ACCOUNT_KEY,
                    LIVE_ACCOUNT_KEY_ENV: LIVE_ACCOUNT_KEY,
                },
            ),
            mock.patch(
                "alphalens_pipeline.brokers.saxo.client.resolve_token_store_path",
                return_value=Path("/nonexistent/token_store.json"),
            ),
        ):
            os.environ.pop(LIVE_ORDERS_UNLOCK_ENV, None)
            os.environ.pop("SAXO_ENV", None)
            client = SaxoClient.from_env()
        self.assertEqual(client._base_url, SIM_BASE_URL)


class TestCapabilitiesAreIndependent(unittest.TestCase):
    """The ADR 0015 attended path still works with the standing keyword False —
    proving the two unlock mechanisms are independent."""

    def test_attended_day_bound_unlock_alone_still_constructs(self):
        with mock.patch.dict("os.environ", {LIVE_ORDERS_UNLOCK_ENV: _utc_today()}):
            os.environ.pop(LIVE_STANDING_ENV, None)
            os.environ.pop(LIVE_ACCOUNT_KEY_ENV, None)
            client = SaxoClient(
                _AnyTokenProvider(), base_url=LIVE_URL, standing_live_authorized=False
            )
        self.assertEqual(client._base_url, LIVE_URL)


class TestStandingEnvConstantsExist(unittest.TestCase):
    """Positive control (parity for later PR-B/PR-C consumers): the two new
    env-var NAMES exist and are distinct — never a LIVE URL literal."""

    def test_constants_are_distinct_env_var_names(self):
        self.assertEqual(LIVE_STANDING_ENV, "ALPHALENS_SAXO_LIVE_STANDING")
        self.assertEqual(LIVE_ACCOUNT_KEY_ENV, "SAXO_LIVE_ACCOUNT_KEY")
        self.assertNotEqual(LIVE_STANDING_ENV, LIVE_ACCOUNT_KEY_ENV)


class TestCreateSaxoBrokerLiveFromEnvRefusals(unittest.TestCase):
    """``create_saxo_broker_live_from_env`` (design memo §2) refusal paths —
    every case below raises BEFORE any network call, and the grant-mismatch
    case raises before :class:`SaxoClient` is even constructed."""

    def test_missing_account_key_raises_keyerror(self):
        """§3 rails pass; SAXO_LIVE_ACCOUNT_KEY absent — a loud KeyError,
        never a silent fallback onto the SIM account key."""
        from alphalens_pipeline.brokers.saxo import broker as broker_mod

        env = dict(_VALID_RAIL_ENV)
        env.pop(LIVE_ACCOUNT_KEY_ENV, None)
        env.pop(LIVE_STANDING_ENV, None)
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(KeyError):
                broker_mod.create_saxo_broker_live_from_env()

    def test_grant_mismatch_raises_before_client_construction(self):
        """§3 rails pass; the grant pair is set but MISMATCHED — refused
        before SaxoClient is constructed at all."""
        from alphalens_pipeline.brokers.saxo import broker as broker_mod

        env = dict(
            _VALID_RAIL_ENV,
            **{LIVE_ACCOUNT_KEY_ENV: LIVE_ACCOUNT_KEY, LIVE_STANDING_ENV: "wrong-value"},
        )
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(broker_mod, "SaxoClient") as mock_client_cls,
        ):
            with self.assertRaises(SaxoLiveEnvironmentBlockedError):
                broker_mod.create_saxo_broker_live_from_env()
        mock_client_cls.assert_not_called()

    def test_rail_violation_raises_before_account_key_is_even_read(self):
        """§3 rails fail (e.g. MAX_OPEN unset) — refused by assert_live_rails
        even though the grant pair itself is a perfect match, and before
        SaxoClient is constructed."""
        from alphalens_pipeline.brokers.saxo import broker as broker_mod
        from broker_contract.contract import BrokerCapabilityError

        env = dict(_VALID_RAIL_ENV)
        env.pop(MAX_OPEN_ENV, None)
        env[LIVE_ACCOUNT_KEY_ENV] = LIVE_ACCOUNT_KEY
        env[LIVE_STANDING_ENV] = LIVE_ACCOUNT_KEY
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(broker_mod, "SaxoClient") as mock_client_cls,
        ):
            with self.assertRaises(BrokerCapabilityError):
                broker_mod.create_saxo_broker_live_from_env()
        mock_client_cls.assert_not_called()


class TestCreateSaxoBrokerLiveFromEnvSuccess(unittest.TestCase):
    """The happy path — every constructor mocked, exact kwargs asserted, no
    network call and no real credential anywhere in this test."""

    def test_full_valid_env_wires_exact_kwargs_default_alert(self):
        """No ``alert`` passed by the caller -> the adapter gets ``alert=None``
        (its own journald-only default), exactly like the pre-PR-B factory."""
        from alphalens_pipeline.brokers.saxo import broker as broker_mod

        env = dict(
            _VALID_RAIL_ENV,
            **{LIVE_ACCOUNT_KEY_ENV: LIVE_ACCOUNT_KEY, LIVE_STANDING_ENV: LIVE_ACCOUNT_KEY},
        )
        underlying_sentinel = object()
        provider_sentinel = object()
        cfg_sentinel = object()
        broker_sentinel = object()
        client_sentinel = object()
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(broker_mod, "SaxoClient") as mock_client_cls,
            mock.patch.object(broker_mod, "SaxoBroker") as mock_broker_cls,
            mock.patch.object(broker_mod, "LiveAuthConfig") as mock_cfg_cls,
            mock.patch.object(broker_mod, "LiveTokenProvider") as mock_provider_cls,
            mock.patch.object(broker_mod, "LiveOrderTokenProvider") as mock_adapter_cls,
        ):
            mock_cfg_cls.from_env.return_value = cfg_sentinel
            mock_provider_cls.return_value = underlying_sentinel
            mock_adapter_cls.return_value = provider_sentinel
            mock_client_cls.return_value = client_sentinel
            mock_broker_cls.return_value = broker_sentinel

            result = broker_mod.create_saxo_broker_live_from_env()

        mock_cfg_cls.from_env.assert_called_once_with()
        mock_provider_cls.assert_called_once_with(cfg_sentinel)
        mock_adapter_cls.assert_called_once_with(underlying_sentinel, alert=None)
        mock_client_cls.assert_called_once_with(
            provider_sentinel,
            base_url=LIVE_API_BASE_URL,
            standing_live_authorized=True,
        )
        mock_broker_cls.assert_called_once_with(client_sentinel, account_key=LIVE_ACCOUNT_KEY)
        self.assertEqual(result, (broker_sentinel, provider_sentinel))

    def test_injected_alert_threads_into_the_adapter(self):
        """The composition root's ``chain_loss_notify`` reaches the adapter
        verbatim (design memo §2) — the factory never builds its own sink."""
        from alphalens_pipeline.brokers.saxo import broker as broker_mod

        env = dict(
            _VALID_RAIL_ENV,
            **{LIVE_ACCOUNT_KEY_ENV: LIVE_ACCOUNT_KEY, LIVE_STANDING_ENV: LIVE_ACCOUNT_KEY},
        )
        underlying_sentinel = object()
        alert_sentinel = mock.Mock()
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(broker_mod, "SaxoClient"),
            mock.patch.object(broker_mod, "SaxoBroker"),
            mock.patch.object(broker_mod, "LiveAuthConfig") as mock_cfg_cls,
            mock.patch.object(broker_mod, "LiveTokenProvider") as mock_provider_cls,
            mock.patch.object(broker_mod, "LiveOrderTokenProvider") as mock_adapter_cls,
        ):
            mock_cfg_cls.from_env.return_value = object()
            mock_provider_cls.return_value = underlying_sentinel

            broker_mod.create_saxo_broker_live_from_env(alert=alert_sentinel)

        mock_adapter_cls.assert_called_once_with(underlying_sentinel, alert=alert_sentinel)


if __name__ == "__main__":
    unittest.main()
