"""§3.1 AV ``reportTime`` spot-check CLI — paradigm-14 PEAD v2 launch gate #3.

Thin wrapper over
``alphalens_research.screeners.event_drift.av_report_time_validation`` (all logic
+ tests live there). Reads the five §3.1 anchor tickers from the AV cache,
compares the coerced ``report_time`` the engine consumes against curated ground
truth, prints a table, optionally writes the verdict JSON, and exits non-zero
when the gate FAILs (< 4/5 agree OR any dangerous pre-market mismatch).

Usage::

    python scripts/validate_av_report_time_pit.py \
        [--cache-dir ~/.alphalens/av_cache] [--out verdict.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from alphalens_research.screeners.event_drift.av_earnings_ingestion import (
    AVEarningsAnnouncement,
    load_av_earnings,
)
from alphalens_research.screeners.event_drift.av_report_time_validation import (
    REPORT_TIME_ANCHORS,
    evaluate_report_time_anchors,
)

_DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "av_cache"


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument("--cache-dir", type=Path, default=_DEFAULT_CACHE_DIR)
    ap.add_argument("--out", type=Path, default=None, help="Optional verdict JSON path.")
    # min_reported_date=2017-06-01 (the load_av_earnings default) excludes
    # nothing earlier than the 2018-Q1 anchors, so the default is correct here.
    return ap


def main() -> int:
    args = _build_parser().parse_args()

    loaded: dict[str, list[AVEarningsAnnouncement]] = {}
    for anchor in REPORT_TIME_ANCHORS:
        try:
            loaded[anchor.ticker] = load_av_earnings(anchor.ticker, cache_dir=args.cache_dir)
        except FileNotFoundError:
            loaded[anchor.ticker] = []  # observed=None → benign coverage gap

    result = evaluate_report_time_anchors(loaded)

    print(f"§3.1 AV reportTime spot-check — cache: {args.cache_dir}")
    print(f"{'ticker':<7}{'date':<12}{'expected':<13}{'observed':<13}{'verdict'}")
    for v in result.verdicts:
        if v.dangerous:
            verdict = "DANGEROUS"
        elif v.agrees:
            verdict = "agree"
        elif v.observed is None:
            verdict = "missing"
        else:
            verdict = "benign"
        print(
            f"{v.ticker:<7}{v.reported_date.isoformat():<12}"
            f"{v.expected:<13}{(v.observed or '—'):<13}{verdict}"
        )
    status = "PASS" if result.passed else "FAIL"
    print(
        f"\n{status}: {result.n_agree}/{result.n_total} agree "
        f"(min {result.to_dict()['acceptance_min_agree']}), "
        f"{result.n_dangerous} dangerous"
    )

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        print(f"Wrote verdict to {args.out}")

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
