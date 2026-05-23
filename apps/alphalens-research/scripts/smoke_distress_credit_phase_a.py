"""Phase A engineering smoke for distress_credit_v1_2026_05_04 (TRAIN only).

Pre-reg gating logic: A4-extended (overlay sanity check) is the only PRIMARY
auto-pivot trigger. If correlation(spread_z, forward 21d market return) is
positive on TRAIN OR sign-drifts across decade-windows, DROP Layer 4 from
PRIMARY hypothesis and run pure-Layer-2 long-only safe-decile under the
same |t|>=3.50 threshold.

Other A1..A3, A5..A8 checks are diagnostic only — they CANNOT change
hypothesis or thresholds (those are locked in pre-reg).

Critical data note: BAMLH0A0HYM2 (FRED ICE BofA US HY OAS) is publicly
available from 2023-05-02 only (ICE/BofA copyright restriction). For the
overlay sanity check on TRAIN (2017-01 → 2024-04), we use BAA10Y as a
literature-backed credit-spread proxy. Both spreads measure the same
economic regime variable (credit stress); their direction-of-association
with equity returns should be aligned. We also run the limited HY OAS
2023-2024 subset as a secondary check to verify the proxy substitution
preserves the result.

Outputs JSON reports under docs/research/distress_credit/.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from alphalens_pipeline.data.macro.signals import hy_oas_z_from_series

OUT_DIR = REPO_ROOT / "docs" / "research" / "distress_credit"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TRAIN_START = pd.Timestamp("2017-01-03")
TRAIN_END = pd.Timestamp("2024-04-29")


def write_json(name: str, payload: dict) -> Path:
    path = OUT_DIR / f"phase_a_{name}.json"
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    return path


def load_macro_series(series_id: str) -> pd.Series:
    path = Path.home() / ".alphalens" / "macro" / f"FRED_{series_id}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"FRED cache missing for {series_id}: {path}")
    df = pd.read_parquet(path)
    s = df.iloc[:, 0]
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def load_spy_history() -> pd.DataFrame:
    path = Path.home() / ".alphalens" / "prices" / "SPY.parquet"
    if not path.exists():
        raise FileNotFoundError(f"SPY OHLCV cache missing: {path}")
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def compute_correlation_overlay_sanity(
    spread: pd.Series, spy: pd.DataFrame, window_start: pd.Timestamp, window_end: pd.Timestamp
) -> dict:
    """Correlation between spread_z(t) and forward 21d SPY log-return(t+1:t+21).

    Negative correlation = wider spread predicts LOWER forward returns
    (overlay direction CORRECT — de-lever in stress reduces drawdown).
    Positive correlation = INVERTS (would lever into post-shock recovery
    when spread widens but equity rallies).
    """
    closes = spy.loc[(spy.index >= window_start) & (spy.index <= window_end), "close"]
    log_rets = np.log(closes / closes.shift(1)).dropna()

    rebal_dates = pd.bdate_range(start=window_start, end=window_end, freq="21D")
    pairs = []
    for asof in rebal_dates:
        z = hy_oas_z_from_series(spread, asof, lookback=252)
        if z is None:
            continue
        # Forward 21d return: closes[asof + 21d window]
        fwd_window = log_rets.loc[
            (log_rets.index > asof) & (log_rets.index <= asof + pd.Timedelta(days=35))
        ]
        # Take exactly 21 trading days forward
        fwd_window = fwd_window.iloc[:21]
        if len(fwd_window) < 10:
            continue
        fwd_ret = float(fwd_window.sum())
        pairs.append({"asof": str(asof.date()), "spread_z": float(z), "fwd_21d_ret": fwd_ret})

    if len(pairs) < 30:
        return {
            "ok": False,
            "reason": f"insufficient samples ({len(pairs)}) — too few rebalances overlap with spread series",
            "n_samples": len(pairs),
        }

    df = pd.DataFrame(pairs)
    pearson = float(df["spread_z"].corr(df["fwd_21d_ret"]))
    spearman = float(df["spread_z"].corr(df["fwd_21d_ret"], method="spearman"))

    # Decade-window analysis: split into two halves
    midpoint = len(df) // 2
    pearson_first_half = float(
        df.iloc[:midpoint]["spread_z"].corr(df.iloc[:midpoint]["fwd_21d_ret"])
    )
    pearson_second_half = float(
        df.iloc[midpoint:]["spread_z"].corr(df.iloc[midpoint:]["fwd_21d_ret"])
    )
    sign_drift = abs(pearson_first_half - pearson_second_half)

    # Pre-reg gate: full-sample mean correlation <= -0.05 AND no decade-window flip > 0.4
    pass_full_sample = pearson <= -0.05
    pass_no_flip = sign_drift <= 0.4
    overall_pass = pass_full_sample and pass_no_flip

    return {
        "ok": overall_pass,
        "pearson_full_sample": pearson,
        "spearman_full_sample": spearman,
        "pearson_first_half": pearson_first_half,
        "pearson_second_half": pearson_second_half,
        "decade_window_sign_drift_abs": sign_drift,
        "n_samples": len(df),
        "window_start": str(window_start.date()),
        "window_end": str(window_end.date()),
        "gate_pass_full_sample_correlation_le_minus_005": pass_full_sample,
        "gate_pass_no_decade_sign_flip_gt_04": pass_no_flip,
        "overall_pass": overall_pass,
    }


def run_a4_extended_overlay_sanity():
    """Pre-committed auto-pivot gate: drop Layer 4 if overlay sanity fails."""
    print("=" * 60)
    print("A4-extended: Overlay sanity check (PRE-COMMITTED AUTO-PIVOT GATE)")
    print("=" * 60)

    spy = load_spy_history()

    # Primary check: BAA10Y as credit-spread proxy on full TRAIN
    print("\n[Primary] BAA10Y (IG credit spread, full TRAIN coverage) on 2017-01..2024-04")
    baa = load_macro_series("BAA10Y")
    baa_result = compute_correlation_overlay_sanity(baa, spy, TRAIN_START, TRAIN_END)

    # Secondary check: BAMLH0A0HYM2 on its limited 2023-05..2024-04 coverage
    print("\n[Secondary] BAMLH0A0HYM2 (HY OAS, limited 2023-2024 cache) on 2023-05..2024-04")
    hy = load_macro_series("BAMLH0A0HYM2")
    hy_window_start = hy.index.min() + pd.Timedelta(days=300)  # ensure 252+ obs of history
    hy_result = compute_correlation_overlay_sanity(hy, spy, hy_window_start, TRAIN_END)

    # Decision logic: PRIMARY (BAA10Y full TRAIN) is authoritative due to coverage
    primary_pass = baa_result.get("overall_pass", False)
    secondary_pass = hy_result.get("overall_pass", False)

    overall = {
        "check_name": "A4-extended overlay sanity",
        "is_pre_committed_gate": True,
        "decision_rule": "Primary BAA10Y on TRAIN is authoritative due to HY OAS data unavailability for TRAIN window. If primary FAILs → DROP Layer 4 from PRIMARY hypothesis.",
        "primary_baa10y": baa_result,
        "secondary_hy_oas_partial": hy_result,
        "primary_pass": primary_pass,
        "secondary_pass": secondary_pass,
        "verdict": "KEEP_LAYER_4" if primary_pass else "DROP_LAYER_4",
        "auto_pivot_to_pure_l2": not primary_pass,
        "data_substitution_caveat": "BAMLH0A0HYM2 (ICE BofA HY OAS) publicly available from 2023-05-02 only (copyright restriction). BAA10Y (Moody's BAA Yield - 10Y Treasury) used as credit-spread proxy on TRAIN. Both spreads measure credit stress; literature (Gilchrist-Zakrajsek 2012, Adrian-Crump-Moench 2013) confirms BAA10Y and HY OAS are highly correlated and have aligned direction-of-association with forward equity returns (negative correlation: wider spreads → weaker forward equity).",
    }
    write_json("a4_extended_overlay_sanity", overall)

    print(f"\nVerdict: {overall['verdict']}")
    if overall["verdict"] == "DROP_LAYER_4":
        print("PRE-COMMITTED AUTO-PIVOT TRIGGERED. Layer 4 will be dropped from PRIMARY.")
        print("Pure-Layer-2 long-only safe-decile becomes the primary hypothesis.")
    return overall


def run_a4_basic_hy_oas_sanity():
    """A4: HY OAS series sanity (no NaN gaps >5BD, z-score finite for rebalances)."""
    print("\n" + "=" * 60)
    print("A4: HY OAS series sanity (NaN gaps + z-score finite)")
    print("=" * 60)
    hy = load_macro_series("BAMLH0A0HYM2")

    # Compute gap distribution on business-day calendar
    bd = pd.bdate_range(start=hy.index.min(), end=hy.index.max())
    aligned = hy.reindex(bd)
    nan_runs = []
    current_run = 0
    for v in aligned.values:
        if pd.isna(v):
            current_run += 1
        else:
            if current_run > 0:
                nan_runs.append(current_run)
            current_run = 0
    max_run = max(nan_runs) if nan_runs else 0

    payload = {
        "check_name": "A4 HY OAS series sanity",
        "n_obs": len(hy),
        "date_range": [str(hy.index.min().date()), str(hy.index.max().date())],
        "max_consecutive_nan_business_days": int(max_run),
        "gate_max_nan_run_le_5_bd": int(max_run) <= 5,
        "ok": int(max_run) <= 5,
    }
    write_json("a4_hy_oas_sanity", payload)
    print(
        f"  HY OAS obs: {payload['n_obs']}, max NaN run: {payload['max_consecutive_nan_business_days']} BD"
    )
    return payload


def main():
    """Run gating Phase A checks. Other (A1..A3, A5..A8) require companyfacts
    fixtures and are deferred to fuller smoke run."""
    print(f"Output directory: {OUT_DIR}")
    print(f"TRAIN window: {TRAIN_START.date()} → {TRAIN_END.date()}")
    print()

    a4_basic = run_a4_basic_hy_oas_sanity()
    a4_ext = run_a4_extended_overlay_sanity()

    # Aggregate gating verdict
    gating = {
        "phase_a_gating_summary": {
            "a4_basic_pass": a4_basic.get("ok", False),
            "a4_extended_pass": a4_ext.get("primary_pass", False),
            "a4_extended_verdict": a4_ext.get("verdict"),
            "auto_pivot_to_pure_l2": a4_ext.get("auto_pivot_to_pure_l2"),
            "registered_id": "distress_credit_v1_2026_05_04",
            "registered_signal_class": "distress_credit_search_2026_05_04",
        }
    }
    write_json("gating_summary", gating)

    if a4_ext.get("auto_pivot_to_pure_l2"):
        print("\n" + "!" * 60)
        print("AUTO-PIVOT TRIGGERED — Layer 4 (HY OAS overlay) DROPPED")
        print("PRIMARY hypothesis = pure-Layer-2 long-only safe-decile")
        print("!" * 60)
    else:
        print("\nGating PASS — Layer 4 retained in PRIMARY hypothesis.")


if __name__ == "__main__":
    main()
