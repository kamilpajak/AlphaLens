"""Opt-in live SIM probe for the Stage-3 PATCH-amend resize primitive.

Answers the amend half of the Stage-3 design's "Live SIM probes before
enabling each flag" gate (``docs/research/saxo_stage3_oco_amend_design_2026_07_23.md``)
— the checks that must pass on the SIM account BEFORE
``ALPHALENS_BROKER_AMEND_ENABLED=1`` is flipped:

  * **PATCH amend UP** — a live resting standalone ``StopIfTraded`` amended
    from ``Amount=1`` to ``Amount=2`` returns HTTP 200, echoes the SAME
    ``OrderId``, reads back at the new Amount, and never leaves the book.
  * **PATCH amend DOWN** — the same stop amended back down (over -> owned)
    returns 200, ``OrderId`` preserved, reads back at the reduced Amount, the
    order stays WORKING throughout.

Unlike the OCO probe (which answers Q1/Q2/Q3a by ``precheck``, reserving
nothing), the amend probe MUST rest a real order: there is NO PATCH precheck
endpoint (design memo §"AmendStop" — "NO precheck"), so the only way to prove
the resize preserves ``OrderId`` and takes effect is to place a live stop and
read it back. The real orders are a small Market open, ONE standalone stop, and
a Market close — all cleaned up in ``finally``.

**Shape only, never values.** The probe asserts the returned ``OrderId`` equals
the resting ``OrderId`` and that the read-back ``Amount`` equals the target we
just set (a consistency check that the amend took effect) — NEVER a market
price or an owned quantity read from live state.

Deliberately behind its OWN attended flag ``SAXO_LIVE_AMEND_PROBE=1`` (it opens
a real position, like the OCO / order probes) and EXCLUDED from
``just probe-live`` + the weekly CI live-probes job — the always-run
meta-assertion below pins that (mirrors ``TestOcoProbeStaysOutOfAutomation``).

``create_saxo_broker_from_env()`` builds an ``OAuthTokenProvider`` from the FULL
Saxo OAuth env — ``SAXO_APP_KEY`` / ``SAXO_APP_SECRET`` /
``SAXO_AUTH_REDIRECT_URL`` (+ the on-disk token store), NOT a bare bearer token
— so run this where that env + a valid refreshable token store already live: the
VPS with ``/etc/alphalens/env`` sourced (the ``alphalens-saxo-refresh`` timer
keeps the token fresh). It also needs ``ALPHALENS_BROKER_ALLOW_ORDERS=1`` (both
already in ``/etc/alphalens/env``). Pause ``alphalens-broker-manager`` first so
the daemon does not react to the probe's un-journaled long / resting stop.
Recipe::

    cd apps/alphalens-research && set -a && . /etc/alphalens/env && set +a && \
    SAXO_LIVE_AMEND_PROBE=1 ALPHALENS_BROKER_ALLOW_ORDERS=1 \
        ../../.venv/bin/python -m unittest tests.live.test_saxo_amend_probe_live -v

The probe instrument defaults to KO @ XNYS (liquid, ~2 shares of market risk for
seconds); override with ``SAXO_LIVE_AMEND_TICKER`` / ``SAXO_LIVE_AMEND_EXCHANGE``.
"""

from __future__ import annotations

import contextlib
import os
import time
import unittest
import uuid
from pathlib import Path
from typing import Any

from tests.live import PermanentProbeError, TransientProbeError, run_probes

_LIVE_FLAG = "SAXO_LIVE_AMEND_PROBE"
_LIVE = os.environ.get(_LIVE_FLAG) == "1"

_TICKER = os.environ.get("SAXO_LIVE_AMEND_TICKER", "KO")
_EXCHANGE = os.environ.get("SAXO_LIVE_AMEND_EXCHANGE", "XNYS")

# The disaster stop is deep (-27%, the wide shape fact #3 proved places
# standalone) so it never triggers during the probe's few seconds of life.
_STOP_MULT = 0.73
# Own this many shares so the amend-UP target (2) never oversells the cash
# account (Q3a: a SELL exceeding owned is rejected). The stop starts at 1.
_OWN_QTY = 2
_STOP_START_QTY = 1

# Saxo rejects an ExternalReference longer than 50 chars (InvalidModelState);
# place_standalone_stop uses its request_id as the ExternalReference, so the
# stop ref must be compact. The amend body carries NO ExternalReference (only an
# x-request-id header), but the refs stay compact + distinct anyway.
_SAXO_EXTERNAL_REF_MAX = 50
_QTY_EPS = 0.5

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]


def _probe_ref(tag: str) -> str:
    """A Saxo-safe ExternalReference (<= 50 chars) for a probe order."""
    return f"amend-probe-{tag}-{uuid.uuid4().hex}"


class TestAmendProbeStaysOutOfAutomation(unittest.TestCase):
    """Hermetic meta-assertion (always runs): the amend-probe flag must never
    join ``just probe-live`` or the weekly CI live-probes job — it opens a real
    position and rests a live order, so it stays an attended act (mirrors the
    OCO / order-probe pins)."""

    def test_flag_absent_from_justfile_and_ci_workflows(self):
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
                    f"{target.name} must never set {_LIVE_FLAG} — the amend probe "
                    "opens a real position and rests a live order; attended-only by design",
                )


class TestAmendProbeRefWithinSaxoLimit(unittest.TestCase):
    """Hermetic (always runs): probe ExternalReferences must stay <= 50 chars,
    or Saxo rejects the order with InvalidModelState (found live 2026-07-22)."""

    def test_probe_refs_within_limit(self):
        for tag in ("open", "stop", "close"):
            with self.subTest(tag=tag):
                ref = _probe_ref(tag)
                self.assertLessEqual(
                    len(ref),
                    _SAXO_EXTERNAL_REF_MAX,
                    f"probe ref {ref!r} ({len(ref)} chars) exceeds Saxo's "
                    f"{_SAXO_EXTERNAL_REF_MAX}-char ExternalReference limit",
                )


def _classify(exc: Exception) -> Exception:
    """Map a raw client/adapter error to the transient/permanent contract."""
    msg = str(exc).lower()
    if "429" in msg or "timeout" in msg or "timed out" in msg or "connection" in msg:
        return TransientProbeError(str(exc))
    return PermanentProbeError(f"saxo amend probe failed: {exc}")


@unittest.skipUnless(_LIVE, f"set {_LIVE_FLAG}=1 to run the attended Saxo SIM amend probe")
class TestSaxoAmendProbe(unittest.TestCase):
    def test_amend_up_then_down_preserves_orderid(self):
        from alphalens_pipeline.brokers.saxo.broker import (
            ALLOW_ORDERS_ENV,
            create_saxo_broker_from_env,
        )

        if os.environ.get(ALLOW_ORDERS_ENV) != "1":
            self.skipTest(
                f"{ALLOW_ORDERS_ENV}=1 not set — export it explicitly to run the "
                "attended amend probe (it opens a position and rests a live stop)"
            )

        broker = create_saxo_broker_from_env()

        instrument = broker.resolve_instrument(_TICKER, _EXCHANGE)
        uic = int(instrument.broker_instrument_id)
        account_key = broker._resolve_account_key()
        details = broker._client.get_instrument_details(uic, "Stock")

        # The open is INSIDE the try so a fill-lag failure AFTER a placed buy
        # still triggers _flatten (never leave a lingering SIM long / stop).
        try:
            owned, anchor = _open_long(broker, uic, account_key, _OWN_QTY)
            self.assertGreaterEqual(
                owned, _OWN_QTY - _QTY_EPS, "setup did not open the full probe long"
            )
            self.assertGreater(
                anchor, 0.0, "position avg price is non-positive — cannot anchor the stop"
            )

            # Clear any stale working SELL on the uic from a prior aborted run.
            for leg in broker.list_working_sell_orders():
                if getattr(leg, "uic", None) == uic and leg.order_id:
                    with contextlib.suppress(Exception):
                        broker.cancel_order(leg.order_id)

            stop_price = broker._quantize_price(anchor * _STOP_MULT, details, label="amend_stop")

            # ---- rest ONE standalone StopIfTraded at the starting Amount ----
            try:
                placed = broker.place_standalone_stop(
                    uic, "SELL", _STOP_START_QTY, stop_price, _probe_ref("stop")
                )
            except Exception as exc:
                raise _classify(exc) from exc
            order_id = placed.entry_order_id
            self.assertTrue(order_id, "place_standalone_stop returned no OrderId")
            _await_resting_amount(broker, order_id, _STOP_START_QTY)

            def _probe_amend_up() -> None:
                """PATCH the stop Amount UP (1 -> 2): OrderId preserved, reads
                back at the new Amount, order never left the book."""
                _amend_and_verify(self, broker, uic, order_id, stop_price, _OWN_QTY)

            def _probe_amend_down() -> None:
                """PATCH the stop Amount DOWN (2 -> 1): OrderId preserved, reads
                back at the reduced Amount, order stays WORKING throughout."""
                _amend_and_verify(self, broker, uic, order_id, stop_price, _STOP_START_QTY)

            run_probes(
                self,
                {
                    "PATCH amend UP preserves OrderId + reads back": _probe_amend_up,
                    "PATCH amend DOWN preserves OrderId + reads back": _probe_amend_down,
                },
                label="saxo-amend",
            )
        finally:
            _flatten(broker, uic, account_key)


def _amend_and_verify(
    case: unittest.TestCase,
    broker: Any,
    uic: int,
    order_id: str,
    stop_price: float,
    target_qty: int,
) -> None:
    """Amend the resting stop to ``target_qty`` and assert SHAPE only: the
    returned OrderId equals the resting OrderId (non-empty) and the read-back
    Amount equals the target we just set. Never asserts a market value."""
    try:
        result = broker.amend_stop_amount(
            uic,
            order_id,
            "SELL",
            "StopIfTraded",
            target_qty,
            stop_price,
            _amend_request_id(target_qty),
        )
    except Exception as exc:
        raise _classify(exc) from exc

    # SHAPE: the amend preserves the resting order's identity (never re-creates).
    if result.exit_order_ids != (order_id,):
        raise PermanentProbeError(
            f"amend to {target_qty} did NOT preserve OrderId: expected "
            f"exit_order_ids=({order_id!r},), got {result.exit_order_ids!r}"
        )
    case.assertTrue(order_id, "resting OrderId went empty after amend")

    # READBACK: the amend took effect and the order is still WORKING.
    readback = _await_resting_amount(broker, order_id, target_qty)
    case.assertEqual(
        readback.order_id,
        order_id,
        "read-back order carries a different OrderId than the amended one",
    )


def _amend_request_id(target_qty: int) -> str:
    """A DISTINCT x-request-id per amend (monotonic-ish) so Saxo's ~15s
    x-request-id dedup never swallows a genuine second resize (design memo
    mitigation A3/H3). The amend body carries no ExternalReference, so this is
    header-only and not length-constrained like the stop ref."""
    return f"amend-{target_qty}-{uuid.uuid4().hex}"


def _await_resting_amount(broker: Any, order_id: str, expected_qty: int):
    """Poll ``get_order`` until the resting order shows the expected Amount.

    Returns the ``OrderState`` once ``amount ~= expected_qty`` (list-orders
    propagation lags a write by a beat). Raises a transient/permanent probe
    error on timeout so a slow SIM read is not mistaken for an amend failure.
    """
    deadline = time.monotonic() + 15.0
    last: Any = None
    while time.monotonic() < deadline:
        try:
            state = broker.get_order(order_id)
        except Exception as exc:
            raise _classify(exc) from exc
        last = state
        amount = state.amount
        if amount is not None and abs(float(amount) - float(expected_qty)) <= _QTY_EPS:
            return state
        time.sleep(1.0)
    raise PermanentProbeError(
        f"resting order {order_id} did not read back at Amount {expected_qty} within 15s "
        f"(last state: order_id={getattr(last, 'order_id', None)!r}, "
        f"amount={getattr(last, 'amount', None)!r})"
    )


def _open_long(broker: Any, uic: int, account_key: str, qty: int) -> tuple[float, float]:
    """Market-buy ``qty`` shares, poll /positions until they appear. Returns (owned, avg_price)."""
    body = {
        "Uic": int(uic),
        "AssetType": "Stock",
        "AccountKey": account_key,
        "Amount": qty,
        "BuySell": "Buy",
        "OrderType": "Market",
        "OrderDuration": {"DurationType": "DayOrder"},
        "ManualOrder": False,
        "ExternalReference": _probe_ref("open"),
    }
    try:
        status, payload = broker._client.place_order(body, request_id=str(uuid.uuid4()))
    except Exception as exc:
        raise _classify(exc) from exc
    # place_order returns (status, payload) and does NOT raise on 4xx — a rejected
    # buy must surface its reason here, not be mistaken for a fill-lag timeout.
    if status >= 400:
        raise PermanentProbeError(f"Market buy of Uic {uic} rejected (HTTP {status}): {payload}")

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        pos = broker.get_positions_by_uic(uic)
        if pos.quantity >= qty - _QTY_EPS:
            return pos.quantity, float(pos.avg_price or 0.0)
        time.sleep(1.5)
    raise PermanentProbeError(
        f"Market buy of {qty} Uic {uic} did not show in /positions within 20s — "
        "cannot run the amend probe"
    )


def _flatten(broker: Any, uic: int, account_key: str) -> None:
    """Best-effort cleanup: cancel working orders on the uic, then Market-close.

    Reads the live netted qty itself (so it also cleans up a partially-open
    position). Never masks the probe result — a cleanup failure prints a LOUD
    manual-action line (SIM position may linger) instead of raising over the
    probe outcome.
    """
    try:
        for order in broker.list_open_orders():
            if getattr(order, "uic", None) == uic and order.order_id:
                with contextlib.suppress(Exception):  # best-effort cleanup
                    broker.cancel_order(order.order_id)
        live = broker.get_positions_by_uic(uic)
        if live.quantity > _QTY_EPS:
            close = {
                "Uic": int(uic),
                "AssetType": "Stock",
                "AccountKey": account_key,
                "Amount": live.quantity,
                "BuySell": "Sell",
                "OrderType": "Market",
                "OrderDuration": {"DurationType": "DayOrder"},
                "ManualOrder": False,
                "ExternalReference": _probe_ref("close"),
            }
            status, payload = broker._client.place_order(close, request_id=str(uuid.uuid4()))
            # place_order does NOT raise on 4xx — a rejected close must trip the
            # manual-action path below, not silently leave a naked SIM long.
            if status >= 400:
                raise RuntimeError(f"Market-close rejected (HTTP {status}): {payload}")
    except Exception as exc:  # surface, do not mask the probe result
        print(
            f"\n*** AMEND PROBE CLEANUP FAILED for Uic {uic}: {exc}\n"
            f"*** MANUAL ACTION: cancel working orders + Market-close Uic {uic} "
            "on the SIM account.\n"
        )


if __name__ == "__main__":
    unittest.main()
