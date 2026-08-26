"""§10.2 missingness instrumentation for the exit-policy pre-registration.

Governing document: ``docs/research/exit_policy_comparison_prereg_2026_08_24.md``
(LOCKED), §7 and §10.2. Where a clause and this module disagree, the clause
wins.

The stored ladder parquet records only that the historical bracket lens
returned ``None`` — never why. The §7.1 flow table needs the reason, so this
module re-derives it by mirroring the DECISION ORDER of the production code
path (``feedback.ladder_replay.replay_ladder_atr_bracket`` →
``broker_contract.exit_geometry.levels.atr_bracket_levels``), one reason per
row, first refusal wins. The mirror is not trusted blind:
:func:`classify_and_verify` also runs the REAL lens and reports whether the
two verdicts agree, and the driver script asserts that agreement on every row
it classifies.

Descriptive only, by §3.4/§7: nothing here computes, or can compute, the
A-vs-B contrast — the module never sees arm B's outcome, only whether the
bracket was constructible. Reason priority follows the CODE, not the memo
table's presentation order (a row that is both stop-degenerate and
ceiling-capped classifies as the stop, because ``atr_bracket_levels`` checks
the stop first).

Artifacts go to a dedicated directory; :func:`ensure_artifact_dir` refuses the
production ladder store outright (memo §10.2: the re-run must not rewrite any
stamped value).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from alphalens_pipeline.feedback.ladder_replay import (
    parse_ladder,
    replay_ladder,
    replay_ladder_atr_bracket,
)
from broker_contract.exit_geometry.levels import atr_bracket_levels, ceiling_from_52w_high

__status__ = "RESEARCH_ONLY"

# bezpazery v1 parameters — frozen instrument tokens (memo §3.3). Restated
# here as the lens defaults restate them; the classify/verify agreement check
# breaks loudly if the lens defaults ever drift from these.
STOP_ATR_MULT = 1.5
TP_ATR_MULT = 1.5
TP_FLOOR_FRAC = 0.006

# §7.1 reason keys, in the order the flow table reports them. Classification
# PRIORITY is the code path's order (see arm_b_null_reason), not this list.
REASONS: tuple[str, ...] = (
    "setup_not_ok_or_missing_stop",
    "no_bars",
    "atr_missing_or_nonpositive",
    "no_fill_walk1",
    "risk_nonpositive",
    "ceiling_at_or_below_cost_floor",
    "bracket_stop_nonpositive",
)


def arm_b_null_reason(
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    *,
    pct_off_52w_high: float | None,
) -> str | None:
    """Why the historical bracket lens returns ``None`` on this row, or ``None``
    when the bracket is constructible and something fills in walk-1.

    Mirrors ``replay_ladder_atr_bracket`` (realised anchor — the lens the
    historical span stamped) check by check, in code order.
    """
    ladder = parse_ladder(trade_setup)
    if not ladder.ok:
        return "setup_not_ok_or_missing_stop"
    if not bars:
        return "no_bars"
    atr = ladder.atr
    if atr is None or not math.isfinite(atr) or atr <= 0:
        return "atr_missing_or_nonpositive"
    # Walk-1 under the lens family contract: no expiries.
    walk = replay_ladder(trade_setup, bars)
    if not walk.entries_filled or walk.blended_entry is None:
        return "no_fill_walk1"
    blended = walk.blended_entry  # realised anchor = blend of touched tiers
    if STOP_ATR_MULT <= 0:
        return "risk_nonpositive"
    # atr_bracket_levels' own order: stop first, ceiling second.
    bracket_stop = blended - STOP_ATR_MULT * atr
    if bracket_stop <= 0:
        return "bracket_stop_nonpositive"
    ceiling = ceiling_from_52w_high(trade_setup, pct_off_52w_high)
    if ceiling is not None and math.isfinite(ceiling):
        tp_floor = blended * (1.0 + TP_FLOOR_FRAC)
        if ceiling <= tp_floor:
            return "ceiling_at_or_below_cost_floor"
    # Cross-check the arithmetic against the shared leaf so a future leaf
    # change cannot leave this mirror silently stale.
    levels = atr_bracket_levels(
        blended,
        atr,
        stop_atr_mult=STOP_ATR_MULT,
        tp_atr_mult=TP_ATR_MULT,
        tp_floor_frac=TP_FLOOR_FRAC,
        ceiling_price=ceiling,
    )
    if levels is None:
        return "bracket_stop_nonpositive"  # unreachable unless the leaf grew a new arm
    return None


@dataclass(frozen=True)
class Verdict:
    reason: str | None
    lens_value: float | None
    agrees: bool


def classify_and_verify(
    trade_setup: Mapping[str, Any] | None,
    bars: Sequence[Mapping[str, Any]],
    *,
    pct_off_52w_high: float | None,
) -> Verdict:
    """Run the mirror AND the real lens; agreement means the mirror's verdict
    (null vs value) matches the production lens on this row."""
    reason = arm_b_null_reason(trade_setup, bars, pct_off_52w_high=pct_off_52w_high)
    ceiling = ceiling_from_52w_high(trade_setup, pct_off_52w_high)
    lens_value = replay_ladder_atr_bracket(
        trade_setup,
        bars,
        anchor="realised",
        stop_atr_mult=STOP_ATR_MULT,
        tp_atr_mult=TP_ATR_MULT,
        tp_floor_frac=TP_FLOOR_FRAC,
        ceiling_price=ceiling,
    )
    return Verdict(
        reason=reason,
        lens_value=lens_value,
        agrees=(lens_value is None) == (reason is not None),
    )


# --------------------------------------------------------------------------
# §7.1 flow table.
# --------------------------------------------------------------------------


def flow_table(rows: pd.DataFrame) -> list[tuple[str, int]]:
    """The §7.1 flow table over classified rows.

    ``rows`` needs boolean columns ``plannable``, ``terminal``,
    ``arm_a_present``, a ``ladder_classification`` column and an
    ``arm_b_reason`` column (``None`` = both arms present). Terminal NO_FILL
    rows carry no arm-A value by construction and get their own level; any
    OTHER terminal row without an arm-A value raises, so imbalance is loud,
    never absorbed.
    """
    total = len(rows)
    not_plannable = int((~rows["plannable"].astype(bool)).sum())
    plannable = rows[rows["plannable"].astype(bool)]
    not_terminal = int((~plannable["terminal"].astype(bool)).sum())
    terminal = plannable[plannable["terminal"].astype(bool)]
    no_arm_a = terminal[~terminal["arm_a_present"].astype(bool)]
    # The store legitimately holds terminal NO_FILL rows with no realized_r
    # (16 in the historical span); anything ELSE terminal without an arm-A
    # value is a genuine contract violation and the table refuses it.
    if not (no_arm_a["ladder_classification"] == "NO_FILL").all():
        raise ValueError(
            "terminal non-NO_FILL rows without an arm-A value violate the "
            "store contract; refusing to build a flow table that would not balance"
        )
    with_arm_a = terminal[terminal["arm_a_present"].astype(bool)]
    table: list[tuple[str, int]] = [
        ("all rows in span", total),
        ("dropped: plannable = False / NO_STRUCTURE", not_plannable),
        ("dropped: not terminal at the read", not_terminal),
        ("terminal: NO_FILL (no arm A value)", len(no_arm_a)),
        ("terminal: arm A value present", len(with_arm_a)),
    ]
    reasons = with_arm_a["arm_b_reason"]
    for reason in REASONS:
        table.append((f"terminal, arm B null: {reason}", int((reasons == reason).sum())))
    table.append(("terminal: both arms present", int(reasons.isna().sum())))
    return table


# --------------------------------------------------------------------------
# §7.2 — does missingness predict arm A's outcome. Day-clustered, one outcome
# column, no second arm anywhere.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MeanDiff:
    diff: float
    ci_low: float
    ci_high: float
    n_indicator: int
    n_rest: int
    n_days: int


def day_cluster_mean_diff(
    rows: pd.DataFrame,
    *,
    indicator: str,
    y: str,
    day: str,
    n_boot: int = 10_000,
    seed: int = 20260824,
) -> MeanDiff:
    """Mean of ``y`` where ``indicator`` is true minus where it is false, with
    a day-cluster bootstrap percentile CI (days resampled with replacement;
    resamples missing either group are skipped and do not count)."""
    flags = rows[indicator].astype(bool).to_numpy()
    values = rows[y].to_numpy(dtype=float)
    days = rows[day].to_numpy()
    point = float(values[flags].mean() - values[~flags].mean())
    unique_days = np.unique(days)
    rng = np.random.default_rng(seed)
    diffs: list[float] = []
    for _ in range(n_boot):
        drawn = rng.choice(unique_days, size=len(unique_days), replace=True)
        mask_parts = [np.flatnonzero(days == d) for d in drawn]
        idx = np.concatenate(mask_parts)
        f, v = flags[idx], values[idx]
        if f.any() and (~f).any():
            diffs.append(float(v[f].mean() - v[~f].mean()))
    if not diffs:
        raise ValueError("no bootstrap resample contained both groups")
    low, high = np.percentile(diffs, [2.5, 97.5])
    return MeanDiff(
        diff=point,
        ci_low=float(low),
        ci_high=float(high),
        n_indicator=int(flags.sum()),
        n_rest=int((~flags).sum()),
        n_days=len(unique_days),
    )


def stored_bracket_null(breakeven_json: str | None) -> bool:
    """Whether the STORED historical lens value is null on this row.

    The stamped ``breakeven_realized_r_json`` either lacks the
    ``atr_bracket_1p5`` key (never stamped) or carries ``null``; unparseable
    payloads count as null — the row demonstrably has no usable value.
    """
    if not isinstance(breakeven_json, str) or not breakeven_json:
        return True
    import json

    try:
        payload = json.loads(breakeven_json)
    except (ValueError, TypeError):
        return True
    if not isinstance(payload, dict):
        return True
    return payload.get("atr_bracket_1p5") is None


# --------------------------------------------------------------------------
# Artifact placement (§10.2): a dedicated directory, never the ladder store.
# --------------------------------------------------------------------------


def ensure_artifact_dir(directory: Path) -> Path:
    """Create/return the artifact directory, refusing the production store."""
    production = Path.home() / ".alphalens" / "population_ladders"
    resolved = directory.expanduser()
    if resolved == production or production in resolved.parents:
        raise SystemExit("refusing to write into the production ladder store")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved
