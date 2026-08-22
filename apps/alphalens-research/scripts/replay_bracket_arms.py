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


@dataclass(frozen=True)
class Attrition:
    """Contract §10: every in-scope row lands in exactly one bucket."""

    in_scope: int
    no_structure: int
    no_bars: int
    terminal: int
    ongoing: int

    def balanced(self) -> bool:
        return self.in_scope == self.no_structure + self.no_bars + self.terminal + self.ongoing

    def as_row(self) -> dict[str, int]:
        return {
            "in_scope": self.in_scope,
            "no_structure": self.no_structure,
            "no_bars": self.no_bars,
            "terminal": self.terminal,
            "ongoing": self.ongoing,
        }


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
        if payload.get("structure") == "NO_STRUCTURE" or not payload.get("entries"):
            no_structure += 1
            continue
        setups[(asof, ticker)] = payload
    return setups, no_structure


def prepare(funnel_dir: Path = FUNNEL_DIR, briefs_dir: Path = BRIEFS_DIR) -> Attrition:
    """Write one synthetic brief parquet per funnel date."""
    funnel = load_funnel(funnel_dir)
    rows = select_arms(funnel)
    tickers = {str(t).upper() for t in rows["ticker"]}
    logger.info("in scope: %d rows, %d tickers", len(rows), len(tickers))

    bars = load_grouped_ohlcv(tickers)
    missing = len(rows[~rows["ticker"].str.upper().isin(bars.keys())])
    setups, no_structure = build_setups(rows, bars)

    briefs_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for raw_asof, day_rows in rows.groupby("asof"):
        asof = cast("dt.date", raw_asof)
        day_setups = {
            str(t): setups[(asof, str(t))] for t in day_rows["ticker"] if (asof, str(t)) in setups
        }
        frame = synthetic_brief_frame(day_rows, day_setups)
        if frame.empty:
            continue
        frame.to_parquet(briefs_dir / f"{asof.isoformat()}.parquet")
        written += len(frame)

    logger.info(
        "prepared %d plannable rows (%d no-structure, %d without bars)",
        written,
        no_structure,
        missing,
    )
    # terminal/ongoing are unknown until the replay runs; the balance check that
    # matters is asserted in the read memo against the replayed store.
    return Attrition(
        in_scope=len(rows),
        no_structure=no_structure,
        no_bars=missing,
        terminal=0,
        ongoing=written,
    )


def replay(briefs_dir: Path = BRIEFS_DIR, store_dir: Path = STORE_DIR) -> None:
    """Run the production monitor over the synthetic briefs, into our own store."""
    from alphalens_pipeline.feedback.population_ladder_monitor import (
        replay_population_ladders,
    )

    if store_dir == Path.home() / ".alphalens" / "population_ladders":
        raise SystemExit("refusing to write the production ladder store")

    reports = replay_population_ladders(
        briefs_dir,
        lookback_days=90,
        store_dir=store_dir,
    )
    for rep in reports:
        logger.info("%s", rep)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="build synthetic briefs")
    parser.add_argument("--replay", action="store_true", help="run the ladder replay")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.prepare:
        att = prepare()
        print(json.dumps(att.as_row(), indent=2))
    if args.replay:
        replay()
    if not (args.prepare or args.replay):
        parser.error("pass --prepare and/or --replay")


if __name__ == "__main__":
    main()
