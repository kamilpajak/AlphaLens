"""Phase A / D driver: place ONE BUY bracket (or naked probe) on Saxo SIM.

First-fill experiment (docs/research/saxo_first_fill_experiment_2026_07_18.md):

- Phase A: marketable BUY bracket, qty=2, stop + tp children —
  ``step_a_entry.py --qty 2 --entry <e> --stop <s> --tp <t>``
- Phase D: naked qty-10 at-the-touch partial-fill probe —
  ``step_a_entry.py --qty 10 --entry <at-touch> --naked``

All writes go through ``SaxoBroker.place_bracket_order`` (env-gated,
prechecked); the placement is journaled so ``alphalens broker reconcile``
sees it; the request echo + response ids land in ``$SCRATCH``.
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qty", type=int, required=True, help="Shares (2 for A, 10 for D).")
    parser.add_argument("--entry", type=float, required=True, help="Entry limit price.")
    parser.add_argument("--stop", type=float, default=None, help="Stop-loss child price.")
    parser.add_argument("--tp", type=float, default=None, help="Take-profit child price.")
    parser.add_argument(
        "--naked", action="store_true", help="Childless probe: forces stop=None and tp=None."
    )
    parser.add_argument("--ticker", default="KO")
    parser.add_argument("--mic", default="XNYS")
    parser.add_argument("--ttl", type=int, default=1, help="Entry GTD TTL in trading days.")
    parser.add_argument("--brief-date", default=today_iso(), help="Journal brief_date.")
    parser.add_argument("--note", default="first-fill experiment phase A")
    parser.add_argument("--out-name", default="10_entry_place", help="Scratch dump basename.")
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
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.naked and (args.stop is not None or args.tp is not None):
        parser.error("--naked forces stop=None/tp=None; drop --stop/--tp")
    stop = None if args.naked else args.stop
    tp = None if args.naked else args.tp

    broker = broker or make_default_broker()
    scratch = scratch_dir(args.scratch)
    placed = place_and_journal(
        broker,
        ticker=args.ticker,
        mic=args.mic,
        side="BUY",
        qty=args.qty,
        entry=args.entry,
        stop=stop,
        tp=tp,
        ttl=args.ttl,
        brief_date=args.brief_date,
        note=args.note,
        scratch=scratch,
        out_name=args.out_name,
        journal_path=Path(args.journal) if args.journal else None,
    )
    if placed is None:
        return 1
    if not args.no_wait:
        poll_until_entry_absent(broker, placed.entry_order_id, sleep=sleep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
