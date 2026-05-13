"""Cross-period diagnostic for v9D options-implied scorer.

Splits the v10-audit pooled returns (5 phase JSONs from
``docs/research/v10_drawdown_overlay/holdout_p[0-4].json``) by calendar
sub-period and recomputes Carhart-4F α t-stat per sub-period. Detects
whether the documented +2.29 mean αt is uniform across the burnt
2024-04-30 → 2026-04-30 holdout, or driven by 1–2 sub-periods.

This is a free diagnostic on data the project already paid for: zero
compute, zero Bonferroni cost (descriptive split, no new hypothesis),
zero HARKing risk (sub-period boundaries are calendar-fixed semesters
locked in this script's source — they are not data-driven choices).

Decision rule (pre-committed in plan file before running):
- ``max(αt) − min(αt) ≤ 2.0`` across sub-periods → **STABLE**
- ``max−min > 2.0`` AND any sub-period αt < 0.5 → **CONCENTRATED**
- ``max−min > 2.0`` but all sub-periods αt ≥ 1.0 → **MIXED**

Output:
- ``docs/research/v9d_cross_period_diagnostic_2026_05_04.md``
- ``docs/research/v9d_cross_period_diagnostic_2026_05_04.json``
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from alphalens.attribution.factor_analysis import run_regression
from alphalens.backtest.metrics import max_drawdown, sharpe
from alphalens.data.factors import load_carhart_daily

PHASE_DIR = Path("docs/research/v10_drawdown_overlay")
PHASES = list(range(5))
CARHART_COLS = ["Mkt-RF", "SMB", "HML", "Mom"]

# Sub-period boundaries — locked here, not data-driven. Four ~6-month
# semesters spanning the burnt holdout window. The first semester is
# slightly short (8 months) to align with the 2024-04-30 audit start.
SUB_PERIODS = [
    ("H2_2024", date(2024, 4, 30), date(2024, 12, 31)),
    ("H1_2025", date(2025, 1, 1), date(2025, 6, 30)),
    ("H2_2025", date(2025, 7, 1), date(2025, 12, 31)),
    ("H1_2026", date(2026, 1, 1), date(2026, 4, 30)),
]

# Pre-committed decision rule thresholds. Locked here before run.
ALPHA_T_RANGE_STABLE = 2.0  # max−min ≤ this → STABLE
ALPHA_T_FLOOR_CONCENTRATED = 0.5  # any < this AND range > stable → CONCENTRATED
ALPHA_T_FLOOR_MIXED = 1.0  # all ≥ this AND range > stable → MIXED


def _load_pooled_base_returns() -> pd.Series:
    """Load 5 phase JSONs, concat raw_returns_for_pooling.base_net into
    one chronologically sorted Series indexed by ``asof`` date."""
    rows: list[tuple[pd.Timestamp, float]] = []
    for p in PHASES:
        payload = json.loads((PHASE_DIR / f"holdout_p{p}.json").read_text())
        rfp = payload["raw_returns_for_pooling"]
        for asof_str, ret in zip(rfp["asof"], rfp["base_net"], strict=True):
            rows.append((pd.Timestamp(asof_str), float(ret)))
    rows.sort(key=lambda r: r[0])
    idx = pd.DatetimeIndex([t for t, _ in rows])
    vals = [v for _, v in rows]
    return pd.Series(vals, index=idx, name="v9d_base_net", dtype=float)


def _slice_period(series: pd.Series, start: date, end: date) -> pd.Series:
    mask = (series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))
    return series.loc[mask]


def _per_period_stats(
    rets: pd.Series, carhart: pd.DataFrame, *, periods_per_year: int = 52
) -> dict:
    """One sub-period summary. ``periods_per_year=52`` because returns are
    per-rebalance (stride=5 ≈ weekly) — same convention as v9D + v10."""
    if len(rets) < 20:
        return {"n": len(rets), "alpha_t": float("nan"), "note": "too few obs"}
    # periods_per_year passes through from the function param (default 52 for
    # weekly stride) so alpha_annualized matches the input cadence. Issue #67.
    res = run_regression(
        rets,
        carhart[[*CARHART_COLS, "RF"]],
        CARHART_COLS,
        periods_per_year=periods_per_year,
    )
    sh = sharpe(rets.tolist(), periods_per_year=periods_per_year)
    cum = (1 + rets.fillna(0)).cumprod()
    mdd = float(max_drawdown(cum.tolist()))
    return {
        "n": len(rets),
        "alpha_gross_4f": float(res.alpha_annualized),
        "alpha_t": float(res.alpha_tstat),
        "sharpe_net": float(sh),
        "max_drawdown": mdd,
        "mean_per_rebal_return": float(rets.mean()),
        "first_asof": str(rets.index.min().date()),
        "last_asof": str(rets.index.max().date()),
    }


def _classify_verdict(per_period: list[dict]) -> tuple[str, str]:
    valid = [p for p in per_period if not pd.isna(p["alpha_t"])]
    if len(valid) < 2:
        return "INCONCLUSIVE", "fewer than 2 valid sub-periods"
    alpha_ts = [p["alpha_t"] for p in valid]
    rng = max(alpha_ts) - min(alpha_ts)
    floor = min(alpha_ts)
    if rng <= ALPHA_T_RANGE_STABLE:
        return "STABLE", (
            f"αt range {rng:.2f} ≤ {ALPHA_T_RANGE_STABLE} → uniform across sub-periods"
        )
    if floor < ALPHA_T_FLOOR_CONCENTRATED:
        return "CONCENTRATED", (
            f"αt range {rng:.2f} > {ALPHA_T_RANGE_STABLE} AND min "
            f"{floor:.2f} < {ALPHA_T_FLOOR_CONCENTRATED} → driven by 1-2 periods"
        )
    if floor >= ALPHA_T_FLOOR_MIXED:
        return "MIXED", (
            f"αt range {rng:.2f} > {ALPHA_T_RANGE_STABLE} but all sub-periods "
            f"≥ {ALPHA_T_FLOOR_MIXED} → real but volatile across regimes"
        )
    return "WEAK", (
        f"αt range {rng:.2f} > {ALPHA_T_RANGE_STABLE}, min {floor:.2f} between "
        f"{ALPHA_T_FLOOR_CONCENTRATED} and {ALPHA_T_FLOOR_MIXED} → marginal"
    )


def main() -> int:
    rets = _load_pooled_base_returns()
    print(f"Loaded {len(rets)} pooled obs from {rets.index.min()} to {rets.index.max()}")

    full_window_start = rets.index.min().date()
    full_window_end = rets.index.max().date()
    # Carhart factors loaded once across full window (with 30d buffer for HAC).
    from datetime import timedelta

    carhart = load_carhart_daily(
        start=full_window_start - timedelta(days=30),
        end=full_window_end + timedelta(days=30),
    )

    full_stats = _per_period_stats(rets, carhart)
    print(
        f"Full-window sanity: n={full_stats['n']} αt={full_stats['alpha_t']:+.2f} "
        f"Sh={full_stats['sharpe_net']:.2f} MDD={full_stats['max_drawdown'] * 100:+.1f}%"
    )

    per_period: list[dict] = []
    for label, start, end in SUB_PERIODS:
        sub = _slice_period(rets, start, end)
        stats = _per_period_stats(sub, carhart)
        stats["label"] = label
        stats["window"] = [str(start), str(end)]
        per_period.append(stats)
        if "alpha_t" in stats and not pd.isna(stats["alpha_t"]):
            print(
                f"  {label} ({start}..{end}): n={stats['n']} αt={stats['alpha_t']:+.2f} "
                f"Sh={stats['sharpe_net']:.2f} MDD={stats['max_drawdown'] * 100:+.1f}%"
            )
        else:
            print(f"  {label}: insufficient obs ({stats.get('n', 0)})")

    verdict, rationale = _classify_verdict(per_period)
    valid = [p for p in per_period if not pd.isna(p["alpha_t"])]
    alpha_ts = [p["alpha_t"] for p in valid]

    payload = {
        "diagnostic_id": "v9d_cross_period_2026_05_04",
        "data_source": "docs/research/v10_drawdown_overlay/holdout_p[0-4].json",
        "n_pooled_obs": len(rets),
        "full_window_stats": full_stats,
        "sub_periods": per_period,
        "alpha_t_range": float(max(alpha_ts) - min(alpha_ts)) if alpha_ts else None,
        "alpha_t_min": float(min(alpha_ts)) if alpha_ts else None,
        "alpha_t_max": float(max(alpha_ts)) if alpha_ts else None,
        "alpha_t_mean": float(sum(alpha_ts) / len(alpha_ts)) if alpha_ts else None,
        "decision_rule": {
            "stable_range_threshold": ALPHA_T_RANGE_STABLE,
            "concentrated_floor_threshold": ALPHA_T_FLOOR_CONCENTRATED,
            "mixed_floor_threshold": ALPHA_T_FLOOR_MIXED,
        },
        "verdict": verdict,
        "rationale": rationale,
    }

    out_json = Path("docs/research/v9d_cross_period_diagnostic_2026_05_04.json")
    out_md = Path("docs/research/v9d_cross_period_diagnostic_2026_05_04.md")
    out_json.write_text(json.dumps(payload, indent=2, default=str))

    md_lines = [
        f"# v9D cross-period diagnostic — verdict: **{verdict}**",
        "",
        f"**Rationale:** {rationale}",
        "",
        f"Data source: `{payload['data_source']}` (5 phase JSONs from v10 audit, "
        f"pooled into {len(rets)} dated rebalance returns).",
        "",
        f"Full window {rets.index.min().date()} → {rets.index.max().date()}: "
        f"αt = **{full_stats['alpha_t']:+.2f}** "
        f"(Sharpe net {full_stats['sharpe_net']:.2f}, "
        f"MaxDD {full_stats['max_drawdown'] * 100:+.1f}%).",
        "",
        "## Per-sub-period stats",
        "",
        "| Sub-period | Window | n | αt (Carhart-4F, HAC) | Sharpe net | MaxDD | mean per-rebal |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in per_period:
        if pd.isna(p["alpha_t"]):
            md_lines.append(
                f"| {p['label']} | {p['window'][0]}..{p['window'][1]} | {p['n']} | — | — | — | — |"
            )
        else:
            md_lines.append(
                f"| {p['label']} | {p['window'][0]}..{p['window'][1]} | "
                f"{p['n']} | {p['alpha_t']:+.2f} | {p['sharpe_net']:.2f} | "
                f"{p['max_drawdown'] * 100:+.1f}% | {p['mean_per_rebal_return'] * 100:+.2f}% |"
            )

    md_lines += [
        "",
        f"**Cross-period αt range:** {payload['alpha_t_range']:.2f} "
        f"(min {payload['alpha_t_min']:+.2f}, max {payload['alpha_t_max']:+.2f}, "
        f"mean {payload['alpha_t_mean']:+.2f}).",
        "",
        "## Decision-rule application",
        "",
        f"- STABLE if αt range ≤ {ALPHA_T_RANGE_STABLE}",
        f"- CONCENTRATED if range > {ALPHA_T_RANGE_STABLE} AND any sub-period αt < {ALPHA_T_FLOOR_CONCENTRATED}",
        f"- MIXED if range > {ALPHA_T_RANGE_STABLE} but all sub-periods αt ≥ {ALPHA_T_FLOOR_MIXED}",
        "",
        f"## Verdict: **{verdict}**",
        "",
        rationale,
        "",
        "## Implications for H+ paper-trade prospective track",
        "",
        {
            "STABLE": (
                "+2.29 αt is uniform across the burnt holdout. H+ priors stay high — "
                "proceed to Phase 2 (paper-trade infrastructure setup) with high confidence "
                "that fresh-window replication is plausible."
            ),
            "MIXED": (
                "+2.29 αt is real across all sub-periods but with substantial variance. "
                "H+ priors are reduced but the signal persists. Proceed to Phase 2 with "
                "elevated regime-risk surveillance — flag in pre-reg that any single "
                "sub-period below αt=1.5 in prospective tracking should trigger early review."
            ),
            "CONCENTRATED": (
                "+2.29 αt is driven by 1-2 sub-periods; one or more sub-periods show "
                "αt < 0.5. **Posterior on regime-stability drops sharply.** Reconsider "
                "Phase 2 commitment — the +2.29 may be a regime-specific artifact that "
                "won't replicate forward. Return to user with revised priors."
            ),
            "WEAK": (
                "Marginal classification. Range exceeds stable threshold but no sub-period "
                "is severely below. Treat as borderline MIXED with extra caution."
            ),
            "INCONCLUSIVE": (
                "Insufficient data to classify. Re-examine sub-period boundaries or n."
            ),
        }.get(verdict, "Unknown verdict — manual review required."),
        "",
    ]

    out_md.write_text("\n".join(md_lines) + "\n")
    print(f"\nVERDICT: {verdict}")
    print(
        f"  Range: {payload['alpha_t_range']:.2f}, min {payload['alpha_t_min']:+.2f}, max {payload['alpha_t_max']:+.2f}"
    )
    print(f"\n→ {out_json}")
    print(f"→ {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
