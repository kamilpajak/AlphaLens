"""Opt-in live probe for the Saxo SIM P3 reconciliation reads (L4; ADR 0014).

Shape-only assertions against the REAL Saxo SIM gateway for the two reads the
reconciler depends on — the audit order-activities store and the
closed-positions view. STRICTLY READ-ONLY: no order call exists in this file
(reconcile makes no writes), so it shares the existing ``SAXO_LIVE_TEST``
flag rather than the attended-only ``SAXO_LIVE_ORDER_TEST`` one. It is a
SEPARATE file so ``test_saxo_live.py``'s four-probe / never-grow-an-order
docstring pin stays intact.

Two probes:
  1. ``/cs/v1/audit/orderactivities`` for a known SIM order id (2026-07-17's
     cancelled bracket entry 5039272886) — envelope keys + per-row
     (Status, SubStatus) presence. ``__count == 0`` is TOLERATED: account
     churn or retention aging removes the pinned id eventually, and an empty
     result is a valid envelope, so the probe must not rot into a permanent
     failure (values are never asserted either way).
  2. ``/port/v1/closedpositions`` — the wrapper must normalize BOTH live
     body shapes (bare ``[]`` on an empty account, ``{__count, Data}``
     envelope otherwise) into the envelope form.

Run explicitly (never in CI's blocking path; needs SAXO_SIM_TOKEN + network):

    SAXO_LIVE_TEST=1 .venv/bin/python -m unittest tests.live.test_saxo_reconcile_live -v
"""

from __future__ import annotations

import os
import unittest

from tests.live import PermanentProbeError, TransientProbeError, run_probes

_LIVE = os.environ.get("SAXO_LIVE_TEST") == "1"

# The cancelled bracket entry from the 2026-07-17 SIM calibration session
# (terminal row Cancelled/Confirmed, LogId 249474866). Aging out of the
# audit window yields __count == 0, which the probe tolerates by design.
_KNOWN_ORDER_ID = "5039272886"


def _classify(exc: Exception) -> Exception:
    """Map a raw client/adapter error to the transient/permanent contract."""
    msg = str(exc).lower()
    if "429" in msg or "timeout" in msg or "timed out" in msg or "connection" in msg:
        return TransientProbeError(str(exc))
    return PermanentProbeError(f"saxo reconcile probe failed: {exc}")


@unittest.skipUnless(_LIVE, "set SAXO_LIVE_TEST=1 to run the live Saxo SIM probe")
class TestSaxoReconcileLive(unittest.TestCase):
    def test_audit_activities_and_closed_positions_shapes(self):
        from alphalens_pipeline.brokers.saxo.client import get_default_saxo_client

        client = get_default_saxo_client()
        client_key = str(client.get_client_info()["ClientKey"])

        def _probe_order_activities() -> None:
            try:
                payload = client.get_order_activities(client_key, order_id=_KNOWN_ORDER_ID)
            except Exception as exc:
                raise _classify(exc) from exc
            if not isinstance(payload, dict) or "Data" not in payload:
                raise PermanentProbeError(
                    f"orderactivities envelope missing Data: {sorted(payload)!r}"
                )
            rows = payload.get("Data") or []
            # __count == 0 tolerated (retention / account churn) — but any
            # row that IS present must carry the classifier's (Status,
            # SubStatus) pair. Shape only; values never asserted.
            for row in rows:
                for key in ("Status", "SubStatus", "OrderId"):
                    if key not in row:
                        raise PermanentProbeError(
                            f"orderactivities row missing {key}: {sorted(row)!r}"
                        )

        def _probe_closed_positions() -> None:
            try:
                payload = client.get_closed_positions(client_key)
            except Exception as exc:
                raise _classify(exc) from exc
            # The wrapper must have normalized either live body shape
            # (bare [] or envelope) into the envelope form.
            if not isinstance(payload, dict) or not isinstance(payload.get("Data"), list):
                raise PermanentProbeError(
                    f"closedpositions not normalized to an envelope: {payload!r}"
                )

        run_probes(
            self,
            {
                "cs/audit/orderactivities (known order id)": _probe_order_activities,
                "port/closedpositions (both body shapes)": _probe_closed_positions,
            },
            label="saxo-reconcile",
        )


if __name__ == "__main__":
    unittest.main()
