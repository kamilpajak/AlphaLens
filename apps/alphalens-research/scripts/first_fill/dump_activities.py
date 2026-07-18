"""Dump the raw audit-log activities for one order id (O1 capture tool).

First-fill experiment (docs/research/saxo_first_fill_experiment_2026_07_18.md):
``dump_activities.py <order_id> --all`` fetches the FULL paged
``/cs/v1/audit/orderactivities?EntryType=All`` payload through the canonical
``SaxoClient.get_order_activities`` wrapper (read-only, ungated) and archives
it UNTRUNCATED to ``$SCRATCH`` — the FinalFill row shape captured here is the
evidence that confirms or refutes the P3 parser's doc-sourced fill fields
(broker.py ``fill_fields_unverified`` branch).
"""

from __future__ import annotations

import argparse
from typing import Any

from _common import dump_payload, scratch_dir

_ROW_FIELDS = (
    "LogId",
    "ActivityTime",
    "Status",
    "SubStatus",
    "FillAmount",
    "FilledAmount",
    "ExecutionPrice",
    "AveragePrice",
    "ExternalReference",
)


def _make_default_client() -> Any:
    """Late-bound factory so hermetic tests never construct a real client."""
    from alphalens_pipeline.brokers.saxo.client import get_default_saxo_client

    return get_default_saxo_client()


def main(argv: list[str] | None = None, *, client: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("order_id", help="Broker OrderId to dump activities for.")
    parser.add_argument(
        "--all", action="store_true", help="EntryType=All (full history; default: Last row only)."
    )
    parser.add_argument("--out-name", default=None, help="Scratch dump basename.")
    parser.add_argument("--scratch", default=None, help="Scratch dir (default: $SCRATCH).")
    args = parser.parse_args(argv)

    client = client or _make_default_client()
    scratch = scratch_dir(args.scratch)
    entry_type = "All" if args.all else "Last"
    client_key = str(client.get_client_info()["ClientKey"])
    payload = client.get_order_activities(client_key, order_id=args.order_id, entry_type=entry_type)

    out_name = args.out_name or f"activities_{args.order_id}_{entry_type.lower()}"
    dump_payload(scratch, out_name, payload)

    rows = payload.get("Data") or []
    print(f"{len(rows)} activity row(s) for order {args.order_id} (EntryType={entry_type}):")
    for row in sorted(rows, key=lambda r: int(r.get("LogId") or 0)):
        summary = "  ".join(f"{field}={row.get(field)!r}" for field in _ROW_FIELDS)
        print(f"  {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
