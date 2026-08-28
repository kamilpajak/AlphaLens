"""SIM-only structural rail (ADR 0014) — the tests that make LIVE unreachable.

Four independent locks, so no single edit can quietly open a LIVE path:

(a) the constructor refuses every LIVE base URL marker;
(b) ``LIVE_TRADING_ENABLED`` is ``False`` (flipped only by a future ADR);
(c) ``from_env`` with a stray ``SAXO_ENV != sim`` fails loudly (operator .env
    confusion guard — there is deliberately NO env-var switch to LIVE);
(d) no LIVE gateway URL string exists anywhere in the ``brokers`` package
    sources outside the ``_LIVE_URL_MARKERS`` tuple itself.
"""

from __future__ import annotations

import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from alphalens_pipeline.brokers.saxo.client import (
    _LIVE_URL_MARKERS,
    LIVE_ORDERS_UNLOCK_ENV,
    LIVE_TRADING_ENABLED,
    SIM_BASE_URL,
    SaxoClient,
    SaxoLiveEnvironmentBlockedError,
)
from alphalens_pipeline.brokers.saxo.tokens import (
    APP_KEY_ENV,
    APP_SECRET_ENV,
    REDIRECT_URL_ENV,
    TOKEN_ENV,
    TOKEN_STORE_PATH_ENV,
)

# tests/brokers/ is one level deeper than tests/, so the repo root is parents[4].
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
BROKERS_DIR = WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_pipeline" / "brokers"


def _sim_env(token_store: Path) -> dict[str, str]:
    """Minimal SIM environment, used with ``clear=True``.

    Every variable ``from_env`` consults is stated here, so the test asserts
    the same thing on a laptop with real Saxo credentials as it does in CI
    with none (#1176). ``SAXO_TOKEN_STORE_PATH`` is what keeps it away from
    the operator's real token store — both the client-side branch check and
    the provider read the path through it.
    """
    return {
        TOKEN_ENV: "tok",
        TOKEN_STORE_PATH_ENV: str(token_store),
        "SAXO_ENV": "sim",
    }


class _AnyTokenProvider:
    def get_access_token(self) -> str:
        return "tok"

    def invalidate(self) -> None:
        pass


class TestSimOnlyRail(unittest.TestCase):
    def test_constructor_refuses_every_live_url_marker(self):
        for marker in _LIVE_URL_MARKERS:
            live_url = f"https://{marker}"
            with self.subTest(base_url=live_url):
                with self.assertRaises(SaxoLiveEnvironmentBlockedError):
                    SaxoClient(_AnyTokenProvider(), base_url=live_url)

    def test_constructor_refuses_any_non_sim_base_url(self):
        """The guard is equality-to-SIM, not a marker blocklist — a typo'd or
        proxied LIVE URL must be refused too."""
        with self.assertRaises(SaxoLiveEnvironmentBlockedError):
            SaxoClient(_AnyTokenProvider(), base_url="https://example.com/openapi")

    def test_sim_base_url_is_accepted(self):
        client = SaxoClient(_AnyTokenProvider(), base_url=SIM_BASE_URL)
        self.assertIsInstance(client, SaxoClient)

    def test_live_trading_flag_is_false(self):
        self.assertIs(
            LIVE_TRADING_ENABLED,
            False,
            "LIVE_TRADING_ENABLED may only be flipped by a future ADR lifting "
            "the SIM-only rail (see ADR 0014)",
        )

    def test_from_env_with_saxo_env_live_raises(self):
        with mock.patch.dict("os.environ", {TOKEN_ENV: "tok", "SAXO_ENV": "live"}):
            with self.assertRaises(SaxoLiveEnvironmentBlockedError):
                SaxoClient.from_env()

    def test_from_env_with_saxo_env_sim_is_accepted_on_the_static_path(self):
        """No token store on disk → the ``SAXO_SIM_TOKEN`` static provider."""
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "token_store.json"
            with mock.patch.dict("os.environ", _sim_env(absent), clear=True):
                client = SaxoClient.from_env()
                self.assertIsInstance(client, SaxoClient)

    def test_from_env_with_saxo_env_sim_is_accepted_on_the_oauth_path(self):
        """Token store present → the OAuth provider, which needs the app
        credentials. Pinned explicitly because ``from_env`` picks its provider
        from what is on DISK: before #1176 this test read the operator's real
        store and real ``SAXO_APP_KEY``, so it asserted a different branch on
        the VPS than in CI and failed outright when run on its own."""
        with tempfile.TemporaryDirectory() as tmp:
            present = Path(tmp) / "token_store.json"
            present.write_text("{}", encoding="utf-8")
            env = _sim_env(present) | {
                APP_KEY_ENV: "app-key",
                APP_SECRET_ENV: "app-secret",
                REDIRECT_URL_ENV: "http://localhost/callback",
            }
            with mock.patch.dict("os.environ", env, clear=True):
                client = SaxoClient.from_env()
                self.assertIsInstance(client, SaxoClient)

    def test_no_live_url_string_outside_marker_tuple(self):
        self.assertTrue(BROKERS_DIR.is_dir(), f"brokers package not found at {BROKERS_DIR}")
        offenders: list[str] = []
        for py in sorted(BROKERS_DIR.rglob("*.py")):
            for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
                if any(marker in line for marker in _LIVE_URL_MARKERS) and (
                    "_LIVE_URL_MARKERS" not in line
                ):
                    offenders.append(f"{py.name}:{lineno}  {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "LIVE gateway URL strings may exist ONLY inside the _LIVE_URL_MARKERS "
            f"tuple; offenders:\n{chr(10).join(offenders)}",
        )

    def test_marker_tuple_positive_control(self):
        """The source-scan above passes vacuously if the marker tuple rots to
        empty — pin that it still names all three LIVE hosts, including the
        LIVE streaming host this branch introduced."""
        # EXACT membership, deliberately not a substring scan. Two reasons:
        # it is strictly stronger (a marker carrying extra text around the
        # host, e.g. "xx-live-streaming.saxobank.com-yy", passes an `in`
        # scan but fails here; a truncated marker fails either way), and
        # substring-matching a hostname is the shape CodeQL flags as
        # incomplete URL sanitization (py/incomplete-url-substring-
        # sanitization) — harmless against our own constants, but not a
        # pattern worth leaving in a security rail for the next reader.
        self.assertEqual(len(_LIVE_URL_MARKERS), 3)
        self.assertIn("gateway.saxobank.com/openapi", _LIVE_URL_MARKERS)
        self.assertIn("live.logonvalidation.net", _LIVE_URL_MARKERS)
        self.assertIn("live-streaming.saxobank.com", _LIVE_URL_MARKERS)
        for marker in _LIVE_URL_MARKERS:
            self.assertNotIn(marker, SIM_BASE_URL, "a LIVE marker must never match SIM")


class _SentinelTokenProvider:
    """Provider whose token value must NEVER appear in any log output."""

    TOKEN = "SECRET-LIVE-TOKEN-SENTINEL-9f3a"

    def get_access_token(self) -> str:
        return self.TOKEN

    def invalidate(self) -> None:
        pass


class TestKeyedDayBoundUnlock(unittest.TestCase):
    """ADR 0015 — the keyed day-bound unlock: the ONLY exception to lock (a).

    The unlock widens the CONSTRUCTOR guard alone. Locks (b)-(d) are
    untouched, and — the load-bearing pin — the unlock has NO effect on
    ``from_env``: the daemon constructs solely through ``from_env``, so it
    has no code path to LIVE regardless of environment contents.
    """

    LIVE_URL = f"https://{_LIVE_URL_MARKERS[0]}"

    @staticmethod
    def _utc_date(offset_days: int = 0) -> str:
        now = datetime.datetime.now(datetime.UTC)
        return (now + datetime.timedelta(days=offset_days)).strftime("%Y-%m-%d")

    def test_unlock_absent_refuses_live_url(self):
        with mock.patch.dict("os.environ"):
            os.environ.pop(LIVE_ORDERS_UNLOCK_ENV, None)
            with self.assertRaises(SaxoLiveEnvironmentBlockedError):
                SaxoClient(_AnyTokenProvider(), base_url=self.LIVE_URL)

    def test_unlock_with_stale_future_or_garbage_value_refuses(self):
        """Yesterday, tomorrow, and non-date values all refuse — the unlock is
        an exact-match against TODAY's UTC date, computed at construction."""
        for value in (self._utc_date(-1), self._utc_date(+1), "1", "true", ""):
            with self.subTest(value=value):
                with mock.patch.dict("os.environ", {LIVE_ORDERS_UNLOCK_ENV: value}):
                    with self.assertRaises(SaxoLiveEnvironmentBlockedError):
                        SaxoClient(_AnyTokenProvider(), base_url=self.LIVE_URL)

    def test_unlock_with_today_utc_date_accepts_and_warns_loudly(self):
        provider = _SentinelTokenProvider()
        with mock.patch.dict("os.environ", {LIVE_ORDERS_UNLOCK_ENV: self._utc_date()}):
            with self.assertLogs(
                "alphalens_pipeline.brokers.saxo.client", level="WARNING"
            ) as captured:
                client = SaxoClient(provider, base_url=self.LIVE_URL)
        self.assertEqual(client._base_url, self.LIVE_URL)
        joined = "\n".join(captured.output)
        self.assertIn(self.LIVE_URL, joined, "the unlock warning must name the base URL")
        self.assertNotIn(
            _SentinelTokenProvider.TOKEN, joined, "token material must never be logged"
        )

    def test_sim_construction_needs_no_unlock(self):
        with mock.patch.dict("os.environ"):
            os.environ.pop(LIVE_ORDERS_UNLOCK_ENV, None)
            client = SaxoClient(_AnyTokenProvider(), base_url=SIM_BASE_URL)
            self.assertIsInstance(client, SaxoClient)

    def test_from_env_ignores_unlock_still_sim_only(self):
        """THE daemon-isolation pin: ``from_env`` with the unlock set still
        returns a SIM client — no factory path can ever produce LIVE."""
        with (
            mock.patch.dict(
                "os.environ",
                {TOKEN_ENV: "tok", LIVE_ORDERS_UNLOCK_ENV: self._utc_date()},
            ),
            mock.patch(
                "alphalens_pipeline.brokers.saxo.client.resolve_token_store_path",
                return_value=Path("/nonexistent/token_store.json"),
            ),
        ):
            client = SaxoClient.from_env()
            self.assertEqual(client._base_url, SIM_BASE_URL)

    def test_from_env_saxo_env_live_still_raises_with_unlock_set(self):
        """Lock (c) survives the unlock: SAXO_ENV=live fails loudly even with
        a valid unlock in the environment."""
        with mock.patch.dict(
            "os.environ",
            {
                TOKEN_ENV: "tok",
                "SAXO_ENV": "live",
                LIVE_ORDERS_UNLOCK_ENV: self._utc_date(),
            },
        ):
            with self.assertRaises(SaxoLiveEnvironmentBlockedError):
                SaxoClient.from_env()


if __name__ == "__main__":
    unittest.main()
