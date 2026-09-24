"""Check the rebuilt pre-open setups against every setup whose true value is known.

``build_pre_open_setups.py`` reconstructs the trade setup of a recovered name that no brief
stores. Nothing in that name's own history can confirm the reconstruction — there is no
stored setup to compare against. What CAN be checked is the rule itself, on the names where
both a stored setup and a rebuild exist:

1. the 61 recovered names a brief still carries, and
2. every name on the dates that are NOT being rebuilt — the out-of-sample arm, which is
   what makes this more than a rule fitted to the cases it was read from.

It also re-derives the committed artefact and refuses if it does not reproduce byte for byte,
and it counts the names whose stop is clamped by the 25%-of-entry floor — the one builder
behaviour known to have changed since June, which matters only if it reaches this population.

Every step re-runs the LIVE builder, so the check only applies while the live builder rule is
the one the artefact was built under. After a rule change (#1529 moved the stop anchor) it
stops and says so instead of reporting a reproduction failure. The artefact is NOT rebuilt:
it must keep the geometry of the briefs of its own dates.

Read-only. Needs the cached daily frames, so run it on the host that has them (the VPS):

    .venv/bin/python apps/alphalens-research/scripts/verify_pre_open_setups.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from alphalens_pipeline.thematic.pre_open_brief import PRE_OPEN_BRIEF_NAMES
from alphalens_pipeline.thematic.pre_open_setup import SETUPS_PATH
from alphalens_pipeline.thematic.trade_setup.builder import build_trade_setup
from alphalens_pipeline.thematic.trade_setup.config_version import setup_builder_config_version

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_pre_open_setups import (
    _JSON_KWARGS,
    DEFAULT_BRIEFS_DIR,
    DEFAULT_OHLCV_DIR,
    _cache_loader,
    build_setups,
    stored_order_ttl_days,
)

# The fields a ladder replay actually reads off a setup. A reconstruction that moves any of
# them moves the outcome.
SCALARS = ("asof_close", "atr", "disaster_stop", "suggested_size_pct")
FLOOR_FRAC = 0.75
TOLERANCE = 6


def _levels(setup: dict[str, Any]) -> tuple[list[float], list[float]]:
    entries = [round(float(t["limit"]), TOLERANCE) for t in setup.get("entry_tiers", [])]
    targets = [round(float(t["target"]), TOLERANCE) for t in setup.get("tp_tranches", [])]
    return entries, targets


def _compare(old: dict[str, Any], new: dict[str, Any]) -> dict[str, bool]:
    same = {
        key: round(float(old.get(key) or 0.0), TOLERANCE)
        == round(float(new.get(key) or 0.0), TOLERANCE)
        for key in SCALARS
    }
    old_entries, old_targets = _levels(old)
    new_entries, new_targets = _levels(new)
    same["entry_tiers"] = old_entries == new_entries
    same["tp_tranches"] = old_targets == new_targets
    return same


def _tally(rows: list[dict[str, bool]]) -> dict[str, str]:
    n = len(rows)
    keys = [*SCALARS, "entry_tiers", "tp_tranches"]
    return {key: f"{sum(r[key] for r in rows)}/{n}" for key in keys}


def _floor_binds(setup: dict[str, Any]) -> bool:
    tiers = setup.get("entry_tiers") or []
    if not tiers:
        return False
    weight = sum(t["alloc_pct"] for t in tiers)
    blended = sum(t["limit"] * t["alloc_pct"] for t in tiers) / weight
    return abs(float(setup["disaster_stop"]) - FLOOR_FRAC * blended) < 1e-6


def rule_mismatch(committed: dict[str, Any], live_token: str) -> str | None:
    """Say why the check does not apply, or ``None`` when the artefact matches the live rule."""
    tokens = {
        str(setup.get("builder_config_version"))
        for day in committed.values()
        for setup in day.values()
    }
    if tokens == {live_token}:
        return None
    return (
        f"the artefact was built under builder rule(s) {sorted(tokens)}, the live builder is "
        f"{live_token}; this check re-derives setups with the live builder, so it does not apply"
    )


def _stored_setups(path: Path) -> dict[str, dict[str, Any]]:
    import pandas as pd

    frame = pd.read_parquet(path)
    out = {}
    for _, row in frame.iterrows():
        raw = row.get("brief_trade_setup")
        # A row with no setup reads back as NaN, which is truthy; the type decides.
        if isinstance(raw, str) and raw:
            out[str(row["ticker"]).upper()] = json.loads(raw)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--briefs-dir", type=Path, default=DEFAULT_BRIEFS_DIR)
    parser.add_argument("--ohlcv-dir", type=Path, default=DEFAULT_OHLCV_DIR)
    args = parser.parse_args(argv)

    committed = SETUPS_PATH.read_text(encoding="utf-8")
    mismatch = rule_mismatch(json.loads(committed), setup_builder_config_version())
    if mismatch is not None:
        print(f"NOT APPLICABLE: {mismatch}", file=sys.stderr)
        return 1

    loader = _cache_loader(args.ohlcv_dir)
    recovered_dates = set(PRE_OPEN_BRIEF_NAMES)
    failures: list[str] = []

    # --- the committed artefact must still be what the rule produces -------------------
    fresh = json.dumps(
        build_setups(briefs_dir=args.briefs_dir, ohlcv_dir=args.ohlcv_dir), **_JSON_KWARGS
    )
    if fresh + "\n" != committed:
        failures.append(f"{SETUPS_PATH} is not what the builder produces from these caches")
    print(f"artefact reproduces: {fresh + chr(10) == committed}")

    # --- arm 1: the recovered names a brief still carries -----------------------------
    in_sample: list[dict[str, bool]] = []
    floor_hits = 0
    for brief_date, names in sorted(PRE_OPEN_BRIEF_NAMES.items()):
        ttl = stored_order_ttl_days(brief_date, args.briefs_dir)
        stored = _stored_setups(args.briefs_dir / f"{brief_date.isoformat()}.parquet")
        for ticker in names:
            rebuilt = build_trade_setup(
                ticker=ticker, asof=brief_date, loader=loader, order_ttl_days=ttl
            ).to_dict()
            if rebuilt.get("status") == "OK" and _floor_binds(rebuilt):
                floor_hits += 1
            old = stored.get(ticker.upper())
            if old is None or old.get("status") != "OK" or rebuilt.get("status") != "OK":
                continue
            in_sample.append(_compare(old, rebuilt))
    print(f"recovered names with a stored setup: {len(in_sample)} -> {_tally(in_sample)}")
    print(f"stop clamped by the {int(FLOOR_FRAC * 100)}% floor, recovered names: {floor_hits}")

    # --- arm 2: every name on the dates that are NOT being rebuilt ---------------------
    out_sample: list[dict[str, bool]] = []
    unbuildable = 0
    for path in sorted(args.briefs_dir.glob("*.parquet")):
        brief_date = dt.date.fromisoformat(path.stem)
        if brief_date in recovered_dates:
            continue
        for ticker, old in _stored_setups(path).items():
            if old.get("status") != "OK":
                continue
            ttl = old.get("order_ttl_days")
            rebuilt = build_trade_setup(
                ticker=ticker,
                asof=brief_date,
                loader=loader,
                **({} if ttl is None else {"order_ttl_days": int(ttl)}),
            ).to_dict()
            if rebuilt.get("status") != "OK":
                unbuildable += 1
                continue
            out_sample.append(_compare(old, rebuilt))
    print(f"out-of-sample names: {len(out_sample)} -> {_tally(out_sample)}")
    print(f"out-of-sample names whose frame is gone: {unbuildable}")

    for message in failures:
        print(f"FAIL: {message}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main())
