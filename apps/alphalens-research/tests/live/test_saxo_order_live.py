"""Opt-in live ORDER probe for the Saxo SIM environment (ADR 0014 P2).

Distinct from the read probe (``test_saxo_live.py``, ``SAXO_LIVE_TEST``,
whose docstring pins that it must NEVER grow an order call): this probe is
behind its OWN flag ``SAXO_LIVE_ORDER_TEST=1`` and DOES place one order —
a qty-1 KO @ XNYS limit bracket far below the market (never fills), asserts
the placed lifecycle (OrderIds returned, entry WORKING, visible in the open
orders list), and cancels in ``try/finally`` so a mid-probe failure still
cleans up. SIM fill realism is poor (no matching queue), so the probe asserts
LIFECYCLE only, never fill behaviour.

Deliberately EXCLUDED from ``just probe-live`` and the weekly CI live-probes
job (meta-assertion below scans both) — placing orders stays a consciously
attended, Mac-only act. Requirements:

    SAXO_SIM_TOKEN=<fresh 24h token> \
    ALPHALENS_BROKER_ALLOW_ORDERS=1 \
    SAXO_LIVE_ORDER_TEST=1 \
        .venv/bin/python -m unittest tests.live.test_saxo_order_live -v

The entry limit defaults to $30 (KO trades far above; ~50% below recent
closes) and can be overridden via ``SAXO_LIVE_ORDER_ENTRY`` if KO ever
drifts near it — the probe REFUSES to run if entry/stop/tp lose their
bracket ordering.
"""

from __future__ import annotations

import os
import time
import unittest
import uuid
from pathlib import Path

from tests.live import PermanentProbeError, TransientProbeError, run_probes

_LIVE_ORDER_FLAG = "SAXO_LIVE_ORDER_TEST"
_LIVE = os.environ.get(_LIVE_ORDER_FLAG) == "1"

_ENTRY = float(os.environ.get("SAXO_LIVE_ORDER_ENTRY", "30.0"))
# Child distances LIVE-CALIBRATED 2026-07-17: Saxo SIM rejects related orders
# "TooFarFromEntryOrder" (the first probe run used 0.85x/1.5x and was refused;
# the limit is measured from the ENTRY, not the market). +-5% keeps both
# children inside the allowance while preserving stop < entry < tp — this
# probe verifies lifecycle mechanics, not economics. Env-overridable for
# future recalibration without a code change.
_STOP = _ENTRY * float(os.environ.get("SAXO_LIVE_ORDER_STOP_MULT", "0.95"))
_TAKE_PROFIT = _ENTRY * float(os.environ.get("SAXO_LIVE_ORDER_TP_MULT", "1.05"))

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]


class TestOrderProbeStaysOutOfAutomation(unittest.TestCase):
    """Hermetic meta-assertion (always runs): the order-probe flag must never
    join ``just probe-live`` or the weekly CI live-probes job — order
    placement stays an attended act (mirrors the SIM-rail source-scan
    style)."""

    def test_flag_absent_from_justfile_and_ci_workflows(self):
        scan_targets = [WORKSPACE_ROOT / "justfile"]
        workflows_dir = WORKSPACE_ROOT / ".github" / "workflows"
        scan_targets.extend(sorted(workflows_dir.glob("*.yml")))
        scan_targets.extend(sorted(workflows_dir.glob("*.yaml")))
        self.assertTrue(scan_targets, "expected the justfile + CI workflow files to exist")
        for target in scan_targets:
            with self.subTest(file=str(target.relative_to(WORKSPACE_ROOT))):
                self.assertNotIn(
                    _LIVE_ORDER_FLAG,
                    target.read_text(encoding="utf-8", errors="replace"),
                    f"{target.name} must never set {_LIVE_ORDER_FLAG} — the order "
                    "probe is attended-only by design",
                )


def _classify(exc: Exception) -> Exception:
    msg = str(exc).lower()
    if "429" in msg or "timeout" in msg or "timed out" in msg or "connection" in msg:
        return TransientProbeError(str(exc))
    return PermanentProbeError(f"saxo order probe failed: {exc}")


@unittest.skipUnless(_LIVE, f"set {_LIVE_ORDER_FLAG}=1 to run the live Saxo SIM order probe")
class TestSaxoSimLiveOrder(unittest.TestCase):
    def test_place_assert_working_cancel(self):
        from alphalens_pipeline.brokers.contract import BracketOrderRequest, OrderStatus
        from alphalens_pipeline.brokers.saxo.broker import (
            ALLOW_ORDERS_ENV,
            create_saxo_broker_from_env,
        )

        if os.environ.get(ALLOW_ORDERS_ENV) != "1":
            self.skipTest(
                f"{ALLOW_ORDERS_ENV}=1 not set — the broker-side gate would refuse; "
                "export it explicitly to run the attended order probe"
            )
        if not (_STOP < _ENTRY < _TAKE_PROFIT):
            self.fail(f"probe price geometry broken: stop={_STOP} entry={_ENTRY} tp={_TAKE_PROFIT}")

        broker = create_saxo_broker_from_env()

        def _probe_order_lifecycle() -> None:
            try:
                instrument = broker.resolve_instrument("KO", "XNYS")
                request = BracketOrderRequest(
                    instrument=instrument,
                    side="BUY",
                    quantity=1,
                    entry_limit=_ENTRY,
                    stop_loss=_STOP,
                    take_profit=_TAKE_PROFIT,
                    entry_ttl_days=1,
                    client_request_id=str(uuid.uuid4()),
                )
                placed = broker.place_bracket_order(request)
            except Exception as exc:
                raise _classify(exc) from exc

            try:
                if not placed.entry_order_id:
                    raise PermanentProbeError(f"empty entry OrderId: {placed!r}")
                if len(placed.exit_order_ids) != 2:
                    raise PermanentProbeError(
                        f"expected 2 exit OrderIds (TP + SL), got {placed.exit_order_ids!r}"
                    )
                # LIVE-CALIBRATED 2026-07-17: /port/v1 order views LAG the
                # placement by seconds (a fresh order reads UNKNOWN first) —
                # poll briefly before asserting instead of failing on the race.
                state = broker.get_order(placed.entry_order_id)
                deadline = time.monotonic() + 15.0
                while (
                    state.status not in (OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED)
                    and time.monotonic() < deadline
                ):
                    time.sleep(1.5)
                    state = broker.get_order(placed.entry_order_id)
                if state.status not in (OrderStatus.WORKING, OrderStatus.PARTIALLY_FILLED):
                    raise PermanentProbeError(
                        f"entry {placed.entry_order_id} not WORKING after placement: "
                        f"{state.status} (raw={state.raw_status!r})"
                    )
                open_ids = {s.order_id for s in broker.list_open_orders()}
                if placed.entry_order_id not in open_ids:
                    raise PermanentProbeError(
                        f"entry {placed.entry_order_id} missing from open orders {open_ids}"
                    )
            finally:
                # Cleanup ALWAYS: one entry DELETE cascades to both children.
                broker.cancel_order(placed.entry_order_id)

        run_probes(self, {"trade/orders bracket lifecycle": _probe_order_lifecycle}, label="saxo")


if __name__ == "__main__":
    unittest.main()
