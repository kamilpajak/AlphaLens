"""Replay both market-cap bracket arms through the SAME ladder machinery.

Implements ``docs/research/mcap_bracket_cost_contract_2026_08_22.md``. Read the
contract first; every non-obvious choice here is a clause of it, and where a
clause and this file disagree, the clause wins.

What it does
------------
1. reads the proposal funnel and splits it into the contract's two arms
   (§4): ``too_big`` (discarded by the bracket) and ``in_bracket`` (kept);
2. builds a trade setup for every row from daily OHLCV, using the SAME source
   for both arms;
3. writes a synthetic briefs directory and replays it with the production
   :func:`replay_population_ladders`, into a store OUTSIDE the production one;
4. reports the attrition table (§10) — it must balance.

It computes no verdict. The read lives in its own memo, per §14.

Why a synthetic brief rather than a bespoke replay: the production monitor
already owns bar fetching, caching, maturity and terminal semantics. Re-deriving
those here would make the two arms differ from the shipped population by
whatever this file got wrong.

Usage::

    .venv/bin/python apps/alphalens-research/scripts/replay_bracket_arms.py --prepare
    .venv/bin/python apps/alphalens-research/scripts/replay_bracket_arms.py --replay
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pandas as pd

logger = logging.getLogger(__name__)

# The contract's two arms (§4). Whitelist values, never a negation: a null or
# unfamiliar verdict must be unable to enter an arm by failing an inequality.
# That exact bug sat latent in the #1002 read's primary denominator.
VERDICT_DISCARDED = "too_big"
VERDICT_KEPT = "in_bracket"
ARM_DISCARDED = "discarded"
ARM_KEPT = "kept"
_VERDICT_TO_ARM = {VERDICT_DISCARDED: ARM_DISCARDED, VERDICT_KEPT: ARM_KEPT}

FUNNEL_DIR = Path.home() / ".alphalens" / "thematic_candidates" / "proposal_funnel"
GROUPED_DIR = Path.home() / ".alphalens" / "grouped_daily_history"
WORK_DIR = Path.home() / ".alphalens" / "bracket_cost"
BRIEFS_DIR = WORK_DIR / "briefs"
STORE_DIR = Path.home() / ".alphalens" / "bracket_cost_ladders"

# SMA200 is one of the entry candidates, so a setup wants roughly a year of
# bars; the grouped store holds ~487 sessions, comfortably more.
OHLCV_LOOKBACK_SESSIONS = 400

# Brand-new rows draw from the monitor's `_FORCED_RESOLVE_BUDGET` — 50 per run,
# hardcoded, NOT tunable by ALPHALENS_FEEDBACK_MAX_FETCHES. Measured on the real
# funnel: 17.2 proposals/day before the 2026-08-18 prompt change and 51.5/day
# after it, peaking at 68. One pass per fire would therefore fall behind at the
# CURRENT rate and accumulate a backlog that never drains — silently, because
# the job still exits 0. Four passes give ~200/day of headroom and the loop
# stops as soon as a pass leaves no unresolved rows or fails to reduce their
# number. The signal is the UNRESOLVED-ROW count, not the fetch count: the first
# live run reported 85 fetches on every pass, because ongoing positions consume
# the main budget nightly by design.
DEFAULT_REPLAY_PASSES = 4


@dataclass(frozen=True)
class Attrition:
    """Contract §10: every in-scope row lands in exactly one bucket."""

    in_scope: int
    no_structure: int
    no_bars: int
    terminal: int
    ongoing: int
    # Rows on a day whose brief was already written and is deliberately left
    # alone, so a scheduled re-run cannot look like rows vanishing.
    frozen: int = 0

    def balanced(self) -> bool:
        return self.in_scope == (
            self.no_structure + self.no_bars + self.terminal + self.ongoing + self.frozen
        )

    def as_row(self) -> dict[str, int]:
        return {
            "in_scope": self.in_scope,
            "no_structure": self.no_structure,
            "no_bars": self.no_bars,
            "terminal": self.terminal,
            "ongoing": self.ongoing,
            "frozen": self.frozen,
        }


def is_plannable_setup(payload: Mapping[str, Any]) -> bool:
    """True when a serialised ``TradeSetup`` carries a usable ladder.

    The key names come from ``TradeSetup.to_dict`` — ``status`` and
    ``entry_tiers`` — and are asserted against a REAL builder output in the
    tests. An earlier version of this guard read invented keys, so it was
    unconditionally False and the run reported a clean, entirely fictional zero
    plannable rows.
    """
    from alphalens_pipeline.thematic.trade_setup.model import STATUS_OK

    return payload.get("status") == STATUS_OK and bool(payload.get("entry_tiers"))


def select_arms(funnel: pd.DataFrame) -> pd.DataFrame:
    """Rows in scope, stamped with their arm (contract §3 + §4).

    Drops ``too_small`` and ``no_mcap`` — both are named exclusions, not
    accidents. Deduplicates on the contract's unit, ``(asof, ticker)``, because
    the pipeline writes six slots per day onto the same date.
    """
    out = funnel.copy()
    out = out[out["ticker"].notna()]
    out["arm"] = out["bracket_verdict"].map(_VERDICT_TO_ARM)
    out = out[out["arm"].notna()]
    key = [c for c in ("asof", "ticker") if c in out.columns]
    return out.drop_duplicates(subset=key, keep="last").reset_index(drop=True)


def synthetic_brief_frame(rows: pd.DataFrame, setups: Mapping[str, Any]) -> pd.DataFrame:
    """Brief-shaped frame the production loader will accept.

    ``verified`` is True for BOTH arms on purpose (contract §5): it bypasses the
    downstream gates by construction and equally, so the bracket stays the only
    difference between the arms. It is not a claim that these rows would have
    passed those gates.

    A row with no setup is EXCLUDED rather than null-filled, so it shows up in
    the attrition table instead of silently becoming an un-plannable member of
    a denominator.
    """
    keep = rows[rows["ticker"].isin(setups.keys())].copy()
    keep["verified"] = True
    keep["brief_trade_setup"] = [json.dumps(setups[t]) for t in keep["ticker"]]
    cols = ["ticker", "theme", "verified", "brief_trade_setup", "arm"]
    for extra in ("market_cap", "bracket_verdict"):
        if extra in keep.columns:
            cols.append(extra)
    return keep[cols].reset_index(drop=True)


def unresolved_rows(store_dir: Path) -> int:
    """Rows in the store with no ladder classification yet.

    This is the drain signal, and the FETCH COUNT is not. Measured on the first
    live run: all four passes reported 85 fetches because ``fetches`` counts the
    main budget as well, and ongoing positions consume it every night by design.
    A count that never reaches zero cannot tell "new rows still draining" from
    "steady state", so the early exit never fired and every fire paid for four
    passes.
    """
    total = 0
    for path in glob.glob(str(store_dir / "*.parquet")):
        frame = pd.read_parquet(path, columns=["ladder_classification"])
        total += int(frame["ladder_classification"].isna().sum())
    return total


def write_brief(frame: pd.DataFrame, path: Path) -> None:
    """Write a brief parquet atomically.

    An overwrite killed halfway — OOM, the unit timeout, a full disk — leaves a
    torn parquet, and the next scheduled run cannot read it: ``prepare`` would
    fail on load and the job would wedge with no brief for that day. Temp file
    plus rename makes the replacement all-or-nothing.
    """
    from alphalens_pipeline.data.parquet_io import write_parquet_atomic

    write_parquet_atomic(frame, path, index=False)


def _load_existing_briefs(briefs_dir: Path) -> dict[tuple[dt.date, str], dict[str, Any]]:
    """Already-measured rows, keyed ``(asof, ticker)``, as plain dicts.

    Read verbatim so a row that has been under replay keeps the exact setup it
    was replayed against. Nothing here re-derives geometry.
    """
    out: dict[tuple[dt.date, str], dict[str, Any]] = {}
    for path in sorted(glob.glob(str(briefs_dir / "*.parquet"))):
        asof = dt.date.fromisoformat(os.path.basename(path)[:-8])
        for record in pd.read_parquet(path).to_dict("records"):
            out[(asof, str(record["ticker"]))] = cast("dict[str, Any]", record)
    return out


def load_funnel(funnel_dir: Path = FUNNEL_DIR) -> pd.DataFrame:
    """All funnel days, with ``asof`` recovered from the filename."""
    frames = []
    for path in sorted(glob.glob(str(funnel_dir / "*.parquet"))):
        frame = pd.read_parquet(path)
        frame["asof"] = dt.date.fromisoformat(os.path.basename(path)[:-8])
        frames.append(frame)
    if not frames:
        raise SystemExit(f"no funnel parquets under {funnel_dir}")
    return pd.concat(frames, ignore_index=True)


def load_grouped_ohlcv(
    tickers: set[str], *, grouped_dir: Path = GROUPED_DIR, sessions: int = OHLCV_LOOKBACK_SESSIONS
) -> dict[str, pd.DataFrame]:
    """Per-ticker daily OHLCV from the whole-market grouped store.

    The production brief loader reads a per-ticker cache that only exists for
    names that reached the SCORE stage — which the discarded arm never did. The
    grouped store covers the whole market, so it is the one source that can
    serve both arms. Using it for BOTH is the point: a different adjustment
    convention per arm would put a systematic difference into ATR, and every
    level in the ladder is ATR-scaled.
    """
    paths = sorted(glob.glob(str(grouped_dir / "*.parquet")))[-sessions:]
    wanted = {t.upper() for t in tickers}
    frames = []
    for path in paths:
        day = pd.read_parquet(path, columns=["T", "t", "o", "h", "l", "c", "v"])
        frames.append(day[day["T"].isin(wanted)])
    if not frames:
        return {}
    allbars = pd.concat(frames, ignore_index=True)
    allbars["date"] = pd.to_datetime(allbars["t"], unit="ms").dt.normalize()
    allbars = allbars.rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    out: dict[str, pd.DataFrame] = {}
    for ticker, grp in allbars.groupby("T"):
        frame = grp.sort_values("date").set_index("date")
        out[str(ticker)] = frame[["open", "high", "low", "close", "volume"]]
    return out


def build_setups(
    rows: pd.DataFrame, bars: Mapping[str, pd.DataFrame]
) -> tuple[dict[tuple[dt.date, str], Any], int]:
    """Trade setup per ``(asof, ticker)``; returns it plus the no-structure count.

    Slices the ticker's history at ``asof`` before building, mirroring the
    production loader's ``df[df.index <= asof]`` cut. A post-asof bar would
    desync ATR and every level derived from it.
    """
    from alphalens_pipeline.thematic.trade_setup import builder

    setups: dict[tuple[dt.date, str], Any] = {}
    no_structure = 0
    for row in rows.itertuples():
        ticker = str(row.ticker)
        asof = cast("dt.date", row.asof)
        frame = bars.get(ticker.upper())
        if frame is None or frame.empty:
            continue
        cut = frame[frame.index <= pd.Timestamp(asof)]
        setup = builder.build_trade_setup_from_frame(cut)
        payload = setup.to_dict()
        if not is_plannable_setup(payload):
            no_structure += 1
            continue
        setups[(asof, ticker)] = payload
    return setups, no_structure


def prepare(
    funnel_dir: Path = FUNNEL_DIR,
    briefs_dir: Path = BRIEFS_DIR,
    grouped_dir: Path = GROUPED_DIR,
    rebuild: bool = False,
    built_on: dt.date | None = None,
) -> Attrition:
    """Write one synthetic brief parquet per funnel date.

    ``grouped_dir`` is a parameter rather than a module global read inside,
    because a default argument binds at def time: a test that reassigned the
    global still read the real store, passed on a machine that had one, and
    failed on CI which does not. The bug was in the test, and the fix is to make
    the dependency injectable rather than ambient.
    """
    funnel = load_funnel(funnel_dir)
    rows = select_arms(funnel)
    briefs_dir.mkdir(parents=True, exist_ok=True)

    # A row already written is FROZEN, and the unit of freezing is the ROW, not
    # the day. The grouped daily store is split-adjusted and retro-adjusts
    # history, so re-deriving a setup weeks later can move every level of a
    # ladder already under replay — but a DAY-level freeze would be wrong in the
    # other direction: the funnel for an asof is rewritten after that date (on
    # the VPS, 2026-08-18's file was last modified on 2026-08-20), so a late
    # proposal would be stranded forever. Existing rows keep their geometry
    # byte-for-byte; new ones join. ``rebuild`` re-derives everything.
    existing = {} if rebuild else _load_existing_briefs(briefs_dir)
    keys = list(zip(rows["asof"], rows["ticker"].astype(str), strict=True))
    is_frozen = pd.Series([k in existing for k in keys], index=rows.index)
    frozen_rows = int(is_frozen.sum())
    todo = rows[~is_frozen]
    logger.info(
        "in scope: %d rows; %d already measured; %d to build",
        len(rows),
        frozen_rows,
        len(todo),
    )

    tickers = {str(t).upper() for t in todo["ticker"]}
    bars = load_grouped_ohlcv(tickers, grouped_dir=grouped_dir) if tickers else {}
    missing = len(todo[~todo["ticker"].str.upper().isin(bars.keys())])
    setups, no_structure = build_setups(todo, bars)

    written = 0
    for raw_asof, day_rows in todo.groupby("asof"):
        asof = cast("dt.date", raw_asof)
        day_setups = {
            str(t): setups[(asof, str(t))] for t in day_rows["ticker"] if (asof, str(t)) in setups
        }
        frame = synthetic_brief_frame(day_rows, day_setups)
        # Provenance, not decoration: a row built later than its day-mates was
        # derived from a grouped store that may have been retro-adjusted since,
        # so the read must be able to tell them apart. Impossible to backfill
        # once the rows exist, which is why it goes in before it is needed.
        if not frame.empty:
            frame["built_at"] = (built_on or dt.date.today()).isoformat()
        kept_rows = [existing[(asof, t)] for (a, t) in existing if a == asof]
        if frame.empty and not kept_rows:
            continue
        merged = pd.concat([pd.DataFrame(kept_rows), frame], ignore_index=True)
        merged = merged.drop_duplicates(subset=["ticker"], keep="first")
        write_brief(merged, briefs_dir / f"{asof.isoformat()}.parquet")
        written += len(frame)

    logger.info(
        "prepared %d rows (%d no-structure, %d without bars, %d frozen)",
        written,
        no_structure,
        missing,
        frozen_rows,
    )
    # terminal/ongoing are unknown until the replay runs; the balance check that
    # matters is asserted in the read memo against the replayed store.
    return Attrition(
        in_scope=len(rows),
        no_structure=no_structure,
        no_bars=missing,
        terminal=0,
        ongoing=written,
        frozen=frozen_rows,
    )


def replay(
    briefs_dir: Path = BRIEFS_DIR,
    store_dir: Path = STORE_DIR,
    *,
    max_passes: int = DEFAULT_REPLAY_PASSES,
    _replay_fn: Any = None,
) -> int:
    """Advance every row, in up to ``max_passes`` passes. Returns passes run.

    One pass resolves at most 50 brand-new rows (see ``DEFAULT_REPLAY_PASSES``),
    which is below the measured arrival rate, so a single pass per fire would
    leave a permanent backlog. The loop stops when a pass leaves no unresolved
    rows or fails to reduce their number — steady state, or rows that are stuck
    for reasons a further pass cannot fix. Note the count has a FLOOR above zero
    in practice: 11 of 413 live rows hold a null classification persistently
    (9 of them MRNA, rejected by the monitor's implausible-move guard), so
    termination normally comes from the "stopped falling" arm, not from zero.
    """
    if store_dir == Path.home() / ".alphalens" / "population_ladders":
        raise SystemExit("refusing to write the production ladder store")

    if _replay_fn is None:
        from alphalens_pipeline.feedback.population_ladder_monitor import (
            replay_population_ladders,
        )

        _replay_fn = replay_population_ladders

    passes = 0
    # The baseline is taken AFTER the first pass, never before it: on a fresh
    # store the count starts at 0, and a "did it fall?" test against 0 stops
    # immediately on the one run that has the most work to do.
    previous: int | None = None
    for attempt in range(1, max_passes + 1):
        _replay_fn(briefs_dir, lookback_days=90, store_dir=store_dir)
        passes = attempt
        remaining = unresolved_rows(store_dir)
        logger.info(
            "replay pass %d/%d: %s unresolved rows left",
            attempt,
            max_passes,
            remaining,
        )
        if remaining == 0 or (previous is not None and remaining >= previous):
            break
        previous = remaining
    return passes


def benchmark(store_dir: Path = STORE_DIR) -> int:
    """Fill the benchmark-excess columns the monitor deliberately leaves null.

    ``replay_population_ladders`` writes ``market_excess_return = None`` and a
    SEPARATE pass computes it, because the benchmark leg needs its own index
    fetch. Production runs that pass right after the monitor; this replay has to
    run it too or the contract's §7 excess secondary is permanently absent —
    which is what read 1 reported.
    """
    from alphalens_pipeline.feedback.benchmark_excess import (
        enrich_store_with_benchmark_excess,
    )

    if store_dir == Path.home() / ".alphalens" / "population_ladders":
        raise SystemExit("refusing to write the production ladder store")
    n = enrich_store_with_benchmark_excess(store_dir)
    logger.info("benchmark-excess: enriched %d rows", n)
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="build synthetic briefs")
    parser.add_argument(
        "--rebuild-briefs",
        action="store_true",
        help="re-derive setups for days already written (moves live ladder levels)",
    )
    parser.add_argument("--replay", action="store_true", help="run the ladder replay")
    parser.add_argument(
        "--replay-passes",
        type=int,
        default=DEFAULT_REPLAY_PASSES,
        help="max passes per run; stops once the unresolved-row count hits 0 or stops falling",
    )
    parser.add_argument(
        "--benchmark", action="store_true", help="fill the benchmark-excess columns"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.prepare:
        att = prepare(rebuild=args.rebuild_briefs)
        print(json.dumps(att.as_row(), indent=2))
    if args.replay:
        replay(max_passes=args.replay_passes)
    if args.benchmark:
        benchmark()
    if not (args.prepare or args.replay or args.benchmark):
        parser.error("pass --prepare, --replay and/or --benchmark")


if __name__ == "__main__":
    main()
