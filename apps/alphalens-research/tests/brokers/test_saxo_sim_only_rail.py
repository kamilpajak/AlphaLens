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

import unittest
from pathlib import Path
from unittest import mock

from alphalens_pipeline.brokers.saxo.client import (
    _LIVE_URL_MARKERS,
    LIVE_TRADING_ENABLED,
    SIM_BASE_URL,
    SaxoClient,
    SaxoLiveEnvironmentBlockedError,
)
from alphalens_pipeline.brokers.saxo.tokens import TOKEN_ENV

# tests/brokers/ is one level deeper than tests/, so the repo root is parents[4].
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
BROKERS_DIR = WORKSPACE_ROOT / "apps" / "alphalens-pipeline" / "alphalens_pipeline" / "brokers"


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

    def test_from_env_with_saxo_env_sim_is_accepted(self):
        with mock.patch.dict("os.environ", {TOKEN_ENV: "tok", "SAXO_ENV": "sim"}):
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
        empty — pin that it still names both LIVE hosts."""
        self.assertEqual(len(_LIVE_URL_MARKERS), 2)
        self.assertTrue(any("openapi" in m for m in _LIVE_URL_MARKERS))
        self.assertTrue(any("logonvalidation" in m for m in _LIVE_URL_MARKERS))
        for marker in _LIVE_URL_MARKERS:
            self.assertNotIn(marker, SIM_BASE_URL, "a LIVE marker must never match SIM")


if __name__ == "__main__":
    unittest.main()
