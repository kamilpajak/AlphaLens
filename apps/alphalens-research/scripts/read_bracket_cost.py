"""Read the bracket-cost replay under the pre-registered contract.

Implements ``docs/research/mcap_bracket_cost_contract_2026_08_22.md`` §6-§12.
Where this file and a clause disagree, the clause wins — the clause was written
first, and the lesson from #1002 is that a script drifting from its own contract
is the failure that is hardest to notice.

Usage::

    .venv/bin/python apps/alphalens-research/scripts/read_bracket_cost.py
    .venv/bin/python apps/alphalens-research/scripts/read_bracket_cost.py --json
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ARM_DISCARDED = "discarded"
ARM_KEPT = "kept"

# Contract §8. Below this the read publishes numbers but refuses a verdict.
MIN_TERMINAL_PER_ARM = 30
# Contract §6.
DEFAULT_DRAWS = 10_000
DEFAULT_SEED = 20260822
CI_LOW_PCT = 2.5
CI_HIGH_PCT = 97.5

VERDICT_EARNS = "BRACKET EARNS ITS KEEP"
VERDICT_NOT_JUSTIFIED = "BRACKET NOT JUSTIFIED BY THIS DATA"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"

STORE_DIR = Path.home() / ".alphalens" / "bracket_cost_ladders"
BRIEFS_DIR = Path.home() / ".alphalens" / "bracket_cost" / "briefs"
# Read-only, for the §10 control. Never written to by this analysis.
PRODUCTION_STORE_DIR = Path.home() / ".alphalens" / "population_ladders"
# Contract §7: the "mega vs merely above ten billion" split.
MEGA_CAP_USD = 50e9


@dataclass(frozen=True)
class Decision:
    verdict: str
    median_discarded: float | None
    median_kept: float | None
    diff: float | None
    ci_low: float | None
    ci_high: float | None
    n_discarded: int
    n_kept: int
    n_days: int


def cluster_bootstrap_median_diff(
    frame: pd.DataFrame, *, n_draws: int = DEFAULT_DRAWS, seed: int = DEFAULT_SEED
) -> tuple[float, float]:
    """Percentile 95% interval on ``median(discarded) - median(kept)``.

    Resamples DAYS, not rows (contract §6). Proposals inside one day share a
    slate, a market session and a prompt version, so a row bootstrap would treat
    dependent observations as independent and report an interval far tighter
    than the design earns.

    A draw that leaves either arm empty contributes no estimate rather than a
    NaN; the interval is taken over the draws that produced one.
    """
    days = frame["brief_date"].unique()
    rng = np.random.default_rng(seed)
    by_day = {d: frame[frame["brief_date"] == d] for d in days}
    diffs: list[float] = []
    for _ in range(n_draws):
        picked = rng.choice(days, size=len(days), replace=True)
        sample = pd.concat([by_day[d] for d in picked], ignore_index=True)
        a = sample.loc[sample["arm"] == ARM_DISCARDED, "realized_r"].dropna()
        b = sample.loc[sample["arm"] == ARM_KEPT, "realized_r"].dropna()
        if a.empty or b.empty:
            continue
        diffs.append(float(a.median() - b.median()))
    if not diffs:
        return (float("nan"), float("nan"))
    return (
        float(np.percentile(diffs, CI_LOW_PCT)),
        float(np.percentile(diffs, CI_HIGH_PCT)),
    )


def decide(
    frame: pd.DataFrame, *, n_draws: int = DEFAULT_DRAWS, seed: int = DEFAULT_SEED
) -> Decision:
    """Apply the contract's fixed verdicts (§12) with the maturity floor (§8).

    The floor is checked FIRST and on its own. A thin arm cannot be argued past
    by a large-looking difference — that is the whole point of fixing the floor
    before seeing any number.
    """
    a = frame[frame["arm"] == ARM_DISCARDED]["realized_r"].dropna()
    b = frame[frame["arm"] == ARM_KEPT]["realized_r"].dropna()
    n_days = int(frame["brief_date"].nunique())

    if len(a) < MIN_TERMINAL_PER_ARM or len(b) < MIN_TERMINAL_PER_ARM:
        return Decision(
            verdict=VERDICT_INCONCLUSIVE,
            median_discarded=float(a.median()) if len(a) else None,
            median_kept=float(b.median()) if len(b) else None,
            diff=None,
            ci_low=None,
            ci_high=None,
            n_discarded=len(a),
            n_kept=len(b),
            n_days=n_days,
        )

    med_a, med_b = float(a.median()), float(b.median())
    diff = med_a - med_b
    lo, hi = cluster_bootstrap_median_diff(frame, n_draws=n_draws, seed=seed)
    excludes_zero = not (lo <= 0.0 <= hi)
    kept_better = diff < 0.0
    verdict = VERDICT_EARNS if (kept_better and excludes_zero) else VERDICT_NOT_JUSTIFIED
    return Decision(
        verdict=verdict,
        median_discarded=med_a,
        median_kept=med_b,
        diff=diff,
        ci_low=lo,
        ci_high=hi,
        n_discarded=len(a),
        n_kept=len(b),
        n_days=n_days,
    )


def positive_control(replayed: pd.DataFrame, production: pd.DataFrame) -> dict:
    """Contract §10: does the kept arm here agree with the production store?

    Only the KEPT arm can be compared — the production store holds no discarded
    rows at all, because the bracket filter runs before a card exists. Comparing
    them would count rows that could never match as disagreements.

    Zero overlap returns ``None``, not ``0.0``. "Nothing was checked" and
    "everything disagreed" are opposite facts and must not share a number: a
    control that silently reports total disagreement as its healthy state is
    worse than no control.

    The SAME rule applies one level down, and the first version of this function
    broke it: a row this store has not yet classified holds no opinion, so it
    cannot agree or disagree. Counting those as disagreements made the control
    read 0.61 while the rows it had actually resolved agreed 0.86 of the time.
    They are reported as ``n_unclassified_here`` — a measure of how far the
    replay has got, which is a different fact from whether it is faithful.
    """
    kept = replayed[replayed["arm"] == ARM_KEPT]
    merged = kept.merge(
        production, on=["brief_date", "ticker"], how="inner", suffixes=("", "_prod")
    )
    comparable = merged[
        merged["ladder_classification"].notna() & merged["ladder_classification_prod"].notna()
    ]
    agreement = (
        float(
            (comparable["ladder_classification"] == comparable["ladder_classification_prod"]).mean()
        )
        if len(comparable)
        else None
    )
    return {
        "n_overlap": len(merged),
        "n_comparable": len(comparable),
        "n_unclassified_here": int(merged["ladder_classification"].isna().sum()),
        "classification_agreement": agreement,
    }


def load_replayed(store_dir: Path = STORE_DIR, briefs_dir: Path = BRIEFS_DIR) -> pd.DataFrame:
    """Ladder rows joined to their arm, which lives on the synthetic brief."""
    ladders = []
    for path in sorted(glob.glob(str(store_dir / "*.parquet"))):
        ladders.append(pd.read_parquet(path))
    if not ladders:
        raise SystemExit(f"no replayed ladders under {store_dir} — run the replay first")
    lad = pd.concat(ladders, ignore_index=True)

    arms = []
    for path in sorted(glob.glob(str(briefs_dir / "*.parquet"))):
        frame = pd.read_parquet(path)[["ticker", "arm", "market_cap"]]
        frame["brief_date"] = dt.date.fromisoformat(os.path.basename(path)[:-8])
        arms.append(frame)
    arm = pd.concat(arms, ignore_index=True)

    lad["brief_date"] = pd.to_datetime(lad["brief_date"]).dt.date
    return lad.merge(arm, on=["brief_date", "ticker"], how="left")


def report(frame: pd.DataFrame) -> dict:
    """Primary plus every secondary the contract names (§7), each with its N."""
    terminal = frame[frame["terminal"] == True]  # noqa: E712
    decision = decide(terminal)

    out: dict = {"primary": asdict(decision)}
    out["attrition"] = {
        "rows": len(frame),
        "terminal": len(terminal),
        "ongoing": int((~frame["terminal"].astype(bool)).sum()),
        "unmatched_arm": int(frame["arm"].isna().sum()),
    }
    out["by_arm"] = {
        arm: {
            "rows": len(g),
            "terminal": int((g["terminal"] == True).sum()),  # noqa: E712
            "no_fill_rate": _rate(g, "NO_FILL"),
            "median_realized_r": _median(g, "realized_r"),
            "median_realized_r_ex_no_fill": _median(
                g[g["ladder_classification"] != "NO_FILL"], "realized_r"
            ),
            "median_market_excess_return": _median(g, "market_excess_return"),
            "classification_mix": g["ladder_classification"].value_counts().to_dict(),
        }
        for arm, g in frame.groupby("arm")
    }
    mega = terminal[terminal["market_cap"] > MEGA_CAP_USD]
    out["mega_split"] = {
        "threshold_usd": MEGA_CAP_USD,
        "n": len(mega),
        "median_realized_r": _median(mega, "realized_r"),
    }
    out["positive_control"] = positive_control(frame, _load_production())
    return out


def _load_production(store_dir: Path = PRODUCTION_STORE_DIR) -> pd.DataFrame:
    """Production ladder rows, read-only, for the §10 control."""
    frames = []
    for path in sorted(glob.glob(str(store_dir / "*.parquet"))):
        frames.append(pd.read_parquet(path)[["brief_date", "ticker", "ladder_classification"]])
    if not frames:
        return pd.DataFrame(columns=["brief_date", "ticker", "ladder_classification"])
    out = pd.concat(frames, ignore_index=True)
    out["brief_date"] = pd.to_datetime(out["brief_date"]).dt.date
    return out


def _median(frame: pd.DataFrame, col: str) -> float | None:
    if col not in frame.columns:
        return None
    vals = frame[col].dropna()
    return float(vals.median()) if len(vals) else None


def _rate(frame: pd.DataFrame, classification: str) -> float | None:
    resolved = frame["ladder_classification"].dropna()
    if resolved.empty:
        return None
    return float((resolved == classification).mean())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    out = report(load_replayed())
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return

    p = out["primary"]
    print(f"VERDICT: {p['verdict']}")
    print(f"  discarded n={p['n_discarded']} median={p['median_discarded']}")
    print(f"  kept      n={p['n_kept']} median={p['median_kept']}")
    if p["ci_low"] is not None:
        print(f"  diff={p['diff']:.4f}  95% CI [{p['ci_low']:.4f}, {p['ci_high']:.4f}]")
    print(f"  days={p['n_days']}")
    print()
    print(json.dumps({k: v for k, v in out.items() if k != "primary"}, indent=2, default=str))


if __name__ == "__main__":
    main()
