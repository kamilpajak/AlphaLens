"""§10.3 planning-sd read for the exit-policy pre-registration (#1115).

Governing document: ``docs/research/exit_policy_comparison_prereg_2026_08_24.md``
(LOCKED), §3.4 / §6.4 / §10.3. Where a clause and this file disagree, the
clause wins.

Over the HISTORICAL span only (brief_date <= 2026-08-23, enforced — the span
end is a hard ceiling, not a default), replay both §5.2 arms per feasible
candidate in net USD and output **sd_d, the pair count and the cluster counts,
and nothing else**. §3.4's enforcement clause is blunt: if this guard is
bypassed — a mean, a median, a sign, or any per-row difference reaches the
operator — the historical read is a look and the memo's slot is forfeit. The
payload is built by :func:`build_payload`, whose key set is pinned by a test,
and per-row differences never leave this process.

The operator reads the JSON on stdout and nothing else; the resulting
``sd_d`` goes into the cohort-open amendment (§13 item 2) together with
``Delta_min`` and the §6.4 pair floor.

Usage::

    .venv/bin/python apps/alphalens-research/scripts/exit_policy_planning_sd.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from alphalens_pipeline.feedback.population_ladder_monitor import (
    _bars_cache_path,
    _engine_cutoffs,
)
from alphalens_research.diagnostics.exit_policy_replay import (
    ARM_A,
    ARM_B,
    infeasibility_reason,
    replay_arm,
)

logger = logging.getLogger(__name__)

# Historical span, frozen by the memo (§3.4). The END is a hard ceiling: a
# later date would let cohort rows into the planning read.
SPAN_START = "2026-05-19"
SPAN_END = "2026-08-23"

# Planning economics (§5.4): the intended live-frame notional and the primary
# slippage. The amendment records the FINAL N0 with its inputs; the §6.4 floor
# is nearly invariant to the exact figure because sd_d and Delta_min both
# scale with it (modulo the per-fill minimum's kink).
PLANNING_N0_USD = 3750.0
PLANNING_SLIPPAGE_BPS = 40.0

EXCHANGE = "XNYS"

STORE_DIR = Path.home() / ".alphalens" / "population_ladders"
BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"


def enforce_span(span_start: str, span_end: str) -> None:
    """Refuse any span outside the memo's frozen historical window."""
    if span_end > SPAN_END:
        raise SystemExit(
            f"span end {span_end} is past the historical ceiling {SPAN_END}; "
            "cohort rows must never enter the planning read (memo section 3.4)"
        )
    if span_start < SPAN_START:
        raise SystemExit(
            f"span start {span_start} predates the historical span {SPAN_START}; "
            "the planning read covers the memo's span, not pre-history"
        )
    if span_start > span_end:
        raise SystemExit("span start is after span end")


def build_payload(
    *,
    diffs_by_key: dict[tuple[str, str], float],
    excluded: dict[str, int],
    n0: float,
    slippage_bps: float,
    read_ts: str,
) -> dict:
    """The ONLY output shape this script may produce (§10.3).

    ``diffs_by_key`` maps ``(brief_date, ticker)`` to ``d_i`` — consumed here
    and reduced to sd + counts; no per-row value, mean, median or sign leaves
    this function. The key set is pinned by a test.
    """
    values = list(diffs_by_key.values())
    sd = statistics.stdev(values) if len(values) >= 2 else None
    return {
        "sd_d_usd": sd,
        "n_pairs": len(values),
        "n_days": len({day for day, _ in diffs_by_key}),
        "n_tickers": len({ticker for _, ticker in diffs_by_key}),
        "excluded_by_reason": dict(sorted(excluded.items())),
        "n0_usd": n0,
        "slippage_bps": slippage_bps,
        "read_ts_utc": read_ts,
        "span": [SPAN_START, SPAN_END],
        "note": (
            "planning read only (memo section 3.4/6.4): sd + counts, deliberately "
            "no mean, no median, no sign, no per-row differences"
        ),
    }


def _setup_and_pct(brief: pd.DataFrame, ticker: str) -> tuple[dict | None, float | None]:
    match = brief[brief["ticker"] == ticker]
    if match.empty:
        return None, None
    row = match.iloc[0]
    setup_col = next((c for c in brief.columns if "trade_setup" in c), None)
    setup = row[setup_col] if setup_col else None
    if isinstance(setup, str):
        try:
            setup = json.loads(setup)
        except (ValueError, TypeError):
            setup = None
    pct = row.get("technical_pct_off_52w_high")
    try:
        pct = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct = None
    return (setup if isinstance(setup, dict) else None), pct


def collect_diffs(
    span_start: str, span_end: str, *, n0: float, slippage_bps: float
) -> tuple[dict[tuple[str, str], float], dict[str, int]]:
    """Both-arm net-cash differences per feasible historical candidate.

    Feasibility is the §5.1 rule via :func:`infeasibility_reason`; rule 4
    (bars cover the window) is checked against the cached path reaching the
    position-expiry cutoff. Exclusions are counted by reason.
    """
    diffs: dict[tuple[str, str], float] = {}
    excluded: dict[str, int] = {}

    def count(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for store_file in sorted(STORE_DIR.glob("*.parquet")):
        brief_date = store_file.stem
        if not (span_start <= brief_date <= span_end):
            continue
        store = pd.read_parquet(store_file, columns=["ticker"])
        brief_file = BRIEFS_DIR / f"{brief_date}.parquet"
        brief = pd.read_parquet(brief_file) if brief_file.exists() else pd.DataFrame()
        for ticker in store["ticker"]:
            setup, pct = (_setup_and_pct(brief, ticker)) if not brief.empty else (None, None)
            (
                arrival_session,
                _entry_expiry_session,
                _position_expiry_session,
                _entry_ttl,
                _position_ttl,
                entry_expiry_ms,
                position_expiry_ms,
            ) = _engine_cutoffs(dt.date.fromisoformat(brief_date), setup or {}, EXCHANGE)
            bars_path = _bars_cache_path(STORE_DIR, ticker, arrival_session)
            bars: list[dict] = []
            if bars_path.exists():
                frame = pd.read_parquet(bars_path)
                if {"t", "l", "h", "c"}.issubset(frame.columns):
                    bars = frame[["t", "l", "h", "c"]].to_dict("records")
            covers = bool(bars) and max(int(b["t"]) for b in bars) >= position_expiry_ms
            reason = infeasibility_reason(setup, bars_cover_window=covers)
            if reason is not None:
                count(reason)
                continue
            outcomes = {
                arm: replay_arm(
                    setup,  # type: ignore[arg-type]  # feasibility guarantees a dict
                    bars,
                    arm=arm,
                    notional=n0,
                    slippage_bps=slippage_bps,
                    entry_expiry_ms=entry_expiry_ms,
                    position_expiry_ms=position_expiry_ms,
                    pct_off_52w_high=pct,
                )
                for arm in (ARM_A, ARM_B)
            }
            diffs[(brief_date, ticker)] = outcomes[ARM_B].net_cash - outcomes[ARM_A].net_cash
    return diffs, excluded


def main() -> int:
    parser = argparse.ArgumentParser(
        description="exit-policy planning-sd read (memo section 6.4 / 10.3)"
    )
    parser.add_argument("--span-start", default=SPAN_START)
    parser.add_argument("--span-end", default=SPAN_END)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    enforce_span(args.span_start, args.span_end)

    read_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    diffs, excluded = collect_diffs(
        args.span_start,
        args.span_end,
        n0=PLANNING_N0_USD,
        slippage_bps=PLANNING_SLIPPAGE_BPS,
    )
    if len(diffs) < 2:
        # The section 6.4 floor needs a defined sd; an ambiguous null in the
        # cohort-open amendment would be a planning-time forfeit.
        print(
            f"only {len(diffs)} feasible pair(s) — sd_d undefined; refusing to emit",
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            build_payload(
                diffs_by_key=diffs,
                excluded=excluded,
                n0=PLANNING_N0_USD,
                slippage_bps=PLANNING_SLIPPAGE_BPS,
                read_ts=read_ts,
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
