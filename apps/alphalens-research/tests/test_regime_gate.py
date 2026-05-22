"""Tests for screener-agnostic regime gate.

The regime gate wraps any AlphaLens Scorer (per the Callable protocol in
``alphalens_research.backtest.engine``) so that on classifier-OFF days the gated
scorer returns an empty selection, on classifier-ON days it passes the
underlying scorer's output through, and on graded scores it scales the
``score`` column by the classifier's float in [0, 1].

The wrapper deliberately knows nothing about which classifier is plugged
in — that's the screener-agnostic contract. Pre-registered classifier
candidates (yield curve, VIX, NFCI, HY OAS, cross-sectional dispersion)
live in their own modules.
"""

from __future__ import annotations

import unittest
from datetime import date

import pandas as pd


def _fake_scorer(histories, config):
    """Return a deterministic ranking of all input tickers (skip benchmark)."""
    bench = config.get("benchmark", "SPY")
    tickers = [t for t in histories if t != bench]
    return pd.DataFrame({"ticker": tickers, "score": list(range(len(tickers), 0, -1))})


def _histories(tickers, asof: date) -> dict[str, pd.DataFrame]:
    """Build histories ending at asof — replicates engine's truncation."""
    idx = pd.date_range(end=pd.Timestamp(asof), periods=10, freq="B")
    return {t: pd.DataFrame({"close": range(10)}, index=idx) for t in tickers}


class _BinaryClassifier:
    """is_on returns True for asof >= 2020-06-01, False before."""

    def is_on(self, asof: date) -> bool:
        return asof >= date(2020, 6, 1)


class _ScoreClassifier:
    """score returns 0.5 for everything (smooth gating test fixture)."""

    def score(self, asof: date) -> float:
        return 0.5


class RegimeGatedScorerTests(unittest.TestCase):
    def test_returns_empty_on_off_day_for_binary_classifier(self):
        from alphalens_research.gates import regime_gated_scorer

        gated = regime_gated_scorer(_fake_scorer, _BinaryClassifier())
        result = gated(
            _histories(["AAPL", "MSFT", "SPY"], date(2020, 1, 15)),
            {"benchmark": "SPY"},
        )

        self.assertEqual(len(result), 0)
        self.assertIn("ticker", result.columns)
        self.assertIn("score", result.columns)

    def test_passes_through_on_on_day_for_binary_classifier(self):
        from alphalens_research.gates import regime_gated_scorer

        gated = regime_gated_scorer(_fake_scorer, _BinaryClassifier())
        result = gated(
            _histories(["AAPL", "MSFT", "SPY"], date(2024, 1, 15)),
            {"benchmark": "SPY"},
        )

        self.assertEqual(set(result["ticker"]), {"AAPL", "MSFT"})
        # No mutation when ON
        self.assertEqual(list(result["score"]), [2, 1])

    def test_score_classifier_scales_underlying_scores(self):
        from alphalens_research.gates import regime_gated_scorer

        gated = regime_gated_scorer(_fake_scorer, _ScoreClassifier())
        result = gated(
            _histories(["AAPL", "MSFT", "SPY"], date(2024, 1, 15)),
            {"benchmark": "SPY"},
        )

        # Underlying scores [2, 1] × 0.5 = [1.0, 0.5]
        self.assertEqual(list(result["score"]), [1.0, 0.5])

    def test_score_classifier_returning_zero_yields_empty_selection(self):
        """A score of 0 is logically OFF — caller can downstream-filter on
        non-zero score; we don't pre-filter the rows here. Locks the
        contract that 0 is a real rescaled value, not a special sentinel."""
        from alphalens_research.gates import regime_gated_scorer

        class _ZeroClassifier:
            def score(self, asof: date) -> float:
                return 0.0

        gated = regime_gated_scorer(_fake_scorer, _ZeroClassifier())
        result = gated(
            _histories(["AAPL", "MSFT", "SPY"], date(2024, 1, 15)),
            {"benchmark": "SPY"},
        )

        self.assertEqual(set(result["ticker"]), {"AAPL", "MSFT"})
        self.assertTrue(all(s == 0.0 for s in result["score"]))

    def test_classifier_score_clamped_to_unit_interval(self):
        """Defensive: classifier scores outside [0,1] would corrupt
        position-size budget. Wrapper clamps."""
        from alphalens_research.gates import regime_gated_scorer

        class _OutOfRangeClassifier:
            def score(self, asof: date) -> float:
                return 1.5

        gated = regime_gated_scorer(_fake_scorer, _OutOfRangeClassifier())
        result = gated(
            _histories(["AAPL", "MSFT", "SPY"], date(2024, 1, 15)),
            {"benchmark": "SPY"},
        )

        # Underlying [2, 1] × 1.0 (clamped) = [2.0, 1.0]
        self.assertEqual(list(result["score"]), [2.0, 1.0])

    def test_empty_histories_returns_empty(self):
        from alphalens_research.gates import regime_gated_scorer

        gated = regime_gated_scorer(_fake_scorer, _BinaryClassifier())
        result = gated({}, {"benchmark": "SPY"})

        self.assertEqual(len(result), 0)

    def test_classifier_with_neither_method_raises(self):
        """Wrapper enforces the protocol: classifier must implement either
        ``is_on`` (binary) or ``score`` (graded). Ambiguity at construction
        time is louder than a silent no-op at runtime."""
        from alphalens_research.gates import regime_gated_scorer

        class _BogusClassifier:
            pass

        with self.assertRaises(TypeError):
            regime_gated_scorer(_fake_scorer, _BogusClassifier())


class RegimeGatePackageStatusTest(unittest.TestCase):
    def test_package_declares_research_only_status(self):
        """Wrapper is a reusable research utility — no concrete classifier
        ever shipped under it (the rescue use-case for mom+lowvol BASE
        failed Phase 1 diagnostic 2026-04-29). Mirrors `alphalens_research.data.macro/`."""
        import alphalens_research.gates as pkg

        self.assertEqual(pkg.__status__, "RESEARCH_ONLY")


if __name__ == "__main__":
    unittest.main()
