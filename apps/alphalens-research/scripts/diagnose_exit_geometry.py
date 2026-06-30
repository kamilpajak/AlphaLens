#!/usr/bin/env python
"""Descriptive counterfactual: does an MFE-triggered break-even / trailing stop fix
the ladder's upside-down reward:risk geometry?

Read-only. No network, no writes. Replays the RETAINED minute bars in
``~/.alphalens/population_ladders/bars/`` for every terminal-with-``realized_r``
outcome under a tiny pre-registered grid of alternate EXIT-stop policies, holding the
candidate (price path), entry tiers, and TP ladder FIXED — so any change in realized R
is attributable to the stop rule, not the pick.

Motivation (``docs/research/exit_geometry_reward_risk_2026_06_30.md``): correct signals
(+14% market_excess) realize only +0.21R on a full take-profit while a stop = full -1R,
so payoff is 0.22:1 and the strategy bleeds in R despite good selection. ~70% of losers
peak >= +0.5R MFE before reversing to -1R, which the existing TP-hit ratchet never
rescues (it arms on a TP target, not on MFE).

CAVEATS — this is DESCRIPTIVE evidence for a pre-registration, NOT a validated edge:
  * N is tiny (terminal-with-fill ~42), conditioned-on-fill, ~5 weeks of brief-days.
  * The grid is computed on the SAME 42 outcomes the hypothesis was read from.
  * The break-even stop only arms AFTER +0.5R MFE and only moves the stop UP toward
    break-even, so it can never cut a position that is not already in profit; the
    "winners changed" count below is the empirical floor-interaction co-validation.

Usage:
    uv run python apps/alphalens-research/scripts/diagnose_exit_geometry.py
    uv run python apps/alphalens-research/scripts/diagnose_exit_geometry.py --bars-dir ~/.alphalens/population_ladders/bars
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from alphalens_pipeline.feedback.ladder_replay import replay_ladder, replay_ladder_breakeven

# RTH window for the May-June 2026 sample (US Eastern is EDT = UTC-4): 09:30-16:00 ET
# = 13:30-20:00 UTC. The production replay RTH-filters before the walk so the minute
# path agrees with the grouped-daily [low, high] superset; we mirror that here. (A
# half-day / DST edge would shift this, but the sample is all summer sessions and the
# baseline-fidelity check below pins that this reproduces the stored realized_r.)
_RTH_START_S = 13 * 3600 + 30 * 60
_RTH_END_S = 20 * 3600

# The pre-registered grid: (label, mfe_trigger_r, trail_frac). None trail = pure break-even.
_GRID: list[tuple[str, float, float | None]] = [
    ("be@0.5R", 0.5, None),
    ("be@0.5R+trail0.6", 0.5, 0.6),
    ("be@0.3R", 0.3, None),
    ("be@0.75R", 0.75, None),
    ("be@0.5R+trail0.4", 0.5, 0.4),
]


def _rth(df: pd.DataFrame) -> pd.DataFrame:
    tod = (df["t"] // 1000) % 86400
    return df[(tod >= _RTH_START_S) & (tod < _RTH_END_S)]


def _index_bar_files(bars_dir: Path) -> dict[str, list[tuple[str, str]]]:
    """Map ticker -> sorted [(arrival_date, path)] for ``<TICKER>_<YYYY-MM-DD>.parquet``."""
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
    """Bar file for (ticker, brief_date): the cache is keyed by arrival_session = the
    first session >= brief_date, so pick the smallest file-date >= brief_date."""
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


def _expectancy(rs: list[float]) -> dict[str, Any]:
    a = np.array([r for r in rs if r is not None], dtype=float)
    wins = a[a > 0]
    losses = a[a <= 0]
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    payoff = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
    be_wr = (
        abs(avg_loss) / (avg_win + abs(avg_loss)) if (avg_win + abs(avg_loss)) > 0 else float("nan")
    )
    return {
        "n": len(a),
        "mean_r": float(a.mean()) if len(a) else float("nan"),
        "median_r": float(np.median(a)) if len(a) else float("nan"),
        "win_rate": float((a > 0).mean()) if len(a) else float("nan"),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": payoff,
        "breakeven_wr": be_wr,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    home = Path(os.path.expanduser("~/.alphalens"))
    ap.add_argument("--store-dir", type=Path, default=home / "population_ladders")
    ap.add_argument("--bars-dir", type=Path, default=home / "population_ladders" / "bars")
    ap.add_argument("--briefs-dir", type=Path, default=home / "thematic_briefs")
    args = ap.parse_args()

    by_ticker = _index_bar_files(args.bars_dir)
    setups = _load_setups(args.briefs_dir)

    lad = pd.concat(
        [pd.read_parquet(f) for f in glob.glob(str(args.store_dir / "*.parquet"))],
        ignore_index=True,
    )
    t = lad[(lad["terminal"] == True) & (lad["realized_r"].notna())].copy()  # noqa: E712

    variants: dict[str, list[float]] = {"baseline(stored)": [], "baseline(replayed)": []}
    for label, _trig, _trail in _GRID:
        variants[label] = []
    fidelity: list[float] = []
    winners_changed: dict[str, int] = {label: 0 for label, _, _ in _GRID}
    matched = 0

    for _, r in t.iterrows():
        bd, tk = str(r["brief_date"]), str(r["ticker"])
        setup = setups.get((bd, tk))
        bp = _find_bars(by_ticker, args.bars_dir, tk, bd)
        if setup is None or bp is None:
            continue
        bars = _rth(pd.read_parquet(bp)).to_dict("records")
        if not bars:
            continue
        base = replay_ladder(setup, bars).realized_r
        if base is None:
            continue
        matched += 1
        stored = float(r["realized_r"])
        fidelity.append(abs(base - stored))
        variants["baseline(stored)"].append(stored)
        variants["baseline(replayed)"].append(base)
        for label, trig, trail in _GRID:
            cf = replay_ladder_breakeven(setup, bars, mfe_trigger_r=trig, trail_frac=trail)
            # When the what-if is undefined (None: no-fill / risk<=0) we substitute the
            # stored baseline so the event is not dropped; this mixes a few unreachable
            # outcomes into a variant's mean. Fine for exploration; a formal eval would
            # carry a variant-specific n + an "undefined" count instead.
            variants[label].append(cf if cf is not None else base)
            # Floor-interaction co-validation: did the policy turn a stored winner into a
            # worse outcome? For a PURE break-even (trail is None) this MUST be 0 — it
            # only arms above the +0.5R MFE trigger, so it never cuts a not-yet-profitable
            # position. A TRAILING variant CAN legitimately exit a winner earlier, so a
            # positive count there is not a bug (matches the printed note below).
            if cf is not None and stored > 0 and cf < stored - 1e-9:
                winners_changed[label] += 1

    fid = np.array(fidelity)
    print(f"\nmatched & replayed: {matched} of {len(t)} terminal-with-realized_r")
    print(
        f"baseline fidelity |replayed - stored|: median={np.median(fid):.4f} "
        f"p90={np.percentile(fid, 90):.4f} max={fid.max():.4f}  "
        f"(within 0.05R: {int((fid < 0.05).sum())}/{len(fid)})"
    )
    print("\n=== EXIT-STOP POLICY GRID (terminal-with-fill, descriptive) ===")
    hdr = f"{'policy':22s} {'mean_R':>8s} {'med_R':>7s} {'win%':>5s} {'payoff':>7s} {'be_wr%':>7s}"
    print(hdr)
    print("-" * len(hdr))
    order = ["baseline(stored)", *[g[0] for g in _GRID]]
    for label in order:
        e = _expectancy(variants[label])
        wc = (
            ""
            if label.startswith("baseline")
            else f"  winners_worse={winners_changed.get(label, 0)}"
        )
        print(
            f"{label:22s} {e['mean_r']:+8.3f} {e['median_r']:+7.3f} {100 * e['win_rate']:5.0f} "
            f"{e['payoff']:7.2f} {100 * e['breakeven_wr']:7.0f}{wc}"
        )
    print(
        "\nNOTE: descriptive only (N~42, conditioned-on-fill, same sample the hypothesis was read from)."
        "\n      'winners_worse' MUST be ~0 (break-even only arms after +0.5R MFE). NOT a validated edge."
    )


if __name__ == "__main__":
    main()
