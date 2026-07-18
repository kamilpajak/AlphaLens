"""Phase C driver: manual opposite close — naked SELL limit on Saxo SIM.

First-fill experiment (docs/research/saxo_first_fill_experiment_2026_07_18.md):

- Default: ``step_c_close.py --qty 2 --limit <ref*0.99>`` places the naked
  (stop=None, tp=None) SELL close whose journal record must reconcile to an
  honest ``r=None`` (compute_realized_r None-by-design without a stop).
- Contingency (Saxo rejects the childless body): re-run with
  ``--stop <round(entry*1.03,2)>`` — the tested stop-only path (stop ABOVE
  entry is valid SELL geometry). After it fills, the orphan StopIfTraded
  child MUST be cancelled immediately (it would otherwise OPEN a short).

All writes go through ``SaxoBroker.place_bracket_order``; journaled;
payloads land in ``$SCRATCH``.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from _common import (
    make_default_broker,
    place_and_journal,
    poll_until_entry_absent,
    scratch_dir,
    today_iso,
)

_DEFAULT_NOTE = "first-fill experiment close; stop=None expected r=None"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qty", type=int, required=True, help="EXACT open position qty.")
    parser.add_argument("--limit", type=float, required=True, help="Marketable SELL limit.")
    parser.add_argument(
        "--stop",
        type=float,
        default=None,
        help="CONTINGENCY ONLY: stop-only close (stop above entry); cancel the orphan post-fill.",
    )
    parser.add_argument("--ticker", default="KO")
    parser.add_argument("--mic", default="XNYS")
    parser.add_argument("--ttl", type=int, default=1, help="Entry GTD TTL in trading days.")
    parser.add_argument("--brief-date", default=today_iso(), help="Journal brief_date.")
    parser.add_argument("--note", default=_DEFAULT_NOTE)
    parser.add_argument("--out-name", default="30_close_place", help="Scratch dump basename.")
    parser.add_argument("--scratch", default=None, help="Scratch dir (default: $SCRATCH).")
    parser.add_argument("--journal", default=None, help="Journal path override (tests only).")
    parser.add_argument(
        "--no-wait", action="store_true", help="Skip the 60s disappeared-from-open-orders poll."
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    broker: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = _build_parser().parse_args(argv)
    broker = broker or make_default_broker()
    scratch = scratch_dir(args.scratch)
    placed = place_and_journal(
        broker,
        ticker=args.ticker,
        mic=args.mic,
        side="SELL",
        qty=args.qty,
        entry=args.limit,
        stop=args.stop,
        tp=None,
        ttl=args.ttl,
        brief_date=args.brief_date,
        note=args.note,
        scratch=scratch,
        out_name=args.out_name,
        journal_path=Path(args.journal) if args.journal else None,
    )
    if placed is None:
        return 1
    if args.stop is not None and placed.exit_order_ids:
        print(
            "CONTINGENCY PATH: after the close fills, IMMEDIATELY cancel the orphan "
            f"stop child {','.join(placed.exit_order_ids)} (it would otherwise OPEN a short): "
            f"alphalens broker cancel {placed.exit_order_ids[0]}"
        )
    if not args.no_wait:
        poll_until_entry_absent(broker, placed.entry_order_id, sleep=sleep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
