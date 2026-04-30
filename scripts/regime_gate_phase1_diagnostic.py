"""Regime-gate Phase 1 diagnostic — % OFF days per classifier in IS 2017-2022.

Verifies the Perplexity hypothesis that time-series macro classifiers (C1-C5)
miss the mega-cap-dominance failure mode while a cross-sectional gate (C6)
covers it. Output drives the Phase 2 pre-registration decision: which of the
five classifiers should we actually backtest, and which look so blind to the
window that running them is a waste of multiple-testing budget.

Data sources:
  C1  yield curve     DGS10 - DGS2          inverted   < 0
  C2  VIX             VIXCLS                elevated   > 20
  C3  NFCI            NFCI (weekly, ffill)  stress     > +1
  C5  HY OAS proxy    BAA10Y (see note)     stress     > 2.5  (250bp)
  C6  cross-sectional IWM/SPY closes        IWM 30d ret < 0 AND
                                            SPY 30d ret > rolling 252d median

Note on C5: the literature-anchored series, BAMLH0A0HYM2 (HY OAS, 400bp
threshold), was truncated to a rolling 3-year window by FRED in April 2026,
so it cannot cover 2017-2022. We substitute Moody's Baa-minus-10Y spread
(BAA10Y) with a 250bp threshold — investment-grade Baa, not HY, so the
threshold scale shifts. Coverage % is the diagnostic; the threshold is a
proxy and will be re-anchored against literature in Phase 2 if we end up
running C5 at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from alphalens.macro.fred_client import FREDClient  # noqa: E402
from alphalens.screeners.lean.lean_csv_loader import load_lean_histories  # noqa: E402

IS_START = pd.Timestamp("2017-01-03")
IS_END = pd.Timestamp("2022-12-30")

# Classifier thresholds — see module docstring for rationale.
C1_INVERSION = 0.0
C2_VIX = 20.0
C3_NFCI = 1.0
C5_BAA10Y_PROXY = 2.5
C6_LOOKBACK = 30
C6_MEDIAN_WINDOW = 252


def _trailing_return(closes: pd.Series, lookback: int) -> pd.Series:
    return closes / closes.shift(lookback) - 1.0


def _run_lengths(flags: pd.Series) -> list[int]:
    """Lengths of consecutive True runs in a boolean Series."""
    runs: list[int] = []
    current = 0
    for v in flags.tolist():
        if v:
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    return runs


def _fetch_macro() -> dict[str, pd.Series]:
    fred = FREDClient.from_env()
    return {sid: fred.fetch_series(sid) for sid in ("DGS10", "DGS2", "VIXCLS", "NFCI", "BAA10Y")}


def _build_calendar(spy: pd.DataFrame) -> pd.DatetimeIndex:
    mask = (spy.index >= IS_START) & (spy.index <= IS_END)
    return spy.loc[mask].index


def _align_to_calendar(s: pd.Series, calendar: pd.DatetimeIndex) -> pd.Series:
    """Reindex onto calendar with forward-fill (handles weekly NFCI cleanly)."""
    return s.sort_index().reindex(calendar, method="ffill")


def compute_flags(
    macro: dict[str, pd.Series],
    spy_close: pd.Series,
    iwm_close: pd.Series,
    calendar: pd.DatetimeIndex,
) -> pd.DataFrame:
    dgs10 = _align_to_calendar(macro["DGS10"], calendar)
    dgs2 = _align_to_calendar(macro["DGS2"], calendar)
    vix = _align_to_calendar(macro["VIXCLS"], calendar)
    nfci = _align_to_calendar(macro["NFCI"], calendar)
    baa = _align_to_calendar(macro["BAA10Y"], calendar)

    spy = spy_close.reindex(calendar).ffill()
    iwm = iwm_close.reindex(calendar).ffill()

    spy_30d = _trailing_return(spy, C6_LOOKBACK)
    iwm_30d = _trailing_return(iwm, C6_LOOKBACK)
    spy_30d_med = spy_30d.rolling(C6_MEDIAN_WINDOW).median()

    flags = pd.DataFrame(index=calendar)
    flags["C1_yield_inverted"] = (dgs10 - dgs2) < C1_INVERSION
    flags["C2_vix_gt_20"] = vix > C2_VIX
    flags["C3_nfci_gt_1"] = nfci > C3_NFCI
    flags["C3a_nfci_gt_0"] = nfci > 0.0
    flags["C5_baa10y_gt_2_5"] = baa > C5_BAA10Y_PROXY
    flags["C6_xsec_dispersion"] = (iwm_30d < 0) & (spy_30d > spy_30d_med)
    # Alt C6 specs: original Perplexity gate ("mega-cap dominance regime")
    # might be miscalibrated. Test cleaner formulations to see if the
    # cross-sectional hypothesis is sound but the operationalization is off.
    flags["C6a_iwm_neg_spy_pos"] = (iwm_30d < 0) & (spy_30d > 0)
    flags["C6b_spread_gt_5pct"] = (spy_30d - iwm_30d) > 0.05
    flags["C6c_spread_gt_3pct"] = (spy_30d - iwm_30d) > 0.03

    return flags


def summarise(flags: pd.DataFrame) -> pd.DataFrame:
    rows = []
    n = len(flags)
    for col in flags.columns:
        col_series = flags[col].fillna(False)
        off_days = int(col_series.sum())
        runs = _run_lengths(col_series)
        rows.append(
            {
                "classifier": col,
                "off_days": off_days,
                "off_pct": round(100 * off_days / n, 1),
                "runs": len(runs),
                "mean_run_days": round(sum(runs) / len(runs), 1) if runs else 0.0,
                "max_run_days": max(runs) if runs else 0,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    print(f"IS window: {IS_START.date()} → {IS_END.date()}")

    macro = _fetch_macro()
    lean_data = Path.home() / ".alphalens" / "lean" / "data"
    hist = load_lean_histories(lean_data, ["SPY", "IWM"])

    spy = hist["SPY"]
    calendar = _build_calendar(spy)
    print(f"trading days: {len(calendar)}")

    flags = compute_flags(macro, spy["close"], hist["IWM"]["close"], calendar)
    summary = summarise(flags)

    pd.set_option("display.width", 120)
    pd.set_option("display.max_columns", None)
    print()
    print(summary.to_string(index=False))

    out_dir = Path.home() / ".alphalens" / "regime_gate"
    out_dir.mkdir(parents=True, exist_ok=True)
    flags_path = out_dir / "phase1_daily_flags.csv"
    summary_path = out_dir / "phase1_summary.csv"
    flags.to_csv(flags_path)
    summary.to_csv(summary_path, index=False)
    print(f"\nwrote {flags_path}")
    print(f"wrote {summary_path}")


if __name__ == "__main__":
    main()
