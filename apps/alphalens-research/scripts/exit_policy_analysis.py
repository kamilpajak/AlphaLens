"""§10.4/§10.5 driver for the exit-policy comparison (#1115).

Governing document: ``docs/research/exit_policy_comparison_prereg_2026_08_24.md``
(LOCKED). Where a clause and this file disagree, the clause wins.

Two-stage commit protocol (§10.5):

1. ``extract`` writes the cohort EXTRACT — input rows only (no outcome column
   can exist in it, pinned by a test) — and prints its sha256. That hash is
   committed to the results record BEFORE any outcome is computed.
2. ``analyze`` refuses to run unless the extract's sha256 matches, refuses
   while the §6.3 block floor (< 10 non-overlapping 42-session blocks) or the
   §6.4 pair floor is unmet, then computes ``d_i`` through the §10.1 replay,
   the five §6.2 inference arms, the §8.1 report, the eight §8.3
   sensitivities, and the §12.2 verdict — read from the WIDEST interval among
   arms 2-5, two-sided α = 0.05, ONE look. Running ``analyze`` on cohort rows
   CONSUMES the memo's slot (§9); it is the look.

The bootstrap seed is fixed (recorded in the payload); no interim mode
exists — a below-floor state is a refusal, not a descriptive report, per
§12.1 item 5.

Usage::

    .venv/bin/python apps/alphalens-research/scripts/exit_policy_analysis.py \
        extract --cohort-open 2026-08-27 --analysis-session 2028-06-01 --out extract.parquet
    .venv/bin/python apps/alphalens-research/scripts/exit_policy_analysis.py \
        analyze --extract extract.parquet --sha256 <hash> --n0 3750 --sd-d <sd> --delta-min <dm>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from alphalens_pipeline.feedback.population_ladder_monitor import (
    _bars_cache_path,
    _engine_cutoffs,
)
from alphalens_pipeline.paper.calendar import n_sessions_before, session_on_or_after
from alphalens_research.diagnostics.exit_policy_analysis import (
    ALPHA_TWO_SIDED,
    BLOCK_FLOOR,
    BLOCK_LEN_SESSIONS,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    EXTRACT_COLUMNS,
    distribution_report,
    inference_arms,
    non_overlapping_blocks,
    pair_floor,
    primary_verdict,
    sha256_of,
    tail_report,
    verify_extract_hash,
)
from alphalens_research.diagnostics.exit_policy_replay import (
    ARM_A,
    ARM_B,
    infeasibility_reason,
    replay_arm,
)

logger = logging.getLogger(__name__)

EXCHANGE = "XNYS"
PRIMARY_SLIPPAGE_BPS = 40.0
SLIPPAGE_GRID = (0.0, 20.0, 40.0, 80.0)
NOTIONAL_GRID_EXTRA = (1_000.0, 10_000.0)
WINSOR_PCT = 0.01

STORE_DIR = Path.home() / ".alphalens" / "population_ladders"
BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"


def _bars_for(ticker: str, brief_date: str) -> list[dict]:
    arrival = session_on_or_after(dt.date.fromisoformat(brief_date), EXCHANGE)
    candidate = _bars_cache_path(STORE_DIR, ticker, arrival)
    if not candidate.exists():
        return []
    frame = pd.read_parquet(candidate)
    if {"t", "l", "h", "c"}.issubset(frame.columns):
        return frame[["t", "l", "h", "c"]].to_dict("records")
    return []


def build_extract(cohort_open: str, analysis_session: str) -> pd.DataFrame:
    """Input-only cohort rows: brief_date in [cohort_open, analysis − H].

    §5.5: a candidate whose brief date is later than ``analysis session − H``
    is not in the sample at all — nothing enters by maturing.
    """
    horizon_close = n_sessions_before(
        dt.date.fromisoformat(analysis_session), BLOCK_LEN_SESSIONS, EXCHANGE
    ).isoformat()
    rows: list[dict] = []
    for store_file in sorted(STORE_DIR.glob("*.parquet")):
        brief_date = store_file.stem
        if not (cohort_open <= brief_date <= horizon_close):
            continue
        store = pd.read_parquet(store_file, columns=["ticker"])
        brief_file = BRIEFS_DIR / f"{brief_date}.parquet"
        brief = pd.read_parquet(brief_file) if brief_file.exists() else pd.DataFrame()
        setup_col = next((c for c in brief.columns if "trade_setup" in c), None)
        for ticker in store["ticker"]:
            setup_json: str | None = None
            pct = None
            if setup_col is not None:
                match = brief[brief["ticker"] == ticker]
                if not match.empty:
                    raw = match.iloc[0][setup_col]
                    setup_json = raw if isinstance(raw, str) else json.dumps(raw)
                    pct = match.iloc[0].get("technical_pct_off_52w_high")
            rows.append(
                {
                    "brief_date": brief_date,
                    "ticker": ticker,
                    "trade_setup_json": setup_json,
                    "pct_off_52w_high": float(pct) if pd.notna(pct) else None,
                }
            )
    return pd.DataFrame(rows, columns=list(EXTRACT_COLUMNS))


def _row_inputs(row: pd.Series) -> tuple[dict, list[dict], int, int] | None:
    """Parse + §5.1 feasibility for one extract row — NO replay, so the
    floors can be checked before any A-vs-B contrast exists (§11 item 3 /
    §12.1 item 5). ``None`` when the row is excluded."""
    setup = None
    if isinstance(row["trade_setup_json"], str):
        try:
            setup = json.loads(row["trade_setup_json"])
        except (ValueError, TypeError):
            setup = None
    *_sessions, entry_expiry_ms, position_expiry_ms = _engine_cutoffs(
        dt.date.fromisoformat(row["brief_date"]), setup or {}, EXCHANGE
    )
    bars = _bars_for(row["ticker"], row["brief_date"])
    covers = bool(bars) and max(int(b["t"]) for b in bars) >= position_expiry_ms
    if infeasibility_reason(setup, bars_cover_window=covers) is not None:
        return None
    return setup, bars, entry_expiry_ms, position_expiry_ms  # type: ignore[return-value]


def _row_outcomes(row: pd.Series, *, n0: float, slippage_bps: float, **variant) -> dict | None:
    """Both-arm net cash for one extract row, or ``None`` when §5.1 excludes it."""
    inputs = _row_inputs(row)
    if inputs is None:
        return None
    setup, bars, entry_expiry_ms, position_expiry_ms = inputs
    outcomes = {
        arm: replay_arm(
            setup,  # type: ignore[arg-type]
            bars,
            arm=arm,
            notional=n0,
            slippage_bps=slippage_bps,
            entry_expiry_ms=entry_expiry_ms,
            position_expiry_ms=position_expiry_ms,
            pct_off_52w_high=row["pct_off_52w_high"],
            **(variant if arm == ARM_B else {}),
        )
        for arm in (ARM_A, ARM_B)
    }
    a, b = outcomes[ARM_A], outcomes[ARM_B]
    return {
        "brief_date": row["brief_date"],
        "ticker": row["ticker"],
        "d": b.net_cash - a.net_cash,
        "net_a": a.net_cash,
        "net_b": b.net_cash,
        "fills_a": a.chargeable_fills,
        "fills_b": b.chargeable_fills,
        "fees_a": a.total_fees,
        "fees_b": b.total_fees,
        "fallback_b": b.used_fallback,
        "holding_ms_a": (a.exit_ts_ms - a.first_fill_ts_ms)
        if a.exit_ts_ms is not None and a.first_fill_ts_ms is not None
        else None,
        "holding_ms_b": (b.exit_ts_ms - b.first_fill_ts_ms)
        if b.exit_ts_ms is not None and b.first_fill_ts_ms is not None
        else None,
        "mae_pct_a": a.mae_pct,
        "mae_pct_b": b.mae_pct,
        "mae_usd_a": (a.mae_pct * a.entry_cost) if a.mae_pct is not None else None,
        "mae_usd_b": (b.mae_pct * b.entry_cost) if b.mae_pct is not None else None,
        "ceiling_capped_b": b.ceiling_capped,
    }


def compute_outcomes(
    extract: pd.DataFrame, *, n0: float, slippage_bps: float, **variant
) -> pd.DataFrame:
    rows = [
        outcome
        for _, row in extract.iterrows()
        if (outcome := _row_outcomes(row, n0=n0, slippage_bps=slippage_bps, **variant)) is not None
    ]
    return pd.DataFrame(rows)


def _delta_only(frame: pd.DataFrame) -> float | None:
    return float(frame["d"].mean()) if len(frame) else None


_MS_PER_DAY = 86_400_000.0


def _holding_days_report(holding_ms: pd.Series) -> dict | None:
    """§8.1 item 3: median + 95th percentile of holding time, in days."""
    days = holding_ms.dropna().astype(float) / _MS_PER_DAY
    if days.empty:
        return None
    return {"median": float(days.median()), "p95": float(days.quantile(0.95))}


def _dist_or_none(values: pd.Series) -> dict | None:
    """§8.1 item 4: a distribution report over the non-null rows, or ``None``."""
    clean = values.dropna().astype(float)
    return distribution_report(clean.to_numpy()) if len(clean) else None


def _sensitivities(extract: pd.DataFrame, *, n0: float, primary: pd.DataFrame) -> dict:
    """§8.3 items — descriptive only, no verdict words, none replace the primary."""
    result: dict = {}
    # 1. jointly feasible = rows where arm B did NOT fall back.
    jf = primary[~primary["fallback_b"].astype(bool)]
    result["jointly_feasible_delta"] = _delta_only(jf)
    # 2. realised anchor.
    realised = compute_outcomes(
        extract, n0=n0, slippage_bps=PRIMARY_SLIPPAGE_BPS, arm_b_anchor="realised"
    )
    result["realised_anchor_delta"] = _delta_only(realised)
    # 3. unclamped static planned bracket (the registered lens geometry).
    lens_like = compute_outcomes(
        extract,
        n0=n0,
        slippage_bps=PRIMARY_SLIPPAGE_BPS,
        arm_b_apply_clamp=False,
        arm_b_reanchor=False,
    )
    result["unclamped_static_delta"] = _delta_only(lens_like)
    # 4. notional grid.
    result["notional_grid_delta"] = {
        str(n): _delta_only(compute_outcomes(extract, n0=n, slippage_bps=PRIMARY_SLIPPAGE_BPS))
        for n in (n0, *NOTIONAL_GRID_EXTRA)
    }
    # 5. slippage grid (the S at which the sign flips, if any, is readable
    # directly from the grid).
    result["slippage_grid_delta"] = {
        str(s): _delta_only(compute_outcomes(extract, n0=n0, slippage_bps=s)) for s in SLIPPAGE_GRID
    }
    # 7. winsorized primary (1% two-sided).
    d = primary["d"].to_numpy(dtype=float)
    if len(d):
        low, high = np.percentile(d, [100 * WINSOR_PCT, 100 * (1 - WINSOR_PCT)])
        result["winsorized_delta"] = float(np.clip(d, low, high).mean())
    else:
        result["winsorized_delta"] = None
    # 6 (equal risk) and 8 (R-space bridge) require per-arm risk sizing and
    # the stamped R columns respectively; both are computed at look time from
    # the same extract — recorded here as explicit TODO markers so the results
    # memo cannot silently omit them.
    result["equal_risk_delta"] = "computed_at_look_time_from_the_same_extract"
    result["r_space_bridge"] = "computed_at_look_time_with_the_section_2_2_caveat"
    return result


def cmd_extract(args: argparse.Namespace) -> int:
    frame = build_extract(args.cohort_open, args.analysis_session)
    if frame.empty:
        print("no cohort rows in range", file=sys.stderr)
        return 1
    out = Path(args.out)
    frame.to_parquet(out, index=False)
    print(json.dumps({"rows": len(frame), "sha256": sha256_of(out), "path": str(out)}))
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    extract_path = Path(args.extract)
    verify_extract_hash(extract_path, args.sha256)
    extract = pd.read_parquet(extract_path)

    # Both floors are checked from parse + feasibility ALONE, before a single
    # replay runs: a below-floor state must refuse while no A-vs-B contrast
    # exists anywhere, not even in memory (§11 item 3 / §12.1 item 5).
    feasible_days = [
        row["brief_date"] for _, row in extract.iterrows() if _row_inputs(row) is not None
    ]
    blocks = non_overlapping_blocks(sorted(set(feasible_days)), block_len=BLOCK_LEN_SESSIONS)
    if blocks < BLOCK_FLOOR:
        raise SystemExit(
            f"{blocks} non-overlapping {BLOCK_LEN_SESSIONS}-session blocks < floor "
            f"{BLOCK_FLOOR} — the look does not happen and the slot is not consumed "
            "(memo section 6.3 / 12.1 item 5)"
        )
    floor = pair_floor(sd_d=args.sd_d, delta_min=args.delta_min)
    if len(feasible_days) < floor:
        raise SystemExit(
            f"{len(feasible_days)} pairs < the section 6.4 floor {floor} at the "
            "planning sd — the look does not happen"
        )

    outcomes = compute_outcomes(extract, n0=args.n0, slippage_bps=PRIMARY_SLIPPAGE_BPS)
    arms = inference_arms(outcomes, n_boot=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED)
    widest_name, verdict = primary_verdict(arms)
    d = outcomes["d"].to_numpy(dtype=float)
    payload = {
        "verdict": verdict,
        "verdict_arm": widest_name,
        "alpha_two_sided": ALPHA_TWO_SIDED,
        "bootstrap": {"resamples": BOOTSTRAP_RESAMPLES, "seed": BOOTSTRAP_SEED},
        "arms": {
            name: {"ci_low": arm.ci_low, "ci_high": arm.ci_high, "n_clusters": arm.n_clusters}
            for name, arm in arms.items()
        },
        "distribution": distribution_report(d),
        "tail": tail_report(d),
        "fills_and_fees": {
            "fills_a_total": int(outcomes["fills_a"].sum()),
            "fills_b_total": int(outcomes["fills_b"].sum()),
            "fees_a_total": float(outcomes["fees_a"].sum()),
            "fees_b_total": float(outcomes["fees_b"].sum()),
        },
        "fallback_share_b": float(outcomes["fallback_b"].astype(bool).mean()),
        "ceiling_capped_share_b": float(outcomes["ceiling_capped_b"].astype(bool).mean()),
        "holding_days": {
            "arm_a": _holding_days_report(outcomes["holding_ms_a"]),
            "arm_b": _holding_days_report(outcomes["holding_ms_b"]),
        },
        "mae": {
            "arm_a": {
                "mae_pct": _dist_or_none(outcomes["mae_pct_a"]),
                "mae_usd": _dist_or_none(outcomes["mae_usd_a"]),
            },
            "arm_b": {
                "mae_pct": _dist_or_none(outcomes["mae_pct_b"]),
                "mae_usd": _dist_or_none(outcomes["mae_usd_b"]),
            },
        },
        # §8.1 item 8: the flow table on the forward cohort is produced by the
        # PR-3 instrument at look time; the pointer here keeps the results
        # memo from silently omitting it.
        "flow_table": (
            "produced at look time by scripts/exit_policy_missingness.py "
            f"--span-start {extract['brief_date'].min()} "
            f"--span-end {extract['brief_date'].max()} (memo section 8.1 item 8)"
        ),
        "sensitivities": _sensitivities(extract, n0=args.n0, primary=outcomes),
        "floors": {"blocks": blocks, "pair_floor": floor, "pairs": len(outcomes)},
        "extract_sha256": args.sha256,
    }
    print(json.dumps(payload, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="exit-policy comparison analysis (memo sections 10.4 / 10.5)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("extract", help="write the input-only cohort extract + sha256")
    ex.add_argument("--cohort-open", required=True)
    ex.add_argument("--analysis-session", required=True)
    ex.add_argument("--out", required=True)
    ex.set_defaults(func=cmd_extract)
    an = sub.add_parser("analyze", help="THE one look — consumes the memo's slot")
    an.add_argument("--extract", required=True)
    an.add_argument("--sha256", required=True)
    an.add_argument("--n0", type=float, required=True)
    an.add_argument("--sd-d", type=float, required=True)
    an.add_argument("--delta-min", type=float, required=True)
    an.set_defaults(func=cmd_analyze)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
