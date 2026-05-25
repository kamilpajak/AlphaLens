"""iVolatility survivorship probe v2 — uses official Python wrapper + ticker
variant cascading + 4-tier retention hierarchy (T1+T2 strict gate per zen CR).

Background:
- v1 reported 51% retention; manual SDK probe showed it was probe methodology
  artifact. SIVB → SIVBQ (Ch11 Q-suffix on PINK), FRC → FRCB. Equity-keyed
  endpoints (ivx/ivs/hv/stock-prices) need post-delisting ticker; chain-keyed
  (option-series-on-date) keeps historical ticker.
- v2 design vetted by zen (gemini-3-pro-preview) 2026-05-01. Zen prescribed:
  STRICT T1+T2 gate (T3 chain-only is "FAIL in disguise" — 90M calls
  infeasible at scale); cascading is workaround pending Master Symbology from
  iVolatility support; TDD with mocked unit tests.

Tier hierarchy:
  T1: equity-direct (original ticker → ivx/ivs/hv hit) — production-ready
  T2: equity-via-variant (original+Q/B/N/V → ivx hit) — needs ETL mapping
  T3: chain-only (option-series-on-date refs but no equity-level data) —
      FAIL for v7 (~90M calls to reconstruct features per-contract)
  T4: completely missing — FAIL

Run:
    ALPHALENS_IVOL_API_KEY=... .venv/bin/python \\
        scripts/probe_ivolatility_options_survivorship_v2.py \\
        --n-acquisitions 70 --n-unknown 123 --random-state 42

Output:
    docs/research/ivolatility_survivorship_probe_v2_2026_05_01.json
    docs/research/ivolatility_survivorship_probe_v2_2026_05_01.md
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SURVIVORSHIP_PARQUET = Path.home() / ".alphalens" / "survivorship" / "delisted_2021_2026.parquet"
OPTIONABLE_PARQUET = (
    Path.home() / ".alphalens" / "survivorship" / "optionable_delisted_2018_2024.parquet"
)
OUTPUT_JSON = REPO_ROOT / "docs" / "research" / "ivolatility_survivorship_probe_v2_2026_05_01.json"
OUTPUT_MD = REPO_ROOT / "docs" / "research" / "ivolatility_survivorship_probe_v2_2026_05_01.md"

WINDOW_START = pd.Timestamp("2018-04-30")
WINDOW_END = pd.Timestamp("2024-04-30")

GROUND_TRUTH = {
    "SIVB": "SVB Financial halted 2023-03-10 (v1 FAIL — expected T2 via SIVBQ)",
    "FRC": "First Republic halted 2023-05-01 (v1 FAIL — expected T2 via FRCB)",
    "SBNY": "Signature Bank halted 2023-03-12 (v1 PASS — expected T1)",
    "TWTR": "Twitter taken private 2022-10-27 (v1 PASS — expected T1)",
    "ATVI": "Activision acquired 2023-10-13 (v1 PASS — expected T1)",
    "SPLK": "Splunk acquired 2024-03-18 (v1 PASS — expected T1)",
    "VMW": "VMware acquired 2023-11-22 (v1 PASS — expected T1)",
}

# Strict gates per zen CR — T1+T2 only. T3 is chain-only retention which
# requires ~90M API calls to reconstruct features at our backtest scale,
# functionally inaccessible. Treat T3 as FAIL for vendor verdict.
GATE_OVERALL_STRICT = 0.95
GATE_ACQUISITION_STRICT = 0.95
GATE_DISTRESS_STRICT = 0.85
IVS_DATE_OFFSETS = (7, 14, 21, 30)
# Bug fix v3 (2026-05-01 PM after exploration): chain endpoint also fluctuates
# by-date — same SHLM ticker oscillates 0/80 across consecutive days.
CHAIN_DATE_OFFSETS = (7, 14, 21, 30, 45, 60, 90)
# ivx/hv require LONG lookback for stale tickers (SHLM example: 30d→0, 365d→123).
EQUITY_LOOKBACK_DAYS = 365
DELAY_BETWEEN_REQUESTS = 0.3  # 3.3 req/s upper bound, polite to vendor
# v4-fix (2026-05-01 PM late): smd coverage drops pre-delisting for low-liquidity
# tickers (HGT 2018-07-15: NaN; HGT 2016-07-15: ivx30=75.95). Cascade earlier
# dates to find populated snapshot.
SMD_DATE_OFFSETS = (30, 180, 365, 730)
# v4-fix: smd returns multi-row for cross-listed tickers (CTT: London + NYSE,
# FRX: TSX + NYSE — non-US row has NaN). Filter to US exchanges only.
US_EXCHANGES = {
    "NYSE",
    "NASDAQ",
    "NASDAQ Stocks Exchange",
    "ARCA",
    "BATS",
    "AMEX",
    "OTC",
    "PINK",
    "PINX",
    "Other OTC (Pink Sheets)",
    "XNAS",
    "XNYS",
    "XASE",
    "BATO",
}


# ============================================================================
# Pure data + logic (testable; no live API calls)
# ============================================================================


@dataclass
class EndpointResult:
    records_found: int
    returned_symbols: list[str]
    error: str | None
    metadata: dict = field(default_factory=dict)
    # smd-specific: True if stock-market-data returned populated ivx30 field
    # (records_found can be 1 with NaN ivx30 for some tickers — distinct from
    # truly empty response).
    smd_populated: bool = False


@dataclass
class TickerProbeResult:
    requested_ticker: str
    delisted_date: str
    reason: str
    tier: int  # 1-5 (probe v4: added smd-primary path)
    resolved_ticker: str | None
    smd_endpoint: EndpointResult | None  # NEW: primary feature source
    equity_endpoints: dict[str, EndpointResult]  # legacy ivx/ivs/hv/stock-prices
    chain_endpoint: EndpointResult | None
    ground_truth: str | None
    ivs_offset_used: int | None
    name: str | None = None


def smart_variants_for_reason(reason: str) -> list[str]:
    """Return suffix variants to try, in priority order. Empty string = original.

    Rationale per US equity post-delisting conventions:
    - Ch11 bankruptcy → "Q" suffix (most common, e.g. SIVBQ, BBBYQ, GMQ)
    - FDIC bank resolution / receivership → "B" or "N" suffix
    - Class share / vintage → "V"
    - acquisition/M&A → original ticker retained until last trade day, no
      post-delisting suffix needed (smart short-circuit)
    """
    if reason == "acquisition":
        return [""]
    return ["", "Q", "B", "N", "V"]


class TickerVariantResolver:
    """Cascades through ticker variants until first endpoint hit.

    Constructor takes `query_fn(symbol) -> EndpointResult` for testability;
    in production `query_fn` calls iVolatility wrapper for a representative
    equity-keyed endpoint (e.g. /ivx).
    """

    def __init__(self, query_fn: Callable[[str], EndpointResult]):
        self._query_fn = query_fn

    def resolve(self, original: str, reason: str) -> tuple[str | None, EndpointResult]:
        suffixes = smart_variants_for_reason(reason)
        last_result = EndpointResult(records_found=0, returned_symbols=[], error=None)
        for suffix in suffixes:
            candidate = original + suffix
            result = self._query_fn(candidate)
            last_result = result
            if result.error == "tariff_denied":
                return None, result
            if result.records_found > 0:
                return candidate, result
        return None, last_result


def classify_tier(
    equity_results: dict[str, EndpointResult],
    chain_result: EndpointResult | None,
    resolved: str | None,
    original: str,
    smd_result: EndpointResult | None = None,
) -> int:
    """Probe v4 tier hierarchy with stock-market-data primary path.

    T1: stock-market-data populated (ivx30 not NaN) with original ticker —
        production-ready single-call feature extraction
    T2: stock-market-data populated via variant (Q-suffix) — look-ahead concern
    T3: smd not populated, but legacy ivx/ivs/hv/stock-prices populated —
        composite-call workable but slower architecture
    T4: chain-only (option-series-on-date returns refs but no equity data)
    T5: completely missing
    """
    smd_populated = smd_result is not None and smd_result.smd_populated
    any_equity_hit = any(r.records_found > 0 for r in equity_results.values())
    chain_hit = chain_result is not None and chain_result.records_found > 0

    if smd_populated:
        if resolved is None or resolved == original:
            return 1
        return 2  # variant resolved
    if any_equity_hit:
        return 3  # legacy composite-path workable
    if chain_hit:
        return 4
    return 5


def summarize(results: list[TickerProbeResult]) -> dict:
    total = len(results)
    tier_counts = {f"T{t}": sum(1 for r in results if r.tier == t) for t in (1, 2, 3, 4, 5)}

    # Probe v4: T1+T2+T3 all production-feasible (smd-primary OR legacy composite).
    # T4 chain-only requires per-contract calls (still infeasible at scale per zen).
    strict_retained = tier_counts["T1"] + tier_counts["T2"]
    reachable_retained = strict_retained + tier_counts["T3"]

    by_reason: dict[str, dict] = {}
    for reason in ("acquisition", "unknown"):
        rows = [r for r in results if r.reason == reason]
        if not rows:
            continue
        n = len(rows)
        s = sum(1 for r in rows if r.tier in (1, 2))
        rec = sum(1 for r in rows if r.tier in (1, 2, 3))
        by_reason[reason] = {
            "n": n,
            "strict_retention_pct": s / n,
            "reachable_retention_pct": rec / n,
            "tier_counts": {f"T{t}": sum(1 for r in rows if r.tier == t) for t in (1, 2, 3, 4, 5)},
        }

    ground_truth = []
    for r in results:
        if r.ground_truth:
            ground_truth.append(
                {
                    "ticker": r.requested_ticker,
                    "expectation": r.ground_truth,
                    "tier": r.tier,
                    "resolved_ticker": r.resolved_ticker,
                }
            )

    return {
        "total": total,
        "tier_counts": tier_counts,
        "strict_retention_pct": strict_retained / total if total else 0.0,
        "reachable_retention_pct": reachable_retained / total if total else 0.0,
        "by_reason": by_reason,
        "ground_truth": ground_truth,
    }


def evaluate_verdict(summary: dict) -> dict:
    """Strict gate evaluation per zen CR.

    Maps `acquisition` reason to the acquisition gate and `unknown` reason
    (mixed Ch11/distress/standard) to the distress gate.
    """
    by_reason = summary.get("by_reason", {})
    overall = summary["strict_retention_pct"]
    acq = by_reason.get("acquisition", {}).get("strict_retention_pct", 0.0)
    distress = by_reason.get("unknown", {}).get("strict_retention_pct", 0.0)

    gates = {
        "overall_strict_retention": (overall, GATE_OVERALL_STRICT, overall >= GATE_OVERALL_STRICT),
        "acquisition_strict_retention": (
            acq,
            GATE_ACQUISITION_STRICT,
            acq >= GATE_ACQUISITION_STRICT,
        ),
        "distress_strict_retention": (
            distress,
            GATE_DISTRESS_STRICT,
            distress >= GATE_DISTRESS_STRICT,
        ),
    }
    overall_pass = all(g[2] for g in gates.values())
    return {"gates": gates, "verdict": "PASS" if overall_pass else "FAIL"}


# ============================================================================
# Live probe (uses ivolatility wrapper)
# ============================================================================


def _build_query_fns(ivol_module):
    """Returns dict of endpoint name → query callable. Wrapped here so tests
    can mock without importing ivolatility."""

    def make(endpoint: str):
        try:
            fn = ivol_module.setMethod(endpoint)
        except Exception as e:
            logger.warning("setMethod failed for %s: %s", endpoint, e)
            return None
        return fn

    return {
        "stock-prices": make("/equities/eod/stock-prices"),
        "ivx": make("/equities/eod/ivx"),
        "hv": make("/equities/eod/hv"),
        "ivs": make("/equities/eod/ivs"),
        "option-series-on-date": make("/equities/eod/option-series-on-date"),
        "stock-market-data": make("/equities/stock-market-data"),  # v4 primary
    }


def _df_to_endpoint_result(df) -> EndpointResult:
    """Convert wrapper DataFrame return into EndpointResult."""
    import pandas as pd

    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return EndpointResult(records_found=0, returned_symbols=[], error=None)
    symbols: list[str] = []
    if "symbol" in df.columns:
        symbols = sorted({str(s) for s in df["symbol"].dropna().unique() if s})
    return EndpointResult(records_found=len(df), returned_symbols=symbols, error=None)


def _safe_call(fn, **kwargs) -> EndpointResult:
    """Wrap wrapper call; classify common errors."""
    if fn is None:
        return EndpointResult(records_found=0, returned_symbols=[], error="wrapper_unavailable")
    try:
        df = fn(**kwargs)
        return _df_to_endpoint_result(df)
    except Exception as e:
        msg = str(e)
        err = "tariff_denied" if "403" in msg or "Forbidden" in msg else f"http_error: {msg[:120]}"
        return EndpointResult(records_found=0, returned_symbols=[], error=err)


def _safe_smd_call(fn, **kwargs) -> EndpointResult:
    """stock-market-data wrapper: returns 1+ rows with possibly-NaN IV fields.

    v4-fix: handles 2 known issues:
    1. Multi-row for cross-listed tickers — filter to US exchanges (CTT TSX+NYSE,
       FRX TSX+NYSE both have non-US row with NaN).
    2. Picks row with populated ivx30 if any (defensive against row order).
    """
    import pandas as pd

    if fn is None:
        return EndpointResult(
            records_found=0, returned_symbols=[], error="wrapper_unavailable", smd_populated=False
        )
    try:
        df = fn(**kwargs)
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return EndpointResult(
                records_found=0, returned_symbols=[], error=None, smd_populated=False
            )
        symbols: list[str] = []
        if "symbol" in df.columns:
            symbols = sorted({str(s) for s in df["symbol"].dropna().unique() if s})

        # v4-fix: filter multi-row to US exchanges first
        candidate_df = df
        if "exchange" in df.columns and len(df) > 1:
            us_mask = df["exchange"].astype(str).isin(US_EXCHANGES)
            if us_mask.any():
                candidate_df = df[us_mask]

        # Check ivx30 populated as canonical signal — pick first populated row
        ivx_populated = False
        if "ivx30" in candidate_df.columns and len(candidate_df) > 0:
            non_null_ivx = candidate_df["ivx30"].dropna()
            ivx_populated = len(non_null_ivx) > 0

        return EndpointResult(
            records_found=len(df),
            returned_symbols=symbols,
            error=None,
            smd_populated=ivx_populated,
            metadata={"us_rows": len(candidate_df)},
        )
    except Exception as e:
        msg = str(e)
        err = "tariff_denied" if "403" in msg or "Forbidden" in msg else f"http_error: {msg[:120]}"
        return EndpointResult(records_found=0, returned_symbols=[], error=err, smd_populated=False)


def _probe_ticker_live(query_fns: dict, row: pd.Series) -> TickerProbeResult:
    ticker = row["ticker"]
    delisted_date = row["delisted_date"]
    reason = row.get("reason", "unknown") or "unknown"
    name = row.get("name")
    # Bug fix: stale tickers need long lookback. SHLM ivx 30d→0, 365d→123.
    from_date = (delisted_date - timedelta(days=EQUITY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    to_date = (delisted_date + timedelta(days=5)).strftime("%Y-%m-%d")
    short_from_date = (delisted_date - timedelta(days=30)).strftime("%Y-%m-%d")

    # PROBE V4: stock-market-data is PRIMARY signal — single call returns 100+
    # pre-computed features per ticker. Use ivx30 populated as canonical hit.
    # v4-fix: cascade through earlier dates because smd coverage drops pre-
    # delisting for low-liquidity tickers (HGT 2018-07-15 NaN, 2016-07-15 75.95).
    smd_result = EndpointResult(
        records_found=0, returned_symbols=[], error=None, smd_populated=False
    )
    resolved = None
    for offset in SMD_DATE_OFFSETS:
        target = (delisted_date - timedelta(days=offset)).strftime("%Y-%m-%d")

        # Wrap _safe_smd_call: resolver uses records_found>0 as hit; we coerce
        # records_found=0 when smd_populated=False so resolver correctly skips
        # unpopulated rows.
        def smd_query(sym: str, _t=target) -> EndpointResult:
            r = _safe_smd_call(query_fns["stock-market-data"], symbols=sym, from_=_t, to=_t)
            if not r.smd_populated and r.error != "tariff_denied":
                return EndpointResult(
                    records_found=0,
                    returned_symbols=r.returned_symbols,
                    error=r.error,
                    smd_populated=False,
                    metadata=r.metadata,
                )
            return r

        smd_resolver = TickerVariantResolver(smd_query)
        candidate_resolved, candidate_result = smd_resolver.resolve(ticker, reason)
        if candidate_resolved and candidate_result.smd_populated:
            resolved = candidate_resolved
            smd_result = candidate_result
            break
        # Keep last attempted result for diagnostic
        smd_result = candidate_result

    # Legacy fallback path: cascading via ivx as canonical (probe v3 logic)
    def ivx_query(sym: str) -> EndpointResult:
        return _safe_call(query_fns["ivx"], symbol=sym, from_=from_date, to=to_date)

    if resolved is None:
        resolver = TickerVariantResolver(ivx_query)
        resolved, ivx_result = resolver.resolve(ticker, reason)
    else:
        ivx_result = _safe_call(query_fns["ivx"], symbol=resolved, from_=from_date, to=to_date)

    # 2. Pull other equity endpoints with resolved ticker (or original if none).
    sym_for_equity = resolved or ticker
    sp_result = _safe_call(
        query_fns["stock-prices"], symbol=sym_for_equity, from_=short_from_date, to=to_date
    )
    hv_result = _safe_call(query_fns["hv"], symbol=sym_for_equity, from_=from_date, to=to_date)

    # 3. ivs cascading offsets (zen v1 fix preserved)
    ivs_result = EndpointResult(records_found=0, returned_symbols=[], error=None)
    ivs_offset_used = None
    for offset in IVS_DATE_OFFSETS:
        d = (delisted_date - timedelta(days=offset)).strftime("%Y-%m-%d")
        r = _safe_call(query_fns["ivs"], symbol=sym_for_equity, date=d)
        if r.records_found > 0:
            ivs_result = r
            ivs_offset_used = offset
            break
        if r.error == "tariff_denied":
            ivs_result = r
            break
        ivs_result = r

    # 4. Chain endpoint with cascading offsets (bug fix: SHLM oscillates 0/80
    # across consecutive days). Always uses ORIGINAL ticker — option-series-on-date
    # preserves historical ticker convention.
    chain_result = EndpointResult(records_found=0, returned_symbols=[], error=None)
    for offset in CHAIN_DATE_OFFSETS:
        d = (delisted_date - timedelta(days=offset)).strftime("%Y-%m-%d")
        r = _safe_call(query_fns["option-series-on-date"], symbol=ticker, date=d)
        if r.records_found > 0:
            chain_result = r
            break
        if r.error == "tariff_denied":
            chain_result = r
            break
        chain_result = r

    equity_endpoints = {
        "stock-prices": sp_result,
        "ivx": ivx_result,
        "ivs": ivs_result,
        "hv": hv_result,
    }
    tier = classify_tier(equity_endpoints, chain_result, resolved, ticker, smd_result=smd_result)

    return TickerProbeResult(
        requested_ticker=ticker,
        delisted_date=delisted_date.strftime("%Y-%m-%d"),
        reason=reason,
        tier=tier,
        resolved_ticker=resolved,
        smd_endpoint=smd_result,
        equity_endpoints=equity_endpoints,
        chain_endpoint=chain_result,
        ground_truth=GROUND_TRUTH.get(ticker),
        ivs_offset_used=ivs_offset_used,
        name=name,
    )


def _sample_stratified(
    n_acquisitions: int, n_unknown: int, random_state: int, optionable_only: bool = False
) -> pd.DataFrame:
    """Stratified random sample from delisted parquet.

    optionable_only=True (zen CR prescription): filters to tickers verified
    as having had options chains in Polygon's reference data at delisted_date - 30d.
    Eliminates SPAC/no-options small-cap contamination that depressed v2 acquisition
    retention to 1.4% (it was 0/60 SPACs that legitimately had no options).
    """
    if optionable_only:
        if not OPTIONABLE_PARQUET.exists():
            raise FileNotFoundError(
                f"Optionable parquet not found: {OPTIONABLE_PARQUET}. "
                "Run scripts/build_optionable_universe.py first."
            )
        df = pd.read_parquet(OPTIONABLE_PARQUET)
        # `== True` filters out pd.NA rows (forced-acquisition placeholders below
        # write NA into this column); bare truth-test on `optionable` would raise
        # on NA boolean indexing. noqa matches the convention used in sibling
        # scripts (pull_v7_smd_universe.py, check_v7_pull_status.py).
        df = df[df["optionable"] == True].copy()  # noqa: E712
        logger.info("Optionable pool size: %d", len(df))
    else:
        df = pd.read_parquet(SURVIVORSHIP_PARQUET)

    mask = (df["delisted_date"] >= WINDOW_START) & (df["delisted_date"] <= WINDOW_END)
    pool = df.loc[mask].copy()

    samples = []
    acquisitions = pool[pool["reason"] == "acquisition"]
    if len(acquisitions) < n_acquisitions:
        logger.warning(
            "Acquisition pool only has %d (asked %d) — sampling all available",
            len(acquisitions),
            n_acquisitions,
        )
        samples.append(acquisitions)
    else:
        samples.append(acquisitions.sample(n=n_acquisitions, random_state=random_state))

    unknown = pool[pool["reason"] == "unknown"]
    unknown = unknown[~unknown["ticker"].isin(GROUND_TRUTH)]
    if len(unknown) < n_unknown:
        logger.warning(
            "Unknown pool only has %d (asked %d) — sampling all available", len(unknown), n_unknown
        )
        samples.append(unknown)
    else:
        samples.append(unknown.sample(n=n_unknown, random_state=random_state))

    # Force-include ground-truth tickers — pull from FULL delisted parquet
    # (some ground-truth like SIVB may not appear in optionable parquet if Polygon
    # missed the chain ref despite vendor having data). Diagnostic value preserved.
    full = pd.read_parquet(SURVIVORSHIP_PARQUET)
    full_mask = (full["delisted_date"] >= WINDOW_START) & (full["delisted_date"] <= WINDOW_END)
    forced = full.loc[full_mask & full["ticker"].isin(GROUND_TRUTH)].copy()
    if "n_contracts" not in forced.columns:
        forced["n_contracts"] = pd.NA
        forced["optionable"] = pd.NA
        forced["polygon_error"] = None
    samples.append(forced)

    return (
        pd.concat(samples, ignore_index=True)
        .drop_duplicates(subset=["ticker"])
        .sort_values("delisted_date")
        .reset_index(drop=True)
    )


def _result_to_dict(r: TickerProbeResult) -> dict:
    def _ep(e: EndpointResult | None) -> dict | None:
        if e is None:
            return None
        out = {
            "records_found": e.records_found,
            "returned_symbols": e.returned_symbols,
            "error": e.error,
        }
        if e.smd_populated:
            out["smd_populated"] = True
        return out

    return {
        "requested_ticker": r.requested_ticker,
        "delisted_date": r.delisted_date,
        "name": r.name,
        "reason": r.reason,
        "tier": r.tier,
        "resolved_ticker": r.resolved_ticker,
        "smd_endpoint": _ep(r.smd_endpoint),
        "equity_endpoints": {ep: _ep(res) for ep, res in r.equity_endpoints.items()},
        "chain_endpoint": _ep(r.chain_endpoint),
        "ground_truth": r.ground_truth,
        "ivs_offset_used": r.ivs_offset_used,
    }


def _write_audit(results: list[TickerProbeResult], summary: dict, verdict: dict, args) -> None:
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "config": {
            "version": "v2",
            "n_acquisitions": args.n_acquisitions,
            "n_unknown": args.n_unknown,
            "random_state": args.random_state,
            "ground_truth_forced": list(GROUND_TRUTH.keys()),
            "endpoints": ["stock-prices", "ivx", "ivs", "hv", "option-series-on-date"],
            "ivs_date_offsets": list(IVS_DATE_OFFSETS),
            "gates": {
                "overall_strict": GATE_OVERALL_STRICT,
                "acquisition_strict": GATE_ACQUISITION_STRICT,
                "distress_strict": GATE_DISTRESS_STRICT,
                "design_note": "T3 chain-only NOT counted in strict gate per zen CR (90M calls infeasible at scale)",
            },
        },
        "summary": summary,
        "verdict": verdict,
        "per_ticker": [_result_to_dict(r) for r in results],
    }
    OUTPUT_JSON.write_text(json.dumps(payload, indent=2, default=str))


def _write_markdown(summary: dict, verdict: dict) -> None:
    lines = [
        f"# iVolatility survivorship probe v2 verdict — {verdict['verdict']}",
        "",
        "**Date:** 2026-05-01",
        f"**Sample:** n={summary['total']}",
        f"**Strict retention (T1+T2):** {summary['strict_retention_pct']:.1%}  *production-ready or variant-resolvable*",
        f"**Reachable retention (T1+T2+T3):** {summary['reachable_retention_pct']:.1%}  *includes chain-only — not feature-extractable at scale*",
        "",
        "## Tier hierarchy",
        "",
        f"- **T1** (equity-direct): {summary['tier_counts']['T1']}  *original ticker → ivx hit, production-ready*",
        f"- **T2** (equity-via-variant): {summary['tier_counts']['T2']}  *needs ETL ticker mapping, look-ahead bias risk*",
        f"- **T3** (chain-only): {summary['tier_counts']['T3']}  *FAIL for v7 — 90M calls infeasible*",
        f"- **T4** (missing): {summary['tier_counts']['T4']}  *FAIL*",
        "",
        "## Gates (strict T1+T2 per zen CR)",
        "",
        "| Gate | Observed | Threshold | Pass |",
        "|------|----------|-----------|------|",
    ]
    for name, (obs, thr, pas) in verdict["gates"].items():
        lines.append(f"| {name} | {obs:.1%} | ≥ {thr:.1%} | {'✅' if pas else '❌'} |")

    lines += ["", "## By delisting reason", ""]
    lines += ["| Reason | n | strict (T1+T2) | reachable (+T3) | T1 | T2 | T3 | T4 |"]
    lines += ["|--------|---|----------------|-----------------|----|----|----|----|"]
    for reason, st in summary["by_reason"].items():
        tc = st["tier_counts"]
        lines.append(
            f"| {reason} | {st['n']} | {st['strict_retention_pct']:.1%} | {st['reachable_retention_pct']:.1%}"
            f" | {tc['T1']} | {tc['T2']} | {tc['T3']} | {tc['T4']} |"
        )

    lines += ["", "## Ground-truth diagnostic", ""]
    lines += ["| Ticker | Expectation | Tier | Resolved as |"]
    lines += ["|--------|-------------|------|-------------|"]
    for g in summary["ground_truth"]:
        lines.append(
            f"| {g['ticker']} | {g['expectation']} | T{g['tier']} | {g['resolved_ticker'] or '—'} |"
        )

    lines += ["", "Audit JSON: `docs/research/ivolatility_survivorship_probe_v2_2026_05_01.json`"]
    OUTPUT_MD.write_text("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-acquisitions", type=int, default=70)
    parser.add_argument("--n-unknown", type=int, default=123)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=DELAY_BETWEEN_REQUESTS)
    parser.add_argument(
        "--optionable-only",
        action="store_true",
        help="Sample from Polygon-verified optionable pool (zen CR prescription)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    api_key = os.environ.get("ALPHALENS_IVOL_API_KEY")
    if not api_key:
        logger.error("ALPHALENS_IVOL_API_KEY env var required")
        return 2

    import ivolatility as ivol  # local import — keep tests pure

    ivol.setLoginParams(apiKey=api_key)
    ivol.setDelayBetweenRequests(args.delay)

    query_fns = _build_query_fns(ivol)

    sample_df = _sample_stratified(
        args.n_acquisitions, args.n_unknown, args.random_state, optionable_only=args.optionable_only
    )
    if args.limit:
        sample_df = sample_df.head(args.limit)
    logger.info(
        "Sample n=%d (acquisitions=%d, unknown=%d, ground-truth=%d)",
        len(sample_df),
        int((sample_df["reason"] == "acquisition").sum()),
        int((sample_df["reason"] == "unknown").sum()),
        int(sample_df["ticker"].isin(GROUND_TRUTH).sum()),
    )

    results: list[TickerProbeResult] = []
    start = time.time()
    for i, (_, row) in enumerate(sample_df.iterrows(), 1):
        try:
            results.append(_probe_ticker_live(query_fns, row))
        except Exception as e:
            logger.warning("probe failure for %s: %s", row["ticker"], e)
            results.append(
                TickerProbeResult(
                    requested_ticker=row["ticker"],
                    delisted_date=row["delisted_date"].strftime("%Y-%m-%d"),
                    reason=row.get("reason", "unknown") or "unknown",
                    tier=4,
                    resolved_ticker=None,
                    equity_endpoints={},
                    chain_endpoint=None,
                    ground_truth=GROUND_TRUTH.get(row["ticker"]),
                    ivs_offset_used=None,
                    name=row.get("name"),
                )
            )
        if i % 20 == 0:
            logger.info("progress: %d/%d (elapsed=%.1fs)", i, len(sample_df), time.time() - start)

    logger.info("All %d probes done in %.1fs", len(results), time.time() - start)

    summary = summarize(results)
    verdict = evaluate_verdict(summary)
    _write_audit(results, summary, verdict, args)
    _write_markdown(summary, verdict)

    print(f"\n=== Verdict: {verdict['verdict']} ===")
    print(f"Strict retention (T1+T2): {summary['strict_retention_pct']:.1%}")
    print(f"Reachable retention (T1+T2+T3): {summary['reachable_retention_pct']:.1%}")
    print(f"Tier counts: {summary['tier_counts']}")
    for name, (obs, thr, pas) in verdict["gates"].items():
        print(f"  {name}: {obs:.1%} ≥ {thr:.1%}  {'PASS' if pas else 'FAIL'}")
    print(f"\nAudit: {OUTPUT_JSON}")
    print(f"Verdict: {OUTPUT_MD}")
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
