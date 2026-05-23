"""Unit tests for probe v2 logic — strict TDD per zen CR (mocked, no live API).

Tests cover:
- TickerVariantResolver cascading logic with smart hints from delisting reason
- Tier classification (T1=direct, T2=variant, T3=chain-only, T4=missing)
- Summary aggregation (tier counts, by-reason breakdown)
- Verdict evaluation (strict T1+T2 gate per zen — T3 reported separately, NOT
  aggregated into pass criterion because chain-only requires ~90M calls
  infeasible at scale).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from probe_ivolatility_options_survivorship_v2 import (  # noqa: E402
    EndpointResult,
    TickerProbeResult,
    TickerVariantResolver,
    classify_tier,
    evaluate_verdict,
    smart_variants_for_reason,
    summarize,
)


def _ok(records: int, symbol: str = "AAPL") -> EndpointResult:
    return EndpointResult(records_found=records, returned_symbols=[symbol], error=None)


def _empty() -> EndpointResult:
    return EndpointResult(records_found=0, returned_symbols=[], error=None)


def _denied() -> EndpointResult:
    return EndpointResult(records_found=0, returned_symbols=[], error="tariff_denied")


def _smd_pop(symbol: str = "AAPL") -> EndpointResult:
    """stock-market-data with populated ivx30 (v4 T1 signal)."""
    return EndpointResult(
        records_found=1, returned_symbols=[symbol], error=None, smd_populated=True
    )


def _smd_empty_shell(symbol: str = "AAPL") -> EndpointResult:
    """stock-market-data returned 1 row but ivx30 is NaN (not a real hit)."""
    return EndpointResult(
        records_found=1, returned_symbols=[symbol], error=None, smd_populated=False
    )


class TestSmartVariants(unittest.TestCase):
    def test_acquisition_no_variants(self):
        self.assertEqual(smart_variants_for_reason("acquisition"), [""])

    def test_unknown_tries_q_first(self):
        variants = smart_variants_for_reason("unknown")
        self.assertEqual(variants[0], "")
        self.assertIn("Q", variants)
        self.assertIn("B", variants)
        # Q comes before B (Ch11 more common than bank resolution)
        self.assertLess(variants.index("Q"), variants.index("B"))

    def test_includes_uppercase_only(self):
        variants = smart_variants_for_reason("unknown")
        for v in variants:
            self.assertEqual(v, v.upper())


class TestTickerVariantResolver(unittest.TestCase):
    def test_original_hit_returns_tier_1(self):
        calls = {"AAPL": _ok(20, "AAPL")}
        resolver = TickerVariantResolver(query_fn=lambda sym: calls.get(sym, _empty()))
        resolved, result = resolver.resolve("AAPL", reason="acquisition")
        self.assertEqual(resolved, "AAPL")
        self.assertEqual(result.records_found, 20)

    def test_variant_hit_returns_resolved_ticker(self):
        calls = {"SIVB": _empty(), "SIVBQ": _ok(16, "SIVBQ")}
        resolver = TickerVariantResolver(query_fn=lambda sym: calls.get(sym, _empty()))
        resolved, result = resolver.resolve("SIVB", reason="unknown")
        self.assertEqual(resolved, "SIVBQ")
        self.assertEqual(result.records_found, 16)

    def test_all_variants_miss_returns_none(self):
        resolver = TickerVariantResolver(query_fn=lambda sym: _empty())
        resolved, result = resolver.resolve("UNKN", reason="unknown")
        self.assertIsNone(resolved)
        self.assertEqual(result.records_found, 0)

    def test_acquisition_skips_variants(self):
        called = []

        def fn(sym):
            called.append(sym)
            return _empty()

        resolver = TickerVariantResolver(query_fn=fn)
        resolver.resolve("FOO", reason="acquisition")
        # acquisition should only try original — no variant cascading
        self.assertEqual(called, ["FOO"])

    def test_unknown_tries_q_then_b(self):
        calls = {"FRC": _empty(), "FRCQ": _empty(), "FRCB": _ok(15, "FRCB")}
        called = []

        def fn(sym):
            called.append(sym)
            return calls.get(sym, _empty())

        resolver = TickerVariantResolver(query_fn=fn)
        resolved, _ = resolver.resolve("FRC", reason="unknown")
        self.assertEqual(resolved, "FRCB")
        # Q tried before B
        self.assertEqual(called[:3], ["FRC", "FRCQ", "FRCB"])

    def test_tariff_denied_does_not_count_as_miss(self):
        # If endpoint is tariff-denied, can't conclude the variant is wrong.
        # Resolver should propagate the denial as a special signal.
        resolver = TickerVariantResolver(query_fn=lambda sym: _denied())
        _resolved, result = resolver.resolve("AAPL", reason="acquisition")
        self.assertEqual(result.error, "tariff_denied")


class TestClassifyTier(unittest.TestCase):
    """v4 tier semantics: T1=smd populated original, T2=smd via variant,
    T3=legacy ivx/ivs/hv hit (no smd), T4=chain-only, T5=missing.
    """

    def test_legacy_t3_when_smd_unavailable_but_equity_hit(self):
        # Without smd_result, equity hit alone → T3 (legacy composite path)
        equity = {"ivx": _ok(20)}
        chain = _empty()
        tier = classify_tier(
            equity_results=equity, chain_result=chain, resolved="AAPL", original="AAPL"
        )
        self.assertEqual(tier, 3)

    def test_legacy_t3_via_variant_no_smd(self):
        equity = {"ivx": _ok(16, "SIVBQ")}
        chain = _ok(738, "SIVB")
        tier = classify_tier(
            equity_results=equity, chain_result=chain, resolved="SIVBQ", original="SIVB"
        )
        self.assertEqual(tier, 3)

    def test_t4_chain_only(self):
        # No equity-keyed data, but chain refs exist
        equity = {"ivx": _empty(), "ivs": _empty(), "stock-prices": _empty()}
        chain = _ok(738, "FOO")
        tier = classify_tier(
            equity_results=equity, chain_result=chain, resolved=None, original="FOO"
        )
        self.assertEqual(tier, 4)

    def test_t5_missing(self):
        equity = {"ivx": _empty()}
        chain = _empty()
        tier = classify_tier(
            equity_results=equity, chain_result=chain, resolved=None, original="FOO"
        )
        self.assertEqual(tier, 5)

    def test_t5_only_chain_tariff_denied_is_still_t5(self):
        equity = {"ivx": _empty()}
        chain = _denied()
        tier = classify_tier(
            equity_results=equity, chain_result=chain, resolved=None, original="FOO"
        )
        self.assertEqual(tier, 5)

    def test_t1_when_resolver_misses_but_other_endpoints_hit(self):
        # Real bug case: ivx resolver returned None but stock-prices/ivs have
        # data with original ticker. Should be T3 in v4 (smd-primary path)
        # because smd is unavailable here. Without smd: equity hit → legacy T3.
        equity = {
            "stock-prices": _ok(21, "SHLM"),
            "ivx": _empty(),
            "ivs": _ok(338, "SHLM"),
            "hv": _ok(21, "SHLM"),
        }
        chain = _ok(80, "SHLM")
        tier = classify_tier(
            equity_results=equity,
            chain_result=chain,
            resolved=None,
            original="SHLM",
            smd_result=None,
        )
        # v4: smd not provided → falls through to legacy equity check → T3
        self.assertEqual(tier, 3)

    def test_v4_t1_smd_populated_with_original(self):
        # v4 primary path: stock-market-data populated with original ticker
        smd = _smd_pop("AAPL")
        tier = classify_tier(
            equity_results={}, chain_result=None, resolved="AAPL", original="AAPL", smd_result=smd
        )
        self.assertEqual(tier, 1)

    def test_v4_t2_smd_populated_via_variant(self):
        smd = _smd_pop("SIVBQ")
        tier = classify_tier(
            equity_results={}, chain_result=None, resolved="SIVBQ", original="SIVB", smd_result=smd
        )
        self.assertEqual(tier, 2)

    def test_v4_t3_smd_empty_shell_but_legacy_hit(self):
        # smd returned 1 row but ivx30 was NaN — legacy ivx/ivs/hv have data
        smd = _smd_empty_shell("FOO")
        equity = {"ivx": _ok(20, "FOO")}
        tier = classify_tier(
            equity_results=equity, chain_result=None, resolved="FOO", original="FOO", smd_result=smd
        )
        self.assertEqual(tier, 3)

    def test_v4_t4_chain_only_smd_and_equity_both_miss(self):
        smd = _smd_empty_shell("FOO")
        equity = {"ivx": _empty()}
        chain = _ok(80, "FOO")
        tier = classify_tier(
            equity_results=equity,
            chain_result=chain,
            resolved="FOO",
            original="FOO",
            smd_result=smd,
        )
        self.assertEqual(tier, 4)

    def test_v4_t5_completely_missing(self):
        smd = _smd_empty_shell("FOO")
        equity = {"ivx": _empty()}
        chain = _empty()
        tier = classify_tier(
            equity_results=equity, chain_result=chain, resolved=None, original="FOO", smd_result=smd
        )
        self.assertEqual(tier, 5)


class TestSummarize(unittest.TestCase):
    def _make_result(self, ticker: str, tier: int, reason: str = "unknown") -> TickerProbeResult:
        return TickerProbeResult(
            requested_ticker=ticker,
            delisted_date="2023-03-08",
            reason=reason,
            tier=tier,
            resolved_ticker=ticker if tier in (1, 2) else None,
            smd_endpoint=_smd_pop(ticker) if tier in (1, 2) else None,
            equity_endpoints={},
            chain_endpoint=_empty() if tier == 5 else _ok(100),
            ground_truth=None,
            ivs_offset_used=None,
        )

    def test_tier_counts(self):
        results = [
            self._make_result("A", 1),
            self._make_result("B", 1),
            self._make_result("C", 2),
            self._make_result("D", 3),
            self._make_result("E", 4),
            self._make_result("F", 5),
        ]
        s = summarize(results)
        self.assertEqual(s["tier_counts"], {"T1": 2, "T2": 1, "T3": 1, "T4": 1, "T5": 1})
        self.assertEqual(s["total"], 6)

    def test_strict_retention_includes_only_t1_t2(self):
        # v4: T1+T2 (smd-primary) is strict gate; T3 (legacy composite) reachable but extra
        results = [
            self._make_result("A", 1),  # smd primary
            self._make_result("B", 2),  # smd via variant
            self._make_result("C", 3),  # legacy composite path
            self._make_result("D", 4),  # chain-only
            self._make_result("E", 5),  # missing
        ]
        s = summarize(results)
        self.assertAlmostEqual(s["strict_retention_pct"], 2 / 5)
        self.assertAlmostEqual(s["reachable_retention_pct"], 3 / 5)

    def test_by_reason_breakdown(self):
        results = [
            self._make_result("A", 1, reason="acquisition"),
            self._make_result("B", 1, reason="acquisition"),
            self._make_result("C", 2, reason="unknown"),
            self._make_result("D", 5, reason="unknown"),
        ]
        s = summarize(results)
        self.assertAlmostEqual(s["by_reason"]["acquisition"]["strict_retention_pct"], 2 / 2)
        self.assertAlmostEqual(s["by_reason"]["unknown"]["strict_retention_pct"], 1 / 2)


class TestEvaluateVerdict(unittest.TestCase):
    def test_strict_gates_pass(self):
        summary = {
            "total": 100,
            "tier_counts": {"T1": 90, "T2": 5, "T3": 3, "T4": 1, "T5": 1},
            "strict_retention_pct": 0.95,
            "reachable_retention_pct": 0.98,
            "by_reason": {
                "acquisition": {"n": 30, "strict_retention_pct": 0.97},
                "unknown": {"n": 70, "strict_retention_pct": 0.94},
            },
        }
        v = evaluate_verdict(summary)
        self.assertEqual(v["verdict"], "PASS")
        self.assertTrue(v["gates"]["overall_strict_retention"][2])
        self.assertTrue(v["gates"]["acquisition_strict_retention"][2])
        self.assertTrue(v["gates"]["distress_strict_retention"][2])

    def test_t3_does_not_lift_failing_strict_gate(self):
        # Lots of T3 (legacy composite) but few T1+T2 → still FAIL per zen prescription.
        # v4: T3 is workable but slower architecture; preferable for vendor verdict to require T1+T2.
        summary = {
            "total": 100,
            "tier_counts": {"T1": 30, "T2": 10, "T3": 50, "T4": 5, "T5": 5},
            "strict_retention_pct": 0.40,
            "reachable_retention_pct": 0.90,
            "by_reason": {
                "acquisition": {"n": 30, "strict_retention_pct": 0.50},
                "unknown": {"n": 70, "strict_retention_pct": 0.36},
            },
        }
        v = evaluate_verdict(summary)
        self.assertEqual(v["verdict"], "FAIL")

    def test_acquisition_gate_below_95_fails(self):
        summary = {
            "total": 100,
            "tier_counts": {"T1": 90, "T2": 0, "T3": 0, "T4": 5, "T5": 5},
            "strict_retention_pct": 0.90,
            "reachable_retention_pct": 0.90,
            "by_reason": {
                "acquisition": {"n": 50, "strict_retention_pct": 0.90},  # below 95
                "unknown": {"n": 50, "strict_retention_pct": 0.90},
            },
        }
        v = evaluate_verdict(summary)
        self.assertEqual(v["verdict"], "FAIL")
        self.assertFalse(v["gates"]["acquisition_strict_retention"][2])


if __name__ == "__main__":
    unittest.main()
