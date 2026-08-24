"""Measure ladder outcomes conditional on WHICH entry tiers filled (issue #1113).

READ-ONLY and OFFLINE. Reads the population-ladder store, the brief parquets and
the monitor's own minute-bar cache; makes no Polygon call and writes nothing back
into ``~/.alphalens``. The analysis itself is
``alphalens_research.diagnostics.fill_partition`` (pure, no I/O); this file only
loads and prints.

This ships the INSTRUMENT, not a finding. The comparison it feeds -- whether
deep-only fills do worse than first-tier fills -- is pre-registered separately in
issue #1115, so nothing here decides anything and the payload deliberately
carries no verdict field.

Three choices worth knowing before reading a number out of it:

* The filled-tier set is re-derived from the CACHED MINUTE BARS, not from the
  store's ``sequence_str``. That column is order-only: a path where E2 fills in
  the same minute as E1 and one where it fills three weeks later produce the
  identical string.
* Fills are priced with a stated overshoot arm (``--overshoot-arm``), never at
  the bare tier limit. The measured arm rests on ONE live round trip, which is
  why the zero-bps and ceiling arms exist beside it.
* Only DECIDED rows count. An ongoing position can still fill a deeper tier, so
  counting it now would understate every conditional rate (the immortal-time
  trap). Undecided rows are still reported, in their own exclusion bucket.

Usage::

    .venv/bin/python apps/alphalens-research/scripts/measure_fill_partition.py
    .venv/bin/python apps/alphalens-research/scripts/measure_fill_partition.py --json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pandas as pd
from alphalens_pipeline.feedback.population_ladder_monitor import (
    _engine_cutoffs,
    _filter_bars_to_rth,
    _read_cached_bars,
)
from alphalens_pipeline.paper.brief_loader import load_brief
from alphalens_research.diagnostics import fill_partition as fp

EXCHANGE = "XNYS"
PAYLOAD_SCHEMA = "fill_partition/1"

DEFAULT_STORE_DIR = Path.home() / ".alphalens" / "population_ladders"
DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"

# Sub-causes behind the ``no_replay`` exclusion bucket. Reported separately so a
# reader can tell a missing brief from a missing bar cache; both still land in
# ONE bucket so the denominator identity holds.
COVERAGE_KEYS = (
    "dates_total",
    "dates_without_a_brief",
    "rows_without_a_brief_row",
    "rows_without_entry_tiers",
    "rows_without_cached_bars",
    "rows_with_empty_bars_after_rth",
)

# Statements a reader needs in order not to misread a cell. Kept as data so the
# envelope carries them and they cannot drift out of the payload.
PAYLOAD_NOTES = (
    "deep_only is empty in the offline replay by construction: the bar walk fills"
    " every tier a bar's low reaches, and tiers descend in price, so only prefix"
    " fill sets can occur. The live rail CAN produce a deep-only set, because"
    " issue #1112 refuses to arm a shallow tier whose own take-profit target sits"
    " at or below a realistic fill.",
    "The unfilled partition has no realised return because no capital was"
    " deployed. Read it against forgone_return, which is the market-excess move"
    " over the same window.",
    "The overshoot arm rests on a single live round trip (SMG, 2026-08-24). Run"
    " the limit and ceiling arms beside it before reading any level as settled.",
    "Both re-anchored numbers are withheld on a non-terminal row, where the"
    " stop-recovery identity they use does not hold.",
)


def _iter_store_dates(store_dir: Path) -> list[dt.date]:
    if not store_dir.exists():
        return []
    return sorted(dt.date.fromisoformat(p.stem) for p in store_dir.glob("????-??-??.parquet"))


def _setups_by_ticker(briefs_dir: Path, date: dt.date) -> dict[str, dict | None] | None:
    """The brief's trade setups keyed by upper-case ticker, or ``None`` if absent.

    A candidate whose ``brief_trade_setup`` did not decode is kept with a ``None``
    value rather than omitted, so the caller can tell "this ticker was not in the
    brief" from "its brief row carried no usable setup" -- two different coverage
    stories that would otherwise collapse into one count.
    """
    try:
        candidates = load_brief(date, briefs_dir)
    except (FileNotFoundError, ValueError, OSError):
        return None
    return {c.ticker.upper(): c.trade_setup for c in candidates}


def _bars_for(store_dir: Path, ticker: str, date: dt.date, setup: dict) -> tuple[list, int]:
    """RTH-filtered cached bars plus the entry-expiry cutoff. Never fetches."""
    cutoffs = _engine_cutoffs(date, setup, EXCHANGE)
    arrival, _entry_expiry_session, position_expiry_session = cutoffs[0], cutoffs[1], cutoffs[2]
    entry_expiry_ms = cutoffs[5]
    raw = _read_cached_bars(store_dir, ticker, arrival)
    if not raw:
        return [], entry_expiry_ms
    bars = _filter_bars_to_rth(raw, arrival, position_expiry_session, EXCHANGE)
    return bars, entry_expiry_ms


def _unreplayable(row: dict, coverage: dict[str, int], key: str) -> fp.Opportunity:
    """An opportunity we could not re-replay: counted, never dropped."""
    coverage[key] += 1
    return fp.Opportunity(
        brief_date=str(row.get("brief_date")),
        ticker=str(row.get("ticker")),
        excluded_reason=fp.EXCLUDE_NO_REPLAY,
        filled_tiers=(),
        fill_bar_ts_ms=(),
        filled_fraction=0.0,
        realised_return=None,
        forgone_return=fp.finite_or_none(row.get("market_excess_return")),
        holding_days=None,
        mae_r=None,
    )


def collect_opportunities(
    *,
    store_dir: Path,
    briefs_dir: Path,
    fill_model: str,
    overshoot_arm: str,
) -> tuple[list[fp.Opportunity], dict[str, int]]:
    """Walk the store and turn every row into exactly one :class:`fp.Opportunity`.

    The list is the WHOLE store slice: a row we cannot re-replay comes back with
    an exclusion reason rather than being skipped, so the report's denominator
    identity holds against the raw file count.
    """
    if overshoot_arm not in fp.OVERSHOOT_ARMS_BPS:
        raise ValueError(f"unknown overshoot_arm {overshoot_arm!r}")
    overshoot_bps = fp.OVERSHOOT_ARMS_BPS[overshoot_arm]

    coverage = dict.fromkeys(COVERAGE_KEYS, 0)
    opportunities: list[fp.Opportunity] = []

    for date in _iter_store_dates(store_dir):
        coverage["dates_total"] += 1
        frame = pd.read_parquet(store_dir / f"{date.isoformat()}.parquet")
        setups = _setups_by_ticker(briefs_dir, date)
        if setups is None:
            coverage["dates_without_a_brief"] += 1
            setups = {}
        for row in frame.to_dict("records"):
            opportunities.append(
                _opportunity_for_row(
                    row,
                    date=date,
                    setups=setups,
                    store_dir=store_dir,
                    coverage=coverage,
                    fill_model=fill_model,
                    overshoot_bps=overshoot_bps,
                )
            )
    return opportunities, coverage


def _opportunity_for_row(
    row: dict,
    *,
    date: dt.date,
    setups: dict[str, dict | None],
    store_dir: Path,
    coverage: dict[str, int],
    fill_model: str,
    overshoot_bps: float,
) -> fp.Opportunity:
    ticker = str(row.get("ticker", "")).upper()
    if ticker not in setups:
        return _unreplayable(row, coverage, "rows_without_a_brief_row")
    setup = setups[ticker]
    tiers = fp.entry_tiers_from_setup(setup)
    if not tiers or setup is None:
        return _unreplayable(row, coverage, "rows_without_entry_tiers")
    bars, entry_expiry_ms = _bars_for(store_dir, ticker, date, setup)
    if not bars:
        key = (
            "rows_without_cached_bars"
            if not _read_cached_bars(store_dir, ticker, _engine_cutoffs(date, setup, EXCHANGE)[0])
            else "rows_with_empty_bars_after_rth"
        )
        return _unreplayable(row, coverage, key)
    fills = fp.walk_entry_fills(
        tiers,
        bars,
        fill_model=fill_model,
        overshoot_bps=overshoot_bps,
        entry_expiry_ms=entry_expiry_ms,
    )
    return fp.opportunity_from_store_row(row, fills=fills, tiers=tiers, overshoot_bps=overshoot_bps)


def build_report(
    opportunities: list[fp.Opportunity], *, fill_model: str, overshoot_arm: str
) -> fp.PartitionReport:
    return fp.partition_report(opportunities, fill_model=fill_model, overshoot_arm=overshoot_arm)


def report_payload(
    report: fp.PartitionReport, *, coverage: dict[str, int], generated_at: str
) -> dict:
    """The JSON envelope. One value, self-describing, no verdict field."""
    return {
        "schema": PAYLOAD_SCHEMA,
        "generated_at": generated_at,
        "inputs": {
            "fill_model": report.fill_model,
            "overshoot_arm": report.overshoot_arm,
            "overshoot_bps": report.overshoot_bps,
            "exchange": EXCHANGE,
        },
        "denominator": {
            "n_store_rows": report.n_store_rows,
            "n_opportunities": report.n_opportunities,
            "excluded": dict(report.excluded),
        },
        "coverage": dict(coverage),
        "partitions": [asdict(p) for p in report.partitions],
        "conditional_fills": [{**asdict(r), "rate": r.rate} for r in report.conditional_fills],
        "notes": list(PAYLOAD_NOTES),
    }


def _render_human(payload: dict) -> str:
    lines = [
        f"fill-partition read ({payload['generated_at']})",
        (
            f"  fill model {payload['inputs']['fill_model']} | overshoot arm "
            f"{payload['inputs']['overshoot_arm']} "
            f"({payload['inputs']['overshoot_bps']:.2f} bps)"
        ),
        (
            f"  store rows {payload['denominator']['n_store_rows']} | opportunities "
            f"{payload['denominator']['n_opportunities']}"
        ),
        "  excluded: "
        + ", ".join(f"{k}={v}" for k, v in payload["denominator"]["excluded"].items()),
        "",
    ]
    for cell in payload["partitions"]:
        flag = " [unreachable offline]" if cell["offline_unreachable"] else ""
        lines.append(f"  {cell['partition']:<12} n={cell['n']:<5}{flag}")
        lines.append(
            f"      realised n={cell['n_realised']} missing={cell['n_missing_realised']}"
            f" | forgone n={cell['n_forgone']} missing={cell['n_missing_forgone']}"
        )
    lines.append("")
    for rec in payload["conditional_fills"]:
        lines.append(
            f"  P({rec['then_tier']} | {rec['given_tier']}): n_given={rec['n_given']}"
            f" n_then={rec['n_then']} (same bar {rec['n_then_same_bar']},"
            f" later {rec['n_then_later']})"
        )
    lines.append("")
    lines.extend(f"  note: {n}" for n in payload["notes"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", default=str(DEFAULT_STORE_DIR))
    ap.add_argument("--briefs", default=str(DEFAULT_BRIEFS_DIR))
    ap.add_argument("--fill-model", choices=list(fp.FILL_MODELS), default=fp.FILL_MODEL_TOUCH)
    ap.add_argument(
        "--overshoot-arm", choices=list(fp.OVERSHOOT_ARMS), default=fp.OVERSHOOT_ARM_MEASURED
    )
    ap.add_argument("--json", action="store_true", help="emit the payload as one JSON value")
    args = ap.parse_args(argv)

    opportunities, coverage = collect_opportunities(
        store_dir=Path(args.store),
        briefs_dir=Path(args.briefs),
        fill_model=args.fill_model,
        overshoot_arm=args.overshoot_arm,
    )
    report = build_report(
        opportunities, fill_model=args.fill_model, overshoot_arm=args.overshoot_arm
    )
    payload = report_payload(
        report, coverage=coverage, generated_at=dt.datetime.now(dt.UTC).isoformat()
    )
    print(json.dumps(payload) if args.json else _render_human(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
