"""v7 Phase A smoke test — feature joiner end-to-end on a small universe.

Pulls smd cache for ~30 hand-picked mega-cap + mid-cap + distress tickers,
runs the feature joiner over 2024-Q4 asofs, reports Phase A gates per
pre-reg `phase_a_gates`:
- coverage ≥ 70% non-NaN feature rows / (universe × asofs)
- max pairwise |corr| < 0.85

If both gates PASS → green light for full pull (~2000 tickers).
If either FAILs → ABORT pre Phase B; investigate before scaling.

Run:
    ALPHALENS_IVOL_API_KEY=... .venv/bin/python scripts/smoke_v7_phase_a.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ivolatility as ivol
import pandas as pd
from alphalens_research.data.alt_data.ivolatility_smd_cache import (
    download_and_cache,
    load_cached_smd,
)
from alphalens_research.screeners.options_implied import (
    FEATURE_NAMES,
    build_feature_frame,
    multicollinearity_drop_recommendation,
    validate_phase_a_gates,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SMD_CACHE_DIR = Path.home() / ".alphalens" / "ivolatility_smd"
OUTPUT_JSON = REPO_ROOT / "docs" / "research" / "v7_phase_a_smoke_2026_05_02.json"
OUTPUT_MD = REPO_ROOT / "docs" / "research" / "v7_phase_a_smoke_2026_05_02.md"

# Smoke universe — diverse cross-section spanning sectors + caps + 2 distress
# tickers (SIVB included as known iVol smd retainer per probe v5 99.5% T1).
SMOKE_UNIVERSE = [
    # Mega-cap tech
    "AAPL",
    "MSFT",
    "GOOGL",
    "NVDA",
    "AMZN",
    "META",
    "TSLA",
    # Financials
    "JPM",
    "BAC",
    "V",
    "MA",
    "BRK.B",
    # Healthcare / pharma
    "UNH",
    "JNJ",
    "PFE",
    "MRK",
    "LLY",
    "ABBV",
    # Energy / industrials
    "XOM",
    "CVX",
    "CAT",
    # Staples / discretionary
    "WMT",
    "PG",
    "KO",
    "PEP",
    "HD",
    "COST",
    # Tech / software
    "ORCL",
    "ADBE",
    "CRM",
    "NFLX",
    "AVGO",
    # Distress (delisted) — pre-halt asofs only
    "SIVB",
    "FRC",
]


def _build_calendar(start: date, end: date, stride: int = 5) -> list[date]:
    """Approximate trading calendar via business days, strided by `stride`."""
    bdays = pd.bdate_range(start, end)
    return [d.date() for d in bdays[::stride]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--smoke-start",
        type=date.fromisoformat,
        default=date(2018, 4, 30),
        help="Start of smd pull window",
    )
    parser.add_argument(
        "--smoke-end",
        type=date.fromisoformat,
        default=date(2026, 4, 30),
        help="End of smd pull window",
    )
    parser.add_argument(
        "--asof-start",
        type=date.fromisoformat,
        default=date(2024, 7, 1),
        help="First feature-frame asof",
    )
    parser.add_argument(
        "--asof-end",
        type=date.fromisoformat,
        default=date(2024, 12, 31),
        help="Last feature-frame asof",
    )
    parser.add_argument("--stride-days", type=int, default=5)
    parser.add_argument("--coverage-min", type=float, default=0.70)
    parser.add_argument("--corr-max", type=float, default=0.85)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    api_key = os.environ.get("ALPHALENS_IVOL_API_KEY") or os.environ.get("IVOL_API_KEY")
    if not api_key:
        logger.error("ALPHALENS_IVOL_API_KEY env var not set")
        return 2

    ivol.setLoginParams(apiKey=api_key)
    ivol.setDelayBetweenRequests(0.3)

    SMD_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Pull smd cache for smoke universe
    t0 = time.time()
    logger.info("Pulling smd for %d tickers → %s", len(SMOKE_UNIVERSE), SMD_CACHE_DIR)
    n_new = download_and_cache(
        SMOKE_UNIVERSE,
        args.smoke_start,
        args.smoke_end,
        SMD_CACHE_DIR,
        sleep_between=0.3,
    )
    logger.info("Pulled %d new parquets in %.1fs", n_new, time.time() - t0)

    # 2. Inventory cache
    inventory: list[dict] = []
    for t in SMOKE_UNIVERSE:
        df = load_cached_smd(t, SMD_CACHE_DIR)
        if df is None:
            inventory.append({"ticker": t, "rows": 0, "cached": False})
            continue
        inventory.append(
            {
                "ticker": t,
                "rows": len(df),
                "cached": True,
                "first_date": str(df["tradeDate"].min()) if "tradeDate" in df else None,
                "last_date": str(df["tradeDate"].max()) if "tradeDate" in df else None,
                "ivx30_nan_pct": float(df["ivx30"].isna().mean()) if "ivx30" in df else None,
            }
        )

    # 3. Build calendar + feature frame
    asofs = _build_calendar(args.asof_start, args.asof_end, args.stride_days)
    logger.info("Asofs: %d (stride=%d, %s..%s)", len(asofs), args.stride_days, asofs[0], asofs[-1])

    def _loader(ticker: str) -> pd.DataFrame | None:
        return load_cached_smd(ticker, SMD_CACHE_DIR)

    t1 = time.time()
    frame = build_feature_frame(
        smd_loader=_loader,
        universe=SMOKE_UNIVERSE,
        asof_dates=[d.isoformat() for d in asofs],
    )
    logger.info("Feature frame: %d rows in %.1fs", len(frame), time.time() - t1)

    # 4. Phase A gates
    gates = validate_phase_a_gates(
        frame,
        coverage_min=args.coverage_min,
        corr_max=args.corr_max,
    )
    logger.info(
        "Coverage: %.1f%% (≥%.0f%%) — %s",
        gates["coverage_pct"] * 100,
        args.coverage_min * 100,
        "PASS" if gates["coverage_pass"] else "FAIL",
    )
    logger.info(
        "Max pairwise |corr|: %.4f (<%.2f) — %s",
        gates["max_abs_corr"],
        args.corr_max,
        "PASS" if gates["multicollinearity_pass"] else "FAIL",
    )
    if not gates["multicollinearity_pass"] and gates["offending_pair"]:
        logger.warning("Offending pair: %s", gates["offending_pair"])
        try:
            drop = multicollinearity_drop_recommendation(offending_pair=gates["offending_pair"])
            logger.warning("Drop recommendation: %s", drop)
        except ValueError as e:
            logger.warning("No pre-committed drop recommendation: %s", e)

    overall_pass = gates["coverage_pass"] and gates["multicollinearity_pass"]
    verdict = "PASS" if overall_pass else "FAIL"

    # 5. Report
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "smoke_v1",
        "date": "2026-05-02",
        "verdict": verdict,
        "config": {
            "universe": SMOKE_UNIVERSE,
            "smoke_window": (args.smoke_start.isoformat(), args.smoke_end.isoformat()),
            "asof_window": (args.asof_start.isoformat(), args.asof_end.isoformat()),
            "stride_days": args.stride_days,
            "coverage_min": args.coverage_min,
            "corr_max": args.corr_max,
        },
        "cache_inventory": inventory,
        "frame_shape": [int(frame.shape[0]), int(frame.shape[1])],
        "gates": gates,
        "feature_summary": {
            f: {
                "non_nan": int(frame[f].notna().sum()),
                "mean": float(frame[f].mean()) if frame[f].notna().any() else None,
                "std": float(frame[f].std()) if frame[f].notna().any() else None,
            }
            for f in FEATURE_NAMES
            if f in frame.columns
        },
    }
    OUTPUT_JSON.write_text(json.dumps(payload, indent=2, default=str))

    md_lines = [
        f"# v7 Phase A smoke — {verdict}",
        "",
        "**Date:** 2026-05-02",
        f"**Universe:** {len(SMOKE_UNIVERSE)} tickers",
        f"**Asofs:** {len(asofs)} (stride {args.stride_days}, {asofs[0]} → {asofs[-1]})",
        f"**Feature frame:** {frame.shape[0]} rows × {frame.shape[1]} cols",
        "",
        "## Gates",
        "",
        f"- Coverage: **{gates['coverage_pct'] * 100:.1f}%** "
        f"(threshold ≥ {args.coverage_min * 100:.0f}%) — "
        f"{'✅' if gates['coverage_pass'] else '❌'}",
        f"- Max pairwise |corr|: **{gates['max_abs_corr']:.4f}** "
        f"(threshold < {args.corr_max:.2f}) — "
        f"{'✅' if gates['multicollinearity_pass'] else '❌'}",
        "",
        f"**Verdict: {verdict}**",
        "",
        "## Cache inventory",
        "",
        "| Ticker | Cached | Rows | First | Last | ivx30 NaN % |",
        "|---|---|---|---|---|---|",
    ]
    for inv in inventory:
        if inv["cached"]:
            md_lines.append(
                f"| {inv['ticker']} | ✅ | {inv['rows']} | "
                f"{inv.get('first_date', '—')} | {inv.get('last_date', '—')} | "
                f"{inv['ivx30_nan_pct'] * 100:.1f}% |"
                if inv.get("ivx30_nan_pct") is not None
                else f"| {inv['ticker']} | ✅ | {inv['rows']} | "
                f"{inv.get('first_date', '—')} | {inv.get('last_date', '—')} | — |"
            )
        else:
            md_lines.append(f"| {inv['ticker']} | ❌ | 0 | — | — | — |")
    md_lines.extend(["", "## Feature summary", ""])
    md_lines.append("| Feature | non-NaN | mean | std |")
    md_lines.append("|---|---|---|---|")
    for f in FEATURE_NAMES:
        if f in frame.columns:
            non_nan = int(frame[f].notna().sum())
            if non_nan > 0:
                m = frame[f].mean()
                s = frame[f].std()
                md_lines.append(f"| {f} | {non_nan} | {m:.4f} | {s:.4f} |")
            else:
                md_lines.append(f"| {f} | 0 | — | — |")
    OUTPUT_MD.write_text("\n".join(md_lines) + "\n")

    print(json.dumps(gates, indent=2, default=str))
    print(f"\nVerdict: {verdict}")
    print(f"JSON → {OUTPUT_JSON}")
    print(f"MD → {OUTPUT_MD}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
