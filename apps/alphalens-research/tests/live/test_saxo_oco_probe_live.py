"""Opt-in live SIM probes for the OCO / reduce-only exit design (ADR 0014).

These answer the three pivotal open questions from the OCO-exit target design
memo (``docs/research/saxo_oco_exit_target_design_2026_07_21.md`` §2) that gate
the Stage-2 (OCO rung) and Stage-3 (reduce-only) upgrades:

  * **Q1 (gates Stage-2 OCO)** — does a 2-leg SELL OCO on a filled long commit
    the owned qty ONCE (no ``SellOrdersAlreadyExistForOwnedContracts``)?
  * **Q2 (gates Stage-2 OCO)** — does the far OCO ``StopIfTraded`` leg escape
    ``TooFarFromEntryOrder`` while OCO-linked to a near ``Limit`` TP?
  * **Q3a (gates Stage-3 reduce-only)** — does the SIM cash-equity account
    REJECT a SELL exceeding owned (so a raw ``uic + Amount`` stop cannot fire
    into a short), or does it accept it (→ ``PositionId``-linked reduce-only is
    required)?

**Answered by PRECHECK, not placement.** ``/trade/v2/orders/precheck`` reserves
nothing yet already surfaces both failure codes we care about
(``SellOrdersAlreadyExist`` reproduced Bug B read-only; ``TooFarFromEntryOrder``
is a validation reject) — so the probe never rests a live OCO on the account.
The only real orders are a 1-share Market open + a Market close, both cleaned up
in ``finally``. A place+read-back+cancel confirmation stays the final per-
instrument gate before OCO is enabled (not done here — precheck is the screen).

Deliberately behind its OWN attended flag ``SAXO_LIVE_OCO_PROBE=1`` (it opens a
real position, like the order probe) and EXCLUDED from ``just probe-live`` + the
weekly CI live-probes job (the always-run meta-assertion below pins that). It
also needs ``ALPHALENS_BROKER_ALLOW_ORDERS=1``. Requirements:

    SAXO_SIM_TOKEN=<fresh 24h token> \
    ALPHALENS_BROKER_ALLOW_ORDERS=1 \
    SAXO_LIVE_OCO_PROBE=1 \
        .venv/bin/python -m unittest tests.live.test_saxo_oco_probe_live -v

The probe instrument defaults to KO @ XNYS (liquid, ~1 share of market risk for
seconds); override with ``SAXO_LIVE_OCO_TICKER`` / ``SAXO_LIVE_OCO_EXCHANGE``.
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

_LIVE_FLAG = "SAXO_LIVE_OCO_PROBE"
_LIVE = os.environ.get(_LIVE_FLAG) == "1"

_TICKER = os.environ.get("SAXO_LIVE_OCO_TICKER", "KO")
_EXCHANGE = os.environ.get("SAXO_LIVE_OCO_EXCHANGE", "XNYS")

# TP is near (+3.6%, an in-band exit); the disaster stop is deep (-27%, the wide
# shape fact #3 proved places standalone). The whole point of Q2 is to confirm
# the deep stop survives being OCO-linked to the near TP.
_TP_MULT = 1.036
_STOP_MULT = 0.73
# Oversell margin for Q3a: precheck a SELL this many shares ABOVE owned.
_OVERSELL_MARGIN = 5

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]


class TestOcoProbeStaysOutOfAutomation(unittest.TestCase):
    """Hermetic meta-assertion (always runs): the OCO-probe flag must never join
    ``just probe-live`` or the weekly CI live-probes job — it opens a real
    position, so it stays an attended act (mirrors the order-probe pin)."""

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
                    f"{target.name} must never set {_LIVE_FLAG} — the OCO probe "
                    "opens a real position and is attended-only by design",
                )


def _classify(exc: Exception) -> Exception:
    """Map a raw client/adapter error to the transient/permanent contract."""
    msg = str(exc).lower()
    if "429" in msg or "timeout" in msg or "timed out" in msg or "connection" in msg:
        return TransientProbeError(str(exc))
    return PermanentProbeError(f"saxo oco probe failed: {exc}")


def _oco_leg(
    *, uic: int, account_key: str, qty: float, order_type: str, price: float
) -> dict[str, Any]:
    """One SELL leg of a standalone OCO pair (OrderRelation=Oco, no parent)."""
    return {
        "Uic": int(uic),
        "AssetType": "Stock",
        "AccountKey": account_key,
        "Amount": qty,
        "BuySell": "Sell",
        "OrderType": order_type,
        "OrderPrice": price,
        "OrderDuration": {"DurationType": "GoodTillCancel"},
        "ManualOrder": False,
        "OrderRelation": "Oco",
    }


@unittest.skipUnless(_LIVE, f"set {_LIVE_FLAG}=1 to run the attended Saxo SIM OCO probe")
class TestSaxoOcoExitProbe(unittest.TestCase):
    def test_oco_and_oversell_prechecks(self):
        from alphalens_pipeline.brokers.contract import OrderRejectedError
        from alphalens_pipeline.brokers.saxo.broker import (
            ALLOW_ORDERS_ENV,
            create_saxo_broker_from_env,
        )

        if os.environ.get(ALLOW_ORDERS_ENV) != "1":
            self.skipTest(
                f"{ALLOW_ORDERS_ENV}=1 not set — export it explicitly to run the "
                "attended OCO probe (it opens a 1-share position)"
            )

        broker = create_saxo_broker_from_env()

        # ---- setup: open ONE real share (Market), poll until /positions shows it ----
        instrument = broker.resolve_instrument(_TICKER, _EXCHANGE)
        uic = int(instrument.broker_instrument_id)
        account_key = broker._resolve_account_key()
        details = broker._client.get_instrument_details(uic, "Stock")
        owned, anchor = _open_one_share_long(broker, uic, account_key)

        try:
            self.assertGreater(owned, 0.0, "setup did not open a long position")
            self.assertGreater(
                anchor, 0.0, "position avg price is non-positive — cannot anchor exits"
            )

            tp_price = broker._quantize_price(anchor * _TP_MULT, details, label="oco_tp")
            stop_price = broker._quantize_price(anchor * _STOP_MULT, details, label="oco_stop")

            def _precheck_result(body: dict[str, Any], label: str) -> OrderRejectedError | None:
                """None == precheck Ok; the error == a genuine reject. Network → transient."""
                try:
                    broker._precheck_or_raise(body, label=label)
                    return None
                except OrderRejectedError as rej:
                    return rej
                except Exception as exc:  # network/gateway → transient, never a Q-answer
                    raise _classify(exc) from exc

            def _probe_oco_precheck() -> None:
                """Q1 + Q2: a 2-leg SELL OCO must precheck Ok (commits owned once,
                far stop escapes the child-distance guard). Tries the documented
                sibling-array shape first, then a top-level+child fallback, so the
                probe also DISCOVERS the shape Saxo SIM accepts (feeds Stage-2
                ``_build_oco_exit_body``)."""
                limit_leg = _oco_leg(
                    uic=uic,
                    account_key=account_key,
                    qty=owned,
                    order_type="Limit",
                    price=tp_price,
                )
                stop_leg = _oco_leg(
                    uic=uic,
                    account_key=account_key,
                    qty=owned,
                    order_type="StopIfTraded",
                    price=stop_price,
                )
                shape_a = {"AccountKey": account_key, "Orders": [limit_leg, stop_leg]}
                shape_b = {**limit_leg, "Orders": [stop_leg]}

                last_reject: OrderRejectedError | None = None
                for shape_name, body in (("siblings-array", shape_a), ("top-level+child", shape_b)):
                    reject = _precheck_result(body, label=f"OCO {shape_name} Uic {uic}")
                    if reject is None:
                        return  # Q1 + Q2 GREEN under this shape → Stage-2 unblocked
                    if reject.error_code == "SellOrdersAlreadyExistForOwnedContracts":
                        raise PermanentProbeError(
                            "Q1 FALSE: a 2-leg SELL OCO double-commits owned "
                            "(SellOrdersAlreadyExist) — the TP is never broker-side "
                            "protection; ship stop-only permanently"
                        )
                    if reject.error_code == "TooFarFromEntryOrder":
                        raise PermanentProbeError(
                            "Q2 FALSE: the far OCO StopIfTraded leg is rejected "
                            "TooFarFromEntryOrder even when OCO-linked to the near TP"
                        )
                    last_reject = reject  # schema/other → try the next shape
                raise PermanentProbeError(
                    "Q1/Q2 INCONCLUSIVE: neither OCO body shape was accepted by "
                    f"Saxo SIM precheck (last reject: {last_reject})"
                )

            def _probe_oversell_blocked() -> None:
                """Q3a: an owned-sized stop must precheck Ok (control), while a
                SELL exceeding owned must be REJECTED (treatment). Acceptance of
                the oversell means raw stops are NOT reduce-only → Stage-3
                PositionId-linked exits are required."""
                control = broker._build_standalone_stop_body(
                    uic, "SELL", owned, stop_price, str(uuid.uuid4()), account_key
                )
                control_reject = _precheck_result(control, label=f"control owned-stop Uic {uic}")
                if control_reject is not None:
                    raise PermanentProbeError(
                        "Q3a control FAILED: an owned-sized stop was rejected "
                        f"({control_reject.error_code}) — the body is invalid, so the "
                        "oversell result would be inconclusive"
                    )
                oversell_qty = owned + _OVERSELL_MARGIN
                oversell = broker._build_standalone_stop_body(
                    uic, "SELL", oversell_qty, stop_price, str(uuid.uuid4()), account_key
                )
                oversell_reject = _precheck_result(
                    oversell, label=f"oversell Uic {uic} qty {oversell_qty}"
                )
                if oversell_reject is None:
                    raise PermanentProbeError(
                        f"Q3a: Saxo SIM ACCEPTED a SELL of {oversell_qty} while owning "
                        f"{owned} — raw uic+Amount stops are NOT reduce-only; Stage-3 "
                        "PositionId-linked reduce-only exits are REQUIRED"
                    )
                # rejected as expected → account cannot oversell into a short

            run_probes(
                self,
                {
                    "trade OCO 2-leg precheck commits owned once (Q1+Q2)": _probe_oco_precheck,
                    "trade oversell precheck is blocked (Q3a shortability)": _probe_oversell_blocked,
                },
                label="saxo-oco",
            )
        finally:
            _flatten(broker, uic, account_key, owned)


def _open_one_share_long(broker: Any, uic: int, account_key: str) -> tuple[float, float]:
    """Market-buy 1 share, poll /positions until it appears. Returns (owned, avg_price)."""
    body = {
        "Uic": int(uic),
        "AssetType": "Stock",
        "AccountKey": account_key,
        "Amount": 1,
        "BuySell": "Buy",
        "OrderType": "Market",
        "OrderDuration": {"DurationType": "DayOrder"},
        "ManualOrder": False,
        "ExternalReference": f"oco-probe-open-{uuid.uuid4()}",
    }
    try:
        broker._client.place_order(body, request_id=str(uuid.uuid4()))
    except Exception as exc:
        raise _classify(exc) from exc

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        pos = broker.get_positions_by_uic(uic)
        if pos.quantity > 0.5:
            return pos.quantity, float(pos.avg_price or 0.0)
        time.sleep(1.5)
    raise PermanentProbeError(
        f"Market buy of Uic {uic} did not show a long in /positions within 20s — "
        "cannot run the OCO probe"
    )


def _flatten(broker: Any, uic: int, account_key: str, owned: float) -> None:
    """Best-effort cleanup: cancel working orders on the uic, then Market-close.

    Never masks the probe result — a cleanup failure prints a LOUD manual-action
    line (SIM position may linger) instead of raising over the probe outcome.
    """
    try:
        for order in broker.list_open_orders():
            if getattr(order, "uic", None) == uic and order.order_id:
                with contextlib.suppress(Exception):  # best-effort cleanup
                    broker.cancel_order(order.order_id)
        live = broker.get_positions_by_uic(uic)
        if live.quantity > 0.5:
            close = {
                "Uic": int(uic),
                "AssetType": "Stock",
                "AccountKey": account_key,
                "Amount": live.quantity,
                "BuySell": "Sell",
                "OrderType": "Market",
                "OrderDuration": {"DurationType": "DayOrder"},
                "ManualOrder": False,
                "ExternalReference": f"oco-probe-close-{uuid.uuid4()}",
            }
            broker._client.place_order(close, request_id=str(uuid.uuid4()))
    except Exception as exc:  # surface, do not mask the probe result
        print(
            f"\n*** OCO PROBE CLEANUP FAILED for Uic {uic} (owned~={owned}): {exc}\n"
            f"*** MANUAL ACTION: Market-close Uic {uic} on the SIM account.\n"
        )


if __name__ == "__main__":
    unittest.main()
