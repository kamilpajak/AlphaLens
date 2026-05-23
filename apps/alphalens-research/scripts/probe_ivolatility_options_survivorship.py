"""iVolatility.com REST API survivorship probe (n=200 stratified).

Adversarial-review follow-up to manual n=7 probe. Both gemini-3-pro-preview
and Sonar-Reasoning-Pro demanded a randomized stratified probe (>=150-200)
before any vendor verdict; n=7 cherry-picked sample is statistically
meaningless and biased toward famous events that vendors are more likely
to manually patch.

Tests 3 EOD endpoints (stock-prices, ivx, ivs) on stratified delisted
tickers from 2018-04-30 to 2024-04-30. Reports retention rate by reason
category, overall, plus symbol-mismatch rate (BBBY/OSTK ticker-reuse
anomaly check), plus per-ticker ground-truth diagnostic.

Auth: reads ALPHALENS_IVOL_API_KEY from environment. NEVER log or echo
the key.

Run:
    ALPHALENS_IVOL_API_KEY=... .venv/bin/python \\
        scripts/probe_ivolatility_options_survivorship.py \\
        --n-acquisitions 70 --n-unknown 123 --random-state 42

Output:
    docs/research/ivolatility_survivorship_probe_2026_05_01.json
    docs/research/ivolatility_survivorship_probe_2026_05_01.md
    stdout: gate verdict + category breakdown
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SURVIVORSHIP_PARQUET = Path.home() / ".alphalens" / "survivorship" / "delisted_2021_2026.parquet"
OUTPUT_JSON = REPO_ROOT / "docs" / "research" / "ivolatility_survivorship_probe_2026_05_01.json"
OUTPUT_MD = REPO_ROOT / "docs" / "research" / "ivolatility_survivorship_probe_2026_05_01.md"

WINDOW_START = pd.Timestamp("2018-04-30")
WINDOW_END = pd.Timestamp("2024-04-30")

GROUND_TRUTH = {
    "SIVB": "SVB Financial halted 2023-03-10 (manual probe FAIL)",
    "FRC": "First Republic halted 2023-05-01 (manual probe FAIL)",
    "SBNY": "Signature Bank halted 2023-03-12 (manual probe PASS)",
    "TWTR": "Twitter taken private 2022-10-27 (manual probe PASS)",
    "ATVI": "Activision acquired 2023-10-13 (manual probe PASS)",
    "SPLK": "Splunk acquired 2024-03-18 (manual probe PASS)",
    "VMW": "VMware acquired 2023-11-22 (manual probe PASS)",
}

BASE_URL = "https://restapi.ivolatility.com"
ENDPOINTS = ("stock-prices", "ivx", "ivs")
GATE_OVERALL_MIN = 0.95
GATE_ACQUISITION_MIN = 0.95
GATE_UNKNOWN_MIN = 0.85
GATE_SYMBOL_MISMATCH_MAX = 0.0
HTTP_TIMEOUT = 20
HTTP_MAX_RETRIES = 7
HTTP_BACKOFF_BASE = 2.0
IVS_DATE_OFFSETS = (7, 14, 21, 30)


def _sample_stratified(
    n_acquisitions: int,
    n_unknown: int,
    random_state: int,
) -> pd.DataFrame:
    df = pd.read_parquet(SURVIVORSHIP_PARQUET)
    mask = (df["delisted_date"] >= WINDOW_START) & (df["delisted_date"] <= WINDOW_END)
    pool = df.loc[mask].copy()

    np.random.default_rng(random_state)
    samples = []

    acquisitions = pool[pool["reason"] == "acquisition"]
    if len(acquisitions) < n_acquisitions:
        raise ValueError(f"Only {len(acquisitions)} acquisitions in window, need {n_acquisitions}")
    samples.append(acquisitions.sample(n=n_acquisitions, random_state=random_state))

    unknown = pool[pool["reason"] == "unknown"]
    unknown = unknown[~unknown["ticker"].isin(GROUND_TRUTH)]
    if len(unknown) < n_unknown:
        raise ValueError(f"Only {len(unknown)} unknown in window, need {n_unknown}")
    samples.append(unknown.sample(n=n_unknown, random_state=random_state))

    forced = pool[pool["ticker"].isin(GROUND_TRUTH)].copy()
    samples.append(forced)

    out = pd.concat(samples, ignore_index=True).drop_duplicates(subset=["ticker"])
    return out.sort_values("delisted_date").reset_index(drop=True)


def _query_endpoint(
    api_key: str,
    endpoint: str,
    ticker: str,
    delisted_date: pd.Timestamp,
    ivs_offset_days: int = 7,
) -> dict:
    """Query single endpoint with retries; returns dict with status + summary.

    Bug-fix-2 (zen CR 2026-05-01):
    - HTTP_MAX_RETRIES=7, HTTP_BACKOFF_BASE=2.0, jitter via uniform(0,1)
    - Rate-limit-exhausted returns http_status=-1 error="rate_limit_exhausted";
      _summarize excludes these from retention denominator (indeterminate, NOT
      a vendor data hole).
    - ivs supports cascading via ivs_offset_days; _probe_ticker iterates
      IVS_DATE_OFFSETS and takes first hit (avoids weekend/halt-date misses).
    """
    if endpoint == "ivs":
        params = {
            "apiKey": api_key,
            "symbol": ticker,
            "date": (delisted_date - timedelta(days=ivs_offset_days)).strftime("%Y-%m-%d"),
        }
    else:
        params = {
            "apiKey": api_key,
            "symbol": ticker,
            "from": (delisted_date - timedelta(days=30)).strftime("%Y-%m-%d"),
            "to": (delisted_date + timedelta(days=5)).strftime("%Y-%m-%d"),
        }

    url = f"{BASE_URL}/equities/eod/{endpoint}"
    for attempt in range(HTTP_MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            try:
                body = resp.json()
            except ValueError:
                body = None

            if resp.status_code == 200 and isinstance(body, dict):
                records = body.get("data", []) or []
                symbols = sorted({r.get("symbol", "") for r in records if r.get("symbol")})
                return {
                    "http_status": 200,
                    "records_found": len(records),
                    "symbols_returned": symbols,
                    "symbol_mismatch": bool(symbols and ticker not in symbols),
                    "error": None,
                }

            if resp.status_code == 403:
                return {
                    "http_status": 403,
                    "records_found": 0,
                    "symbols_returned": [],
                    "symbol_mismatch": False,
                    "error": "tariff_denied",
                }

            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep((HTTP_BACKOFF_BASE**attempt) + float(np.random.uniform(0, 1)))
                continue

            return {
                "http_status": resp.status_code,
                "records_found": 0,
                "symbols_returned": [],
                "symbol_mismatch": False,
                "error": (body or {}).get("message")
                if isinstance(body, dict)
                else f"HTTP {resp.status_code}",
            }
        except requests.RequestException:
            time.sleep((HTTP_BACKOFF_BASE**attempt) + float(np.random.uniform(0, 1)))

    return {
        "http_status": -1,
        "records_found": 0,
        "symbols_returned": [],
        "symbol_mismatch": False,
        "error": "rate_limit_exhausted",
    }


def _probe_ticker(api_key: str, row: pd.Series) -> dict:
    ticker = row["ticker"]
    delisted_date = row["delisted_date"]
    result = {
        "ticker": ticker,
        "delisted_date": delisted_date.strftime("%Y-%m-%d"),
        "name": row.get("name"),
        "reason": row.get("reason"),
        "ground_truth": GROUND_TRUTH.get(ticker),
        "endpoints": {},
    }
    for ep in ("stock-prices", "ivx"):
        result["endpoints"][ep] = _query_endpoint(api_key, ep, ticker, delisted_date)

    # ivs: cascade through offsets; take first hit. Bug fix: avoids
    # weekend/holiday/post-halt-date single-shot misses.
    ivs_result = None
    ivs_attempts = []
    for offset in IVS_DATE_OFFSETS:
        r = _query_endpoint(api_key, "ivs", ticker, delisted_date, ivs_offset_days=offset)
        ivs_attempts.append(
            {
                "offset_days": offset,
                "records_found": r["records_found"],
                "http_status": r["http_status"],
                "error": r["error"],
            }
        )
        if r["records_found"] > 0:
            ivs_result = r
            ivs_result["ivs_offset_used"] = offset
            break
        if r["error"] == "tariff_denied":
            ivs_result = r
            break
    if ivs_result is None:
        ivs_result = r
        ivs_result["ivs_offset_used"] = None
    result["endpoints"]["ivs"] = ivs_result
    result["ivs_attempts"] = ivs_attempts

    any_data = any(e["records_found"] > 0 for e in result["endpoints"].values())
    any_mismatch = any(e["symbol_mismatch"] for e in result["endpoints"].values())
    # Indeterminate: ALL endpoints rate-limit-exhausted (cannot conclude vendor gap).
    all_indeterminate = all(
        e.get("error") == "rate_limit_exhausted" for e in result["endpoints"].values()
    )
    result["any_endpoint_returned_data"] = any_data
    result["any_symbol_mismatch"] = any_mismatch
    result["all_indeterminate"] = all_indeterminate
    return result


def _summarize(results: list[dict]) -> dict:
    total = len(results)
    indeterminate = sum(1 for r in results if r.get("all_indeterminate"))
    decidable = total - indeterminate
    retained_overall = sum(1 for r in results if r["any_endpoint_returned_data"])
    # Bug fix: retention denominator excludes indeterminate (rate-limit-exhausted)
    retained_pct = retained_overall / decidable if decidable else 0.0

    symbol_mismatch_in_retained = [
        r for r in results if r["any_endpoint_returned_data"] and r["any_symbol_mismatch"]
    ]
    symbol_mismatch_pct = (
        len(symbol_mismatch_in_retained) / retained_overall if retained_overall else 0.0
    )

    by_reason: dict[str, dict] = {}
    for reason in ("acquisition", "unknown"):
        rows = [r for r in results if r["reason"] == reason]
        if not rows:
            continue
        rows_decidable = [r for r in rows if not r.get("all_indeterminate")]
        retained = sum(1 for r in rows_decidable if r["any_endpoint_returned_data"])
        by_reason[reason] = {
            "n": len(rows),
            "n_decidable": len(rows_decidable),
            "retained": retained,
            "retention_pct": retained / len(rows_decidable) if rows_decidable else 0.0,
        }

    by_endpoint: dict[str, dict] = {}
    for ep in ENDPOINTS:
        retained = sum(1 for r in results if r["endpoints"][ep]["records_found"] > 0)
        denied = sum(1 for r in results if r["endpoints"][ep]["error"] == "tariff_denied")
        rate_lim = sum(1 for r in results if r["endpoints"][ep]["error"] == "rate_limit_exhausted")
        by_endpoint[ep] = {
            "retained": retained,
            "retention_pct": retained / total,
            "tariff_denied": denied,
            "rate_limit_exhausted": rate_lim,
        }

    ground_truth_results = []
    for r in results:
        if r["ground_truth"]:
            ground_truth_results.append(
                {
                    "ticker": r["ticker"],
                    "expectation": r["ground_truth"],
                    "any_data": r["any_endpoint_returned_data"],
                    "endpoints": {ep: e["records_found"] for ep, e in r["endpoints"].items()},
                }
            )

    return {
        "total": total,
        "indeterminate": indeterminate,
        "decidable": decidable,
        "retained_overall": retained_overall,
        "retention_pct": retained_pct,
        "symbol_mismatch_pct_in_retained": symbol_mismatch_pct,
        "by_reason": by_reason,
        "by_endpoint": by_endpoint,
        "ground_truth": ground_truth_results,
    }


def _verdict(summary: dict) -> dict:
    gates = {
        "overall_retention": (
            summary["retention_pct"],
            GATE_OVERALL_MIN,
            summary["retention_pct"] >= GATE_OVERALL_MIN,
        ),
        "acquisition_retention": (
            summary["by_reason"].get("acquisition", {}).get("retention_pct", 0.0),
            GATE_ACQUISITION_MIN,
            summary["by_reason"].get("acquisition", {}).get("retention_pct", 0.0)
            >= GATE_ACQUISITION_MIN,
        ),
        "unknown_retention": (
            summary["by_reason"].get("unknown", {}).get("retention_pct", 0.0),
            GATE_UNKNOWN_MIN,
            summary["by_reason"].get("unknown", {}).get("retention_pct", 0.0) >= GATE_UNKNOWN_MIN,
        ),
        "symbol_integrity": (
            summary["symbol_mismatch_pct_in_retained"],
            GATE_SYMBOL_MISMATCH_MAX,
            summary["symbol_mismatch_pct_in_retained"] <= GATE_SYMBOL_MISMATCH_MAX,
        ),
    }
    overall_pass = all(g[2] for g in gates.values())
    return {"gates": gates, "verdict": "PASS" if overall_pass else "FAIL"}


def _write_audit(
    sample_df: pd.DataFrame,
    results: list[dict],
    summary: dict,
    verdict: dict,
    args: argparse.Namespace,
) -> None:
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "config": {
            "n_acquisitions": args.n_acquisitions,
            "n_unknown": args.n_unknown,
            "random_state": args.random_state,
            "ground_truth_forced": list(GROUND_TRUTH.keys()),
            "window_start": WINDOW_START.strftime("%Y-%m-%d"),
            "window_end": WINDOW_END.strftime("%Y-%m-%d"),
            "endpoints": list(ENDPOINTS),
            "gates": {
                "overall_min": GATE_OVERALL_MIN,
                "acquisition_min": GATE_ACQUISITION_MIN,
                "unknown_min": GATE_UNKNOWN_MIN,
                "symbol_mismatch_max": GATE_SYMBOL_MISMATCH_MAX,
            },
        },
        "summary": summary,
        "verdict": verdict,
        "per_ticker": results,
        "sample_size": len(sample_df),
    }
    OUTPUT_JSON.write_text(json.dumps(payload, indent=2, default=str))


def _write_markdown(summary: dict, verdict: dict) -> None:
    lines = []
    lines.append(f"# iVolatility survivorship probe verdict — {verdict['verdict']}")
    lines.append("")
    lines.append("**Date:** 2026-05-01")
    lines.append(
        f"**Sample:** n={summary['total']} (decidable={summary['decidable']}, indeterminate={summary['indeterminate']})"
    )
    lines.append(
        f"**Overall retention:** {summary['retention_pct']:.1%} (denominator excludes rate-limit-exhausted indeterminate)"
    )
    lines.append(
        f"**Symbol-mismatch in retained:** {summary['symbol_mismatch_pct_in_retained']:.1%}"
    )
    lines.append("")
    lines.append("## Gates")
    lines.append("")
    lines.append("| Gate | Observed | Threshold | Pass |")
    lines.append("|------|----------|-----------|------|")
    for name, (obs, thr, pas) in verdict["gates"].items():
        comp = "≤" if "mismatch" in name else "≥"
        lines.append(f"| {name} | {obs:.1%} | {comp} {thr:.1%} | {'✅' if pas else '❌'} |")
    lines.append("")
    lines.append("## By delisting reason")
    lines.append("")
    lines.append("| Reason | n | retained | retention % |")
    lines.append("|--------|---|----------|-------------|")
    for reason, stats in summary["by_reason"].items():
        lines.append(
            f"| {reason} | {stats['n']} | {stats['retained']} | {stats['retention_pct']:.1%} |"
        )
    lines.append("")
    lines.append("## By endpoint")
    lines.append("")
    lines.append("| Endpoint | retained | retention % | tariff_denied |")
    lines.append("|----------|----------|-------------|---------------|")
    for ep, stats in summary["by_endpoint"].items():
        lines.append(
            f"| {ep} | {stats['retained']} | {stats['retention_pct']:.1%} | {stats['tariff_denied']} |"
        )
    lines.append("")
    lines.append("## Ground-truth diagnostic (manual probe corroboration)")
    lines.append("")
    lines.append("| Ticker | Expectation | Any data | stock-prices | ivx | ivs |")
    lines.append("|--------|-------------|----------|--------------|-----|-----|")
    for g in summary["ground_truth"]:
        eps = g["endpoints"]
        flag = "✅" if g["any_data"] else "❌"
        lines.append(
            f"| {g['ticker']} | {g['expectation']} | {flag} | {eps.get('stock-prices', 0)} | {eps.get('ivx', 0)} | {eps.get('ivs', 0)} |"
        )
    lines.append("")
    lines.append("Audit JSON: `docs/research/ivolatility_survivorship_probe_2026_05_01.json`")
    OUTPUT_MD.write_text("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-acquisitions", type=int, default=70)
    parser.add_argument("--n-unknown", type=int, default=123)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap total tickers (for smoke testing)"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    api_key = os.environ.get("ALPHALENS_IVOL_API_KEY")
    if not api_key:
        logger.error("ALPHALENS_IVOL_API_KEY env var required")
        return 2

    logger.info("Sampling stratified delisted tickers from %s", SURVIVORSHIP_PARQUET)
    sample_df = _sample_stratified(args.n_acquisitions, args.n_unknown, args.random_state)
    if args.limit:
        sample_df = sample_df.head(args.limit)
    logger.info(
        "Sample size: %d (acquisitions=%d, unknown=%d, ground-truth=%d)",
        len(sample_df),
        int((sample_df["reason"] == "acquisition").sum()),
        int((sample_df["reason"] == "unknown").sum()),
        int(sample_df["ticker"].isin(GROUND_TRUTH).sum()),
    )

    results: list[dict] = []
    start = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(_probe_ticker, api_key, row): row["ticker"]
            for _, row in sample_df.iterrows()
        }
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                results.append(fut.result())
            except Exception as e:
                logger.warning("probe failure for %s: %s", futures[fut], e)
                results.append(
                    {
                        "ticker": futures[fut],
                        "delisted_date": None,
                        "name": None,
                        "reason": None,
                        "ground_truth": GROUND_TRUTH.get(futures[fut]),
                        "endpoints": {},
                        "any_endpoint_returned_data": False,
                        "any_symbol_mismatch": False,
                        "error": str(e),
                    }
                )
            if i % 20 == 0:
                elapsed = time.time() - start
                logger.info("progress: %d/%d (elapsed=%.1fs)", i, len(sample_df), elapsed)

    logger.info("All %d probes complete in %.1fs", len(results), time.time() - start)

    summary = _summarize(results)
    verdict = _verdict(summary)

    _write_audit(sample_df, results, summary, verdict, args)
    _write_markdown(summary, verdict)

    print(f"\n=== Verdict: {verdict['verdict']} ===")
    print(f"Overall retention: {summary['retention_pct']:.1%} (n={summary['total']})")
    print(f"Symbol-mismatch in retained: {summary['symbol_mismatch_pct_in_retained']:.1%}")
    print("Gates:")
    for name, (obs, thr, pas) in verdict["gates"].items():
        print(
            f"  {name}: {obs:.1%} {'≤' if 'mismatch' in name else '≥'} {thr:.1%}  {'PASS' if pas else 'FAIL'}"
        )
    print(f"\nAudit:    {OUTPUT_JSON}")
    print(f"Verdict:  {OUTPUT_MD}")
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
