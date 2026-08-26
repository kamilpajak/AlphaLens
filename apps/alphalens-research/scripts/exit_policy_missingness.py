"""§10.2 missingness audit over the historical span (#1115, memo §7).

Governing document: ``docs/research/exit_policy_comparison_prereg_2026_08_24.md``
(LOCKED). Where a clause and this file disagree, the clause wins.

Re-runs the historical bracket-lens constructibility with reason capture over
the cached bars, builds the §7.1 flow table, and runs the §7.2
missingness-vs-outcome diagnostic (day-clustered). Descriptive only: nothing
here computes the A-vs-B contrast, and the historical span carries no verdict
words (§3.4).

Reads the production ladder store and the briefs parquets READ-ONLY; the
artifact goes to a dedicated directory (never the ladder store — the module's
guard refuses it). Dry-run by default; ``--write`` persists the classified
rows + the flow table beside a recorded read timestamp.

Usage::

    .venv/bin/python apps/alphalens-research/scripts/exit_policy_missingness.py
    .venv/bin/python apps/alphalens-research/scripts/exit_policy_missingness.py --write
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from alphalens_research.diagnostics.exit_policy_missingness import (
    classify_and_verify,
    day_cluster_mean_diff,
    ensure_artifact_dir,
    flow_table,
    stored_bracket_null,
)

logger = logging.getLogger(__name__)

# Historical span, frozen by the memo (§3.4/§7.1): NOT in the forward sample.
SPAN_START = "2026-05-19"
SPAN_END = "2026-08-23"

STORE_DIR = Path.home() / ".alphalens" / "population_ladders"
BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"
ARTIFACT_DIR = Path.home() / ".alphalens" / "exit_policy_prereg"


def _setup_from_brief(brief: pd.DataFrame, ticker: str) -> tuple[dict | None, float | None]:
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


def _bars_for(ticker: str, brief_date: str) -> list[dict]:
    """The cached bar path for THIS row's arrival.

    Bars files are keyed ``TICKER_<arrival-date>``, and the arrival lags the
    brief date by 0-3 calendar days (weekends/holidays) — measured on the real
    cache: 249 exact, 56 at +1, 48 at +2..3. The search window is
    [brief_date, brief_date+5], EARLIEST match wins. Deliberately no fallback
    to other dates: bars from a different brief of the same ticker would walk
    a foreign window and misclassify this row — a missing cache entry is an
    honest ``no_bars``.
    """
    from datetime import date, timedelta

    start = date.fromisoformat(brief_date)
    for offset in range(6):
        candidate = (
            STORE_DIR / "bars"
        ) / f"{ticker}_{(start + timedelta(days=offset)).isoformat()}.parquet"
        if candidate.exists():
            frame = pd.read_parquet(candidate)
            if {"t", "l", "h", "c"}.issubset(frame.columns):
                return frame[["t", "l", "h", "c"]].to_dict("records")
            return []
    return []


def classify_span(span_start: str, span_end: str) -> pd.DataFrame:
    rows: list[dict] = []
    for store_file in sorted(STORE_DIR.glob("*.parquet")):
        brief_date = store_file.stem
        if not (span_start <= brief_date <= span_end):
            continue
        store = pd.read_parquet(store_file)
        brief_file = BRIEFS_DIR / f"{brief_date}.parquet"
        brief = pd.read_parquet(brief_file) if brief_file.exists() else pd.DataFrame()
        for _, row in store.iterrows():
            record: dict = {
                "brief_date": brief_date,
                "ticker": row["ticker"],
                "plannable": bool(row.get("plannable", False)),
                "terminal": bool(row.get("terminal", False)),
                "arm_a_present": pd.notna(row.get("realized_r")),
                "ladder_classification": row.get("ladder_classification"),
                "realized_r": row.get("realized_r"),
                "stored_bracket_null": stored_bracket_null(row.get("breakeven_realized_r_json")),
                "arm_b_reason": None,
                "mirror_agrees_with_lens": None,
            }
            if record["plannable"] and record["terminal"] and record["arm_a_present"]:
                # A missing brief file leaves setup=None and classifies
                # honestly (setup_not_ok...) through the SAME verified path —
                # no hand-set reasons.
                setup, pct = (
                    _setup_from_brief(brief, row["ticker"]) if not brief.empty else (None, None)
                )
                bars = _bars_for(row["ticker"], brief_date)
                verdict = classify_and_verify(setup, bars, pct_off_52w_high=pct)
                record["arm_b_reason"] = verdict.reason
                record["mirror_agrees_with_lens"] = verdict.agrees
            rows.append(record)
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="exit-policy missingness audit (memo section 7 / 10.2)"
    )
    parser.add_argument("--span-start", default=SPAN_START)
    parser.add_argument("--span-end", default=SPAN_END)
    parser.add_argument("--write", action="store_true", help="persist the artifact")
    parser.add_argument("--json", action="store_true", help="machine output to stdout")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    read_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    frame = classify_span(args.span_start, args.span_end)
    if frame.empty:
        print("no rows in span — is the ladder store present on this host?", file=sys.stderr)
        return 1

    table = flow_table(frame)
    verified = frame["mirror_agrees_with_lens"].dropna()
    agreement = {
        "rows_verified": len(verified),
        "rows_disagreeing": int((~verified.astype(bool)).sum()),
    }

    diag_rows = frame[frame["plannable"] & frame["terminal"] & frame["arm_a_present"]]
    diagnostic = None
    if not diag_rows.empty and diag_rows["stored_bracket_null"].nunique() > 1:
        result = day_cluster_mean_diff(
            diag_rows,
            indicator="stored_bracket_null",
            y="realized_r",
            day="brief_date",
        )
        diagnostic = {
            "arm_a_mean_diff_null_minus_nonnull": result.diff,
            "ci95_day_cluster": [result.ci_low, result.ci_high],
            "n_null": result.n_indicator,
            "n_nonnull": result.n_rest,
            "n_days": result.n_days,
        }

    payload = {
        "read_ts_utc": read_ts,
        "span": [args.span_start, args.span_end],
        "flow_table": table,
        "mirror_vs_lens_agreement": agreement,
        "missingness_vs_outcome": diagnostic,
        "note": "descriptive only; no verdict words; never the A-vs-B contrast (memo section 3.4/7)",
        "stored_null_caveat": (
            "stored nulls conflate 'lens not yet registered' (before its 2026-07-16 deploy; "
            "forward-only stamping) with 'bracket unconstructible' — the re-run reasons above "
            "describe today's constructibility, the section 7.2 indicator uses the stored series"
        ),
    }

    if args.write:
        artifact_dir = ensure_artifact_dir(ARTIFACT_DIR)
        frame.to_parquet(artifact_dir / f"missingness_rows_{read_ts}.parquet", index=False)
        (artifact_dir / f"missingness_flow_{read_ts}.json").write_text(
            json.dumps(payload, indent=2) + "\n"
        )
        logger.info("artifact written to %s", artifact_dir)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for level, count in table:
            print(f"{count:6d}  {level}")
        print(f"\nmirror vs lens: {agreement}")
        print(f"missingness vs outcome: {diagnostic}")
        print(f"read_ts: {read_ts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
