"""Read the client's PositionNettingProfile (Phase 0.4 preflight).

First-fill experiment (docs/research/saxo_first_fill_experiment_2026_07_18.md):
GET ``/port/v1/clients/{ClientKey}`` via the canonical ``SaxoClient.get_json``
escape hatch (read-only). The profile value branches every Phase-C
closed-positions expectation:

- ``FifoRealTime`` / ``AverageRealTime``: the FIFO pair appears on
  ``/port/v1/closedpositions`` within minutes of the close fill;
- ``FifoEndOfDay``: BOTH offsetting positions sit open (NetPosition Square)
  and closedpositions stays EMPTY until exchange EOD — documented behavior,
  NOT a failure; schedule the next-US-morning verification.
"""

from __future__ import annotations

import argparse
from typing import Any

from _common import dump_payload, scratch_dir


def _make_default_client() -> Any:
    """Late-bound factory so hermetic tests never construct a real client."""
    from alphalens_pipeline.brokers.saxo.client import get_default_saxo_client

    return get_default_saxo_client()


def main(argv: list[str] | None = None, *, client: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-name", default="01_client_profile", help="Scratch dump basename.")
    parser.add_argument("--scratch", default=None, help="Scratch dir (default: $SCRATCH).")
    args = parser.parse_args(argv)

    client = client or _make_default_client()
    scratch = scratch_dir(args.scratch)
    client_key = str(client.get_client_info()["ClientKey"])
    payload = client.get_json(f"/port/v1/clients/{client_key}")
    dump_payload(scratch, args.out_name, payload)
    print(f"PositionNettingProfile: {payload.get('PositionNettingProfile')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
