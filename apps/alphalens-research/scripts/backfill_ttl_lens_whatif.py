#!/usr/bin/env python
"""One-off per-key backfill: merge the TTL-honouring lens (``be_0p5r_trail0p6_ttl7``,
issue #1232) into the ``breakeven_realized_r_json`` maps of historical
population-ladder rows. Every other stamped key is preserved verbatim — this NEVER
recomputes another lens (the full-grid path of ``backfill_breakeven_whatif.py``
would silently replace 52w-ceiling-capped ATR-bracket stamps with uncapped
recomputes, which is why that frozen script cannot be reused here).

Differences from the frozen full-grid script, each deliberate:
  * per-key merge via ``apply_lens_key_backfill`` (rows whose map already exists);
    empty cells stay the OLD script's scope — this one never seeds a whole map;
  * the entry-TTL cutoff is derived per row from its stamped ``entry_ttl_days``
    (the TTL actually applied when the row froze, never today's default) through
    ``entry_ttl_cutoff_ms`` — parity with the monitor's ``_engine_cutoffs(...)[5]``
    is pinned by a test;
  * the RTH filter is SESSION-AWARE (per-day open/close from the exchange
    calendar), not the frozen script's summer-only fixed EDT window;
  * a missing bar file is a SKIP (counted), never a substitute — the old
    last-file-before fallback can hand the walk another brief's window, and with
    a brief-date-derived cutoff the whole substitute window may sit post-cutoff.

DRY-RUN by default. Pass ``--write`` to persist, then re-ingest to Postgres
(ONLY after the full backfill completes, so the new key's cohort never shifts
mid-read):
    compose run --rm rebuild-ladder-outcomes

Usage:
    python apps/alphalens-research/scripts/backfill_ttl_lens_whatif.py            # dry-run
    python apps/alphalens-research/scripts/backfill_ttl_lens_whatif.py --write    # persist
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd
import pandas.testing as pdt
from alphalens_pipeline.feedback.breakeven_lenses import BREAKEVEN_LENSES
from alphalens_pipeline.feedback.ladder_replay import replay_ladder_breakeven
from alphalens_pipeline.feedback.population_ladder_monitor import _rth_window_utc
from alphalens_research.diagnostics.breakeven_backfill import (
    UNRESOLVABLE,
    apply_lens_key_backfill,
    entry_ttl_cutoff_ms,
)
from broker_contract.constants import DEFAULT_ORDER_TTL_DAYS

_LENS_ID = "be_0p5r_trail0p6_ttl7"
_LENS = next(lens for lens in BREAKEVEN_LENSES if lens.lens_id == _LENS_ID)
_EXCHANGE = "XNYS"


def _rth_session_aware(df: pd.DataFrame) -> pd.DataFrame:
    """Keep bars inside their own session's RTH window.

    Reuses the monitor's ``_rth_window_utc`` (span-derived close, half-day aware,
    close-INCLUSIVE) so the filter cannot drift from the windows the monitor
    itself replays under — a hand-rolled ``session_close_utc`` mirror here was
    both exclusive at the close and blind to half-day spans (zen review).
    """
    if df.empty:
        return df
    days = pd.to_datetime(df["t"], unit="ms", utc=True).dt.date
    keep = pd.Series(False, index=df.index)
    for day in days.unique():
        try:
            open_ms, close_ms = _rth_window_utc(day, _EXCHANGE)
        except ValueError:  # non-session day: no bar of that day is RTH
            continue
        keep |= (days == day) & (df["t"] >= open_ms) & (df["t"] <= close_ms)
    return df[keep]


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


def _find_bars(by_ticker: dict[str, list[tuple[str, str]]], tk: str, bd: str) -> str | None:
    """First bar file dated on-or-after the brief (the arrival-session cache key).

    NO before-fallback: a missing window is a skip, never another brief's window.
    """
    ge = [p for d, p in by_ticker.get(tk, []) if d >= bd]
    return ge[0] if ge else None


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


def _row_entry_ttl_days(row: Any) -> int:
    try:
        value = row["entry_ttl_days"]
    except (KeyError, IndexError):
        return DEFAULT_ORDER_TTL_DAYS
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return DEFAULT_ORDER_TTL_DAYS
    return int(value)


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
    skipped = {"no_setup": 0, "no_bars": 0, "default_ttl_fallback": 0}

    def _bars(tk: str, bd: str) -> list[dict] | None:
        bp = _find_bars(by_ticker, tk, bd)
        if bp is None:
            return None
        if bp not in bars_cache:
            bars_cache[bp] = _rth_session_aware(pd.read_parquet(bp)).to_dict("records")
        return bars_cache[bp]

    def compute(row: Any) -> Any:
        bd = str(row["brief_date"])
        setup = setups.get((bd, str(row["ticker"])))
        if setup is None:
            skipped["no_setup"] += 1
            return UNRESOLVABLE
        bars = _bars(str(row["ticker"]), bd)
        if not bars:
            skipped["no_bars"] += 1
            return UNRESOLVABLE
        ttl_days = _row_entry_ttl_days(row)
        if "entry_ttl_days" not in row or pd.isna(row.get("entry_ttl_days")):
            skipped["default_ttl_fallback"] += 1
        cutoff = entry_ttl_cutoff_ms(dt.date.fromisoformat(bd), ttl_days, _EXCHANGE)
        return replay_ladder_breakeven(
            setup,
            bars,
            mfe_trigger_r=_LENS.mfe_trigger_r if _LENS.mfe_trigger_r is not None else float("inf"),
            trail_frac=_LENS.trail_frac,
            entry_expiry_ms=cutoff,
        )

    total_filled = 0
    total_rows = 0
    for f in sorted(glob.glob(str(args.store_dir / "*.parquet"))):
        df = pd.read_parquet(f)
        out, n = apply_lens_key_backfill(df, _LENS_ID, compute)
        total_rows += len(df)
        total_filled += n
        if n:
            print(f"  {os.path.basename(f)}: +{n}")
            if args.write:
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
    print(f"\nmerged {_LENS_ID} into {total_filled} rows across {total_rows} total — {mode}")
    print(f"skipped: {skipped}")
    if args.write and total_filled:
        print(
            "next (AFTER the full backfill only): re-ingest to Postgres via "
            "`compose run --rm rebuild-ladder-outcomes`"
        )


if __name__ == "__main__":
    main()
