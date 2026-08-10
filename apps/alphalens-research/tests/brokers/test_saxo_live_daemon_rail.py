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

LIVE_URL = f"https://{_LIVE_URL_MARKERS[0]}"
LIVE_ACCOUNT_KEY = "STANDING-GRANT-SENTINEL-ACCOUNT-7c2e"


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


if __name__ == "__main__":
    unittest.main()
