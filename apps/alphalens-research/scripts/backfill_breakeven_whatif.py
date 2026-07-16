#!/usr/bin/env python
"""One-off backfill: stamp ``breakeven_realized_r_json`` onto historical
population-ladder rows whose bars are retained but which the monitor FROZE before
the column existed (already-terminal decisions never get a fresh minute-resolve, so
the nightly job leaves them empty; the /edge what-if aggregate is terminal-only, so
it stays empty until this backfill runs or new positions mature).

Reuses the validated arrival-aware bar matching + RTH filter (mirrors
``diagnose_exit_geometry.py``, which reproduces the stored realized_r 42/42 to 0.0000)
and the pipeline's ``breakeven_grid``. Writes the parquet SoT atomically per file;
NEVER overwrites a value the monitor already stamped. DISPLAY-ONLY (realized_r is
never touched). It is an in-sample counterfactual either way.

KNOWN LIMITATION (atr_bracket_1p5): this script calls ``breakeven_grid`` WITHOUT
``pct_off_52w_high`` (the CandidateBrief column is not loaded here), so any
backfilled ATR-bracket values carry an UNCAPPED TP — a different cohort than the
monitor's forward-stamped, 52w-ceiling-capped values (memo
``bezpazery_lens_design_2026_07_16.md`` section 4.2). Deliberate: the lens accrues
forward-only; do not use this script to seed its history without threading the
brief column first.

DRY-RUN by default. Pass ``--write`` to persist, then re-ingest to Postgres:
    compose run --rm rebuild-ladder-outcomes

Usage:
    python apps/alphalens-research/scripts/backfill_breakeven_whatif.py            # dry-run
    python apps/alphalens-research/scripts/backfill_breakeven_whatif.py --write    # persist
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd
import pandas.testing as pdt
from alphalens_pipeline.feedback.breakeven_lenses import breakeven_grid
from alphalens_research.diagnostics.breakeven_backfill import apply_backfill

# WARNING: EDT (UTC-4) SUMMER ONLY. RTH = 09:30-16:00 ET = 13:30-20:00 UTC. This is a
# one-off backfill over a summer sample; a winter (EST) or half-day date would need a
# session-aware filter. Mirrors diagnose_exit_geometry.py, whose fidelity check pins
# that this reproduces the stored realized_r exactly on this data (42/42 to 0.0000).
_RTH_START_S = 13 * 3600 + 30 * 60
_RTH_END_S = 20 * 3600


def _rth(df: pd.DataFrame) -> pd.DataFrame:
    tod = (df["t"] // 1000) % 86400
    return df[(tod >= _RTH_START_S) & (tod < _RTH_END_S)]


def _index_bar_files(bars_dir: Path) -> dict[str, list[tuple[str, str]]]:
    by_ticker: dict[str, list[tuple[str, str]]] = {}
    for p in glob.glob(str(bars_dir / "*.parquet")):
        stem = os.path.basename(p)[:-8]
        m = re.match(r"^(.+)_(\d{4}-\d{2}-\d{2})$", stem)
        if m:
            by_ticker.setdefault(m.group(1), []).append((m.group(2), p))
    for entries in by_ticker.values():
        entries.sort()
    return by_ticker


def _find_bars(
    by_ticker: dict[str, list[tuple[str, str]]], bars_dir: Path, tk: str, bd: str
) -> str | None:
    cand = by_ticker.get(tk, [])
    if not cand:
        p = bars_dir / f"{tk}.parquet"
        return str(p) if p.exists() else None
    ge = [p for d, p in cand if d >= bd]
    return ge[0] if ge else cand[-1][1]


def _load_setups(briefs_dir: Path) -> dict[tuple[str, str], dict]:
    setups: dict[tuple[str, str], dict] = {}
    for f in glob.glob(str(briefs_dir / "*.parquet")):
        bd = os.path.basename(f)[:-8]
        try:
            b = pd.read_parquet(f, columns=["ticker", "brief_trade_setup"])
        except (ValueError, KeyError, OSError):
            continue
        for _, r in b.iterrows():
            s = r["brief_trade_setup"]
            if not isinstance(s, (str, dict)):
                continue
            try:
                d = json.loads(s) if isinstance(s, str) else s
            except (ValueError, TypeError):
                continue
            if isinstance(d, dict) and d.get("entry_tiers"):
                setups[(bd, str(r["ticker"]))] = d
    return setups


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    home = Path(os.path.expanduser("~/.alphalens"))
    ap.add_argument("--store-dir", type=Path, default=home / "population_ladders")
    ap.add_argument("--bars-dir", type=Path, default=home / "population_ladders" / "bars")
    ap.add_argument("--briefs-dir", type=Path, default=home / "thematic_briefs")
    ap.add_argument("--write", action="store_true", help="persist (default: dry-run)")
    args = ap.parse_args()

    setups = _load_setups(args.briefs_dir)
    by_ticker = _index_bar_files(args.bars_dir)
    bars_cache: dict[str, list[dict] | None] = {}

    def _bars(tk: str, bd: str) -> list[dict] | None:
        bp = _find_bars(by_ticker, args.bars_dir, tk, bd)
        if bp is None:
            return None
        if bp not in bars_cache:
            bars_cache[bp] = _rth(pd.read_parquet(bp)).to_dict("records")
        return bars_cache[bp]

    def compute(row: Any) -> str | None:
        setup = setups.get((str(row["brief_date"]), str(row["ticker"])))
        if setup is None:
            return None
        bars = _bars(str(row["ticker"]), str(row["brief_date"]))
        if not bars:
            return None
        # Mirror the monitor: json.dumps(grid) whenever the grid is built (a lens that
        # cannot resolve maps to null inside it, which the aggregate then drops).
        grid = breakeven_grid(setup, bars)
        return json.dumps(grid) if grid else None

    total_filled = 0
    total_rows = 0
    for f in sorted(glob.glob(str(args.store_dir / "*.parquet"))):
        df = pd.read_parquet(f)
        out, n = apply_backfill(df, compute)
        total_rows += len(df)
        total_filled += n
        if n:
            print(f"  {os.path.basename(f)}: +{n}")
            if args.write:
                # Unique temp name (avoid collisions on a manual re-run), same dir as
                # the target so os.replace is an atomic rename on one filesystem.
                tmp = f"{f}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
                out.to_parquet(tmp, index=False)
                # SoT guard: the round-trip must preserve every NON-backfill column
                # (dtype-tolerant, value-strict). Abort rather than replace on drift.
                check = pd.read_parquet(tmp)
                others = [c for c in df.columns if c != "breakeven_realized_r_json"]
                try:
                    pdt.assert_frame_equal(
                        df[others].reset_index(drop=True),
                        check[others].reset_index(drop=True),
                        check_dtype=False,
                    )
                except AssertionError as exc:
                    os.remove(tmp)
                    raise SystemExit(
                        f"ABORT: parquet round-trip altered {os.path.basename(f)} — {exc}"
                    ) from exc
                os.replace(tmp, f)  # atomic

    mode = "WRITTEN" if args.write else "DRY-RUN (pass --write to persist)"
    print(f"\nbackfilled {total_filled} rows across {total_rows} total — {mode}")
    if args.write and total_filled:
        print("next: re-ingest to Postgres via `compose run --rm rebuild-ladder-outcomes`")


if __name__ == "__main__":
    main()
