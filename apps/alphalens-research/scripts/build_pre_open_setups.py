"""Freeze the trade setup of every recovered pre-open name that no brief ever stored.

#1494 recovered, from the build journal, the brief list that existed at the arrival open on
14 as-of dates. A name on that list which a later run dropped is in no brief parquet, so it
has no trade setup — and ``/edge`` cannot replay a ladder without one.

The setup is rebuilt with the production builder over the frame the pipeline itself cached
that day (``~/.alphalens/thematic_ohlcv/<TICKER>_<asof>.parquet``), which makes it a pure
function of data at rest. ``order_ttl_days`` is taken from the date's own stored setups
rather than from today's default, because the default changed on 2026-06-11 and the date's
other rows carry the old value; every one of the 14 dates carries exactly one value.

The result is written once and committed, so the geometry of a re-added name is fixed before
any outcome is recomputed and can be read in a diff. Re-running this script over the same
caches must reproduce the file byte for byte.

Read-only. Run on the host that has the caches (the VPS):

    .venv/bin/python apps/alphalens-research/scripts/build_pre_open_setups.py \
        --out apps/alphalens-pipeline/alphalens_pipeline/thematic/config/pre_open_setups.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from alphalens_pipeline.thematic.pre_open_brief import PRE_OPEN_BRIEF_NAMES
from alphalens_pipeline.thematic.trade_setup.builder import build_trade_setup

DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"
DEFAULT_OHLCV_DIR = Path.home() / ".alphalens" / "thematic_ohlcv"

# The JSON is committed, so it is written the one way that survives a diff: keys in a fixed
# order, two-space indent, a trailing newline.
_JSON_KWARGS: dict[str, Any] = {"indent": 2, "sort_keys": True, "ensure_ascii": False}


class BuildError(RuntimeError):
    """The caches cannot answer a question the artefact must not guess at."""


def stored_order_ttl_days(brief_date: dt.date, briefs_dir: Path) -> int:
    """The single ``order_ttl_days`` carried by the date's stored setups.

    Raises rather than picking one when the date disagrees with itself, because a rebuilt
    row that carried a different value from its date-mates would pool under a different
    ``ladder_config_version`` token.
    """
    import pandas as pd

    path = briefs_dir / f"{brief_date.isoformat()}.parquet"
    if not path.exists():
        raise BuildError(f"{brief_date}: no brief parquet at {path}")
    frame = pd.read_parquet(path)
    values = set()
    for raw in frame.get("brief_trade_setup", []):
        # A row with no setup reads back as NaN, not as an empty string, and NaN is
        # truthy — so the type is what decides here, never the truthiness.
        if not isinstance(raw, str) or not raw:
            continue
        value = json.loads(raw).get("order_ttl_days")
        if value is not None:
            values.add(int(value))
    if len(values) != 1:
        raise BuildError(
            f"{brief_date}: stored setups carry {sorted(values)} order_ttl_days, not one"
        )
    return values.pop()


def stored_tickers(brief_date: dt.date, briefs_dir: Path) -> set[str]:
    import pandas as pd

    frame = pd.read_parquet(briefs_dir / f"{brief_date.isoformat()}.parquet")
    return {str(t).upper() for t in frame["ticker"]}


def _cache_loader(ohlcv_dir: Path):
    """The brief-time loader: the cached frame for that (ticker, asof), cut at the asof.

    Deliberately cache-only and network-free — the same shape as the orchestrator's loader.
    A miss yields an empty frame, which the builder turns into ``NO_STRUCTURE``.
    """
    import pandas as pd

    def load(ticker: str, asof: dt.date) -> pd.DataFrame:
        path = ohlcv_dir / f"{ticker.upper()}_{asof.isoformat()}.parquet"
        if not path.exists():
            return pd.DataFrame()
        frame = pd.read_parquet(path)
        return frame[frame.index <= pd.Timestamp(asof)]

    return load


def build_setups(*, briefs_dir: Path, ohlcv_dir: Path) -> dict[str, dict[str, Any]]:
    """The frozen setups, keyed by ISO date and then by ticker.

    Only names absent from the date's stored brief are built: a name the brief still holds
    already carries its own setup, and using that one keeps it on its original footing.
    """
    loader = _cache_loader(ohlcv_dir)
    out: dict[str, dict[str, Any]] = {}
    for brief_date, names in sorted(PRE_OPEN_BRIEF_NAMES.items()):
        ttl = stored_order_ttl_days(brief_date, briefs_dir)
        stored = stored_tickers(brief_date, briefs_dir)
        for ticker in names:
            if ticker.upper() in stored:
                continue
            setup = build_trade_setup(
                ticker=ticker, asof=brief_date, loader=loader, order_ttl_days=ttl
            )
            out.setdefault(brief_date.isoformat(), {})[ticker.upper()] = setup.to_dict()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Where to write the JSON.")
    parser.add_argument("--briefs-dir", type=Path, default=DEFAULT_BRIEFS_DIR)
    parser.add_argument("--ohlcv-dir", type=Path, default=DEFAULT_OHLCV_DIR)
    args = parser.parse_args(argv)

    setups = build_setups(briefs_dir=args.briefs_dir, ohlcv_dir=args.ohlcv_dir)
    n_names = sum(len(v) for v in setups.values())
    args.out.write_text(json.dumps(setups, **_JSON_KWARGS) + "\n", encoding="utf-8")
    statuses: dict[str, int] = {}
    for per_date in setups.values():
        for setup in per_date.values():
            statuses[setup["status"]] = statuses.get(setup["status"], 0) + 1
    print(f"wrote {n_names} setups across {len(setups)} dates to {args.out}")
    print(f"statuses: {statuses}")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main())
