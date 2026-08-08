"""Live Saxo LIVE market-data probe — opt-in via SAXO_MARKETDATA_LIVE_TEST=1.

Shape-only, NEVER values. Needs the SAXO_LIVE_* env (SAXO_LIVE_APP_KEY,
SAXO_LIVE_APP_SECRET, SAXO_LIVE_AUTH_REDIRECT_URL) and a bootstrapped LIVE
token store (see deploy/systemd/README.md "Saxo LIVE market data"). Asserts:
the session can be elevated to FullTradingAndChat, AAPL resolves to a positive
int uic on XNAS, and a price subscription returns a snapshot whose quote row
carries a Quote block with DelayedByMinutes == 0. A closed market (or any
report of a delayed quote) is TRANSIENT (inconclusive), not a shape break — a
non-elevated / demoted session serves delayed prices by design, and that is
exactly the condition the daemon's own freshness gate exists to catch, not
something this probe should fail on.

WARNING: elevating the session takes real-time data away from any
SaxoTraderGO session the operator has open, and from the production daemon
(``alphalens-broker-manager.service`` with ``ALPHALENS_SAXO_LIVE_PRICES=1``)
if it is running — Saxo permits exactly ONE elevated holder at a time (see
``session_reclaim.py``). Do not run this probe while the daemon is live
without coordinating with the operator first.

Behind its OWN attended flag (``skipUnless``, NON-gating) and deliberately
EXCLUDED from ``just probe-live`` and the weekly CI live-probes job — see
``TestMarketDataProbeStaysOutOfAutomation`` below, mirroring the streaming
probe's pin (``tests.live.test_saxo_stream_live``).

    SAXO_MARKETDATA_LIVE_TEST=1 .venv/bin/python -m unittest tests.live.test_saxo_marketdata_live -v
"""

from __future__ import annotations

import contextlib
import os
import time
import unittest
from pathlib import Path
from typing import Any

from tests.live import PermanentProbeError, TransientProbeError, run_probes

_LIVE_FLAG = "SAXO_MARKETDATA_LIVE_TEST"
_LIVE = os.environ.get(_LIVE_FLAG) == "1"

_TICKER = "AAPL"  # liquid, never delisted, always quotes during XNAS hours
_EXCHANGE_MIC = "XNAS"

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]


class TestMarketDataProbeStaysOutOfAutomation(unittest.TestCase):
    """Hermetic meta-assertion (always runs): the market-data probe flag must
    never join ``just probe-live`` or the weekly CI live-probes job — it
    elevates the single-holder LIVE session and needs a bootstrapped LIVE
    token store, so it is attended-only (mirrors the streaming / OCO / order
    probe pins)."""

    def test_flag_absent_from_justfile_and_ci_workflows(self) -> None:
        scan_targets = [WORKSPACE_ROOT / "justfile"]
        workflows_dir = WORKSPACE_ROOT / ".github" / "workflows"
        scan_targets.extend(sorted(workflows_dir.glob("*.yml")))
        scan_targets.extend(sorted(workflows_dir.glob("*.yaml")))
        self.assertTrue(scan_targets, "expected the justfile + CI workflow files to exist")
        for target in scan_targets:
            with self.subTest(file=str(target.relative_to(WORKSPACE_ROOT))):
                self.assertNotIn(
                    _LIVE_FLAG,
                    target.read_text(encoding="utf-8", errors="replace"),
                    f"{target.name} must never set {_LIVE_FLAG} — the market-data probe "
                    "elevates the single-holder LIVE session and is attended-only by design",
                )


def _classify(exc: Exception) -> Exception:
    """Map a raw network/gateway error to the transient/permanent contract."""
    msg = str(exc).lower()
    if any(tok in msg for tok in ("429", "timeout", "timed out", "connection", "refused", "reset")):
        return TransientProbeError(str(exc))
    return PermanentProbeError(f"saxo marketdata probe failed: {exc}")


@unittest.skipUnless(_LIVE, f"set {_LIVE_FLAG}=1 to run the live Saxo marketdata probe")
class TestSaxoMarketDataLiveProbe(unittest.TestCase):
    def test_elevate_resolve_uic_and_price_subscription_shape(self) -> None:
        from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import (
            LiveAuthConfig,
            LiveTokenProvider,
        )
        from alphalens_pipeline.data.alt_data.saxo_marketdata_client import SaxoMarketDataClient

        # Config resolution is deliberately OUTSIDE the classified probes below:
        # missing env is a misconfiguration, not a flaky network condition, and
        # should fail loudly with LiveAuthConfig.from_env()'s own clear message.
        cfg = LiveAuthConfig.from_env()
        client = SaxoMarketDataClient(token_provider=LiveTokenProvider(cfg))

        context_id = f"mktdataprobe-{os.getpid()}-{int(time.time())}"
        reference_id = "px"
        state: dict[str, Any] = {"uic": None, "subscribed": False}

        def _probe_elevate_session() -> None:
            if not client.elevate_session():
                raise PermanentProbeError(
                    "elevate_session() returned False — check the LIVE app's "
                    "trade-level entitlement, or that the token store is bootstrapped"
                )

        def _probe_resolve_uic() -> None:
            try:
                uic = client.resolve_uic(_TICKER, exchange_mic=_EXCHANGE_MIC)
            except Exception as exc:
                raise _classify(exc) from exc
            if not isinstance(uic, int) or isinstance(uic, bool) or uic <= 0:
                raise PermanentProbeError(
                    f"resolve_uic({_TICKER!r}, exchange_mic={_EXCHANGE_MIC!r}) did not "
                    f"return a positive int uic: {uic!r}"
                )
            state["uic"] = uic

        def _probe_price_subscription() -> None:
            uic = state["uic"]
            if uic is None:
                raise PermanentProbeError(
                    "uic resolution failed earlier — cannot probe the price subscription"
                )
            try:
                snapshot = client.create_price_subscription(
                    context_id=context_id, reference_id=reference_id, uics=[uic]
                )
            except Exception as exc:
                raise _classify(exc) from exc
            state["subscribed"] = True

            rows = snapshot.get("Snapshot", {}).get("Data", [])
            quote_rows = [row for row in rows if isinstance(row, dict) and "Quote" in row]
            if not quote_rows:
                raise PermanentProbeError(
                    f"price subscription snapshot has no row with a Quote block (rows={rows!r})"
                )
            delayed = quote_rows[0]["Quote"].get("DelayedByMinutes")
            if delayed != 0:
                raise TransientProbeError(
                    f"quote reports DelayedByMinutes={delayed!r} — closed market or a "
                    "non-elevated/demoted session (inconclusive, not a shape break)"
                )

        try:
            run_probes(
                self,
                {
                    "elevate_session() -> True": _probe_elevate_session,
                    f"resolve_uic({_TICKER!r}) -> positive int": _probe_resolve_uic,
                    "price subscription -> Quote row, DelayedByMinutes == 0": _probe_price_subscription,
                },
                label="saxo-marketdata",
            )
        finally:
            # Idempotent teardown regardless of how far the probe got — a
            # mid-flow failure must never leak a live subscription.
            if state["subscribed"]:
                with contextlib.suppress(Exception):
                    client.delete_price_subscription(context_id, reference_id)


if __name__ == "__main__":
    unittest.main()
