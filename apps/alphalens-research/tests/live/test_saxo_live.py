"""Opt-in live probe for the Saxo SIM environment (L4; ADR 0014 P1).

Shape-only assertions against the REAL Saxo SIM gateway — keys and
non-emptiness, never values, and NEVER any order call (the client cannot place
orders in P1 anyway; the probe additionally must never grow one in P2+).

Four probes:
  1. ``/port/v1/users/me`` round-trips with a non-empty payload
  2. ``/port/v1/accounts/me`` lists at least one SIM account
  3. instrument resolution AAPL @ XNAS (US coverage)
  4. instrument resolution CDR @ XWAR — the WSE-on-SIM coverage validation the
     design memo flags as the most likely gap; a miss here is a PERMANENT
     finding (it decides the P2 scope), not a flake.

Needs a fresh 24h Developer-Portal SIM token in ``SAXO_SIM_TOKEN`` — the token
expires daily, so this probe is Mac-attended only; it joins the weekly CI
live-probes job only after P4 lands non-expiring OAuth.

Run explicitly (never in CI's blocking path; needs SAXO_SIM_TOKEN + network):

    SAXO_LIVE_TEST=1 .venv/bin/python -m unittest tests.live.test_saxo_live -v
"""

from __future__ import annotations

import os
import unittest

from tests.live import PermanentProbeError, TransientProbeError, run_probes

_LIVE = os.environ.get("SAXO_LIVE_TEST") == "1"


def _classify(exc: Exception) -> Exception:
    """Map a raw client/adapter error to the transient/permanent contract."""
    msg = str(exc).lower()
    if "429" in msg or "timeout" in msg or "timed out" in msg or "connection" in msg:
        return TransientProbeError(str(exc))
    return PermanentProbeError(f"saxo probe failed: {exc}")


@unittest.skipUnless(_LIVE, "set SAXO_LIVE_TEST=1 to run the live Saxo SIM probe")
class TestSaxoSimLive(unittest.TestCase):
    def test_sim_reads_and_instrument_resolution(self):
        from alphalens_pipeline.brokers.saxo.broker import create_saxo_broker_from_env
        from alphalens_pipeline.brokers.saxo.client import get_default_saxo_client
        from broker_contract.contract import InstrumentNotFoundError

        client = get_default_saxo_client()
        broker = create_saxo_broker_from_env()

        def _probe_user() -> None:
            try:
                payload = client.get_user()
            except Exception as exc:
                raise _classify(exc) from exc
            if not payload or not isinstance(payload, dict):
                raise PermanentProbeError(f"users/me returned an empty payload: {payload!r}")
            # Shape-only: the key set must be non-empty; we never assert values.
            if not payload.keys():
                raise PermanentProbeError("users/me payload has no keys")

        def _probe_accounts() -> None:
            try:
                payload = client.get_accounts()
            except Exception as exc:
                raise _classify(exc) from exc
            accounts = payload.get("Data") or []
            if len(accounts) < 1:
                raise PermanentProbeError(
                    f"accounts/me returned no accounts: {sorted(payload.keys())}"
                )
            if "AccountKey" not in accounts[0]:
                raise PermanentProbeError(f"account row missing AccountKey: {sorted(accounts[0])}")

        def _probe_resolve_us() -> None:
            try:
                ref = broker.resolve_instrument("AAPL", "XNAS")
            except Exception as exc:
                raise _classify(exc) from exc
            if not ref.broker_instrument_id:
                raise PermanentProbeError(f"AAPL resolve returned empty Uic: {ref!r}")

        def _probe_resolve_wse() -> None:
            # The WSE-on-SIM coverage validation (design memo open question 3).
            # An InstrumentNotFoundError here is a PERMANENT finding: it means
            # the SIM environment does not serve Warsaw listings and the P2
            # scope must treat XWAR as unverified.
            try:
                ref = broker.resolve_instrument("CDR", "XWAR")
            except InstrumentNotFoundError as exc:
                raise PermanentProbeError(
                    f"WSE-on-SIM coverage gap: CDR @ XWAR did not resolve ({exc}); "
                    "record this in the design memo before locking P2 scope"
                ) from exc
            except Exception as exc:
                raise _classify(exc) from exc
            if not ref.broker_instrument_id:
                raise PermanentProbeError(f"CDR resolve returned empty Uic: {ref!r}")

        def _probe_resolve_xams() -> None:
            # Euronext-Amsterdam-on-SIM coverage (#1238 PR 6) — same stance as
            # the WSE probe: InstrumentNotFoundError is a PERMANENT finding
            # (SIM does not serve XAMS listings), everything else classifies.
            try:
                ref = broker.resolve_instrument("ASML", "XAMS")
            except InstrumentNotFoundError as exc:
                raise PermanentProbeError(
                    f"XAMS-on-SIM coverage gap: ASML @ XAMS did not resolve ({exc})"
                ) from exc
            except Exception as exc:
                raise _classify(exc) from exc
            if not ref.broker_instrument_id:
                raise PermanentProbeError(f"ASML resolve returned empty Uic: {ref!r}")

        def _probe_resolve_xetr() -> None:
            # Xetra-on-SIM coverage (#1271 PR 1) — same stance as the WSE /
            # XAMS probes, plus the alias path: Saxo lists Rheinmetall under
            # the root RHMG, so resolving the MARKET ticker RHM exercises
            # SAXO_TICKER_ALIASES end to end and the uic pin (16135) is the
            # shape assertion.
            try:
                ref = broker.resolve_instrument("RHM", "XETR")
            except InstrumentNotFoundError as exc:
                raise PermanentProbeError(
                    f"XETR-on-SIM coverage gap: RHM @ XETR did not resolve ({exc})"
                ) from exc
            except Exception as exc:
                raise _classify(exc) from exc
            if str(ref.broker_instrument_id) != "16135":
                raise PermanentProbeError(
                    f"RHM alias resolved to unexpected Uic {ref.broker_instrument_id!r} "
                    "(expected the pinned 16135 — Saxo may have renamed RHMG)"
                )

        run_probes(
            self,
            {
                "port/users-me": _probe_user,
                "port/accounts-me": _probe_accounts,
                "resolve AAPL@XNAS": _probe_resolve_us,
                "resolve CDR@XWAR (WSE-on-SIM validation)": _probe_resolve_wse,
                "resolve ASML@XAMS (Euronext-on-SIM validation)": _probe_resolve_xams,
                "resolve RHM@XETR (alias + FSE-on-SIM validation)": _probe_resolve_xetr,
            },
            label="saxo",
        )


if __name__ == "__main__":
    unittest.main()
