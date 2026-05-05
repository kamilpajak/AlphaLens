"""Rebuild iVol cache inventory parquet for fast asof-eligibility lookup.

Scans every parquet in ``~/.alphalens/ivolatility_smd/`` and emits a single
inventory frame with columns ``[ticker, first_date, last_date, n_rows,
ivp30_rows, pre_2018_rows, pre_2008_rows]``. Consumed by the universe loaders
in :mod:`alphalens.paper_trade.universe_loaders` for fast U2/U3 candidate
selection.

Idempotent — safe to run after every backfill batch.

The core logic is split into :func:`build_inventory` (pure-function, takes
explicit paths) and :func:`main` (CLI wrapper using ``~/.alphalens`` defaults)
so tests can drive the function with a tmp directory."""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd

logger = logging.getLogger("build_ivol_inventory")

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "ivolatility_smd"
DEFAULT_OUT = Path.home() / ".alphalens" / "ivolatility_smd_inventory.parquet"
CUTOFF_PRE_2018 = pd.Timestamp("2018-04-30").date()
CUTOFF_PRE_2008 = pd.Timestamp("2008-04-30").date()


def _scan_parquet(path: str) -> dict | None:
    """Read tradeDate + ivp30 columns from one parquet, return summary row.

    Returns ``None`` for empty parquets and parquets with no parseable
    ``tradeDate`` values (caller treats both as skip)."""
    try:
        df = pd.read_parquet(path, columns=["tradeDate", "ivp30"])
    except Exception:
        # Schema may be missing ``ivp30`` (e.g. empty parquets written via
        # ``pd.DataFrame``); fall back to projecting whatever's available.
        df = pd.read_parquet(path)
        if "tradeDate" not in df.columns:
            return None
    if df.empty:
        return None
    td = pd.to_datetime(df["tradeDate"], errors="coerce").dropna()
    if not len(td):
        return None
    ivp30_n = int(df["ivp30"].notna().sum()) if "ivp30" in df.columns else 0
    return {
        "ticker": os.path.basename(path).replace(".parquet", ""),
        "first_date": td.min().date(),
        "last_date": td.max().date(),
        "n_rows": len(td),
        "ivp30_rows": ivp30_n,
        "pre_2018_rows": int((td.dt.date < CUTOFF_PRE_2018).sum()),
        "pre_2008_rows": int((td.dt.date < CUTOFF_PRE_2008).sum()),
    }


def build_inventory(cache_dir: Path, out_path: Path) -> dict:
    """Scan ``cache_dir`` for parquets, write inventory to ``out_path``.

    Returns a counts dict ``{"scanned": N, "ok": N, "errors": N}``.
    Inventory file is sorted by ticker for deterministic output. Failed reads
    are logged at WARNING but do not abort the scan — the caller decides what
    to do with the error count."""
    files = sorted(glob.glob(f"{cache_dir}/*.parquet"))
    rows: list[dict] = []
    errors = 0
    start = time.time()
    for i, path in enumerate(files, start=1):
        try:
            row = _scan_parquet(path)
            if row is not None:
                rows.append(row)
        except Exception as exc:
            logger.warning("[%s] read failed: %s", path, str(exc)[:100])
            errors += 1
        if i % 500 == 0:
            logger.info(
                "Scanned %d/%d (%.1fs elapsed, errors=%d)",
                i,
                len(files),
                time.time() - start,
                errors,
            )

    inv = pd.DataFrame(rows)
    if not inv.empty:
        inv = inv.sort_values("ticker").reset_index(drop=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    inv.to_parquet(out_path, index=False)
    counts = {"scanned": len(files), "ok": len(inv), "errors": errors}
    logger.info(
        "DONE: %d ok / %d scanned in %.1fs (errors=%d) → %s",
        counts["ok"],
        counts["scanned"],
        time.time() - start,
        counts["errors"],
        out_path,
    )
    return counts


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    counts = build_inventory(args.cache_dir, args.out)
    inv = pd.read_parquet(args.out)
    if not inv.empty:
        live = (inv["last_date"] >= pd.Timestamp("2026-01-01").date()).sum()
        delisted = (inv["last_date"] < pd.Timestamp("2026-01-01").date()).sum()
        with_pre_2018 = (inv["pre_2018_rows"] >= 100).sum()
        print(f"\nLive (last_date >= 2026-01-01): {live}")
        print(f"Delisted (last_date < 2026-01-01): {delisted}")
        print(f"With pre-2018 history (>=100 rows): {with_pre_2018}")
    return 0 if counts["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
