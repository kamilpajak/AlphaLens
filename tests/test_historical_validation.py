import unittest
from datetime import date

import pandas as pd


def _mk_pick(date_str, ticker, rank=1, score=0.8, themes=None, fwd=0.01):
    from alphalens.backtest.historical_validation import PickRecord

    return PickRecord(
        asof_date=date.fromisoformat(date_str),
        ticker=ticker,
        rank=rank,
        momentum_score=score,
        themes=themes or ["quantum"],
        forward_return=fwd,
    )


def _always_accept(ticker, asof, context):
    from alphalens.backtest.historical_validation import LLMVerdict

    return LLMVerdict(verdict="accept", confidence=0.9, cost_usd=0.01, latency_sec=0.5)


def _always_reject(ticker, asof, context):
    from alphalens.backtest.historical_validation import LLMVerdict

    return LLMVerdict(verdict="reject", confidence=0.8, cost_usd=0.01, latency_sec=0.5)


def _smart_scorer(ticker, asof, context):
    """Perfect signal: accept tickers starting with 'A' (which we'll arrange to
    have positive forward returns in the test)."""
    from alphalens.backtest.historical_validation import LLMVerdict

    if ticker.startswith("A"):
        return LLMVerdict(verdict="accept", confidence=0.9, cost_usd=0.01)
    return LLMVerdict(verdict="reject", confidence=0.9, cost_usd=0.01)


class TestEvaluateHistoricalPicks(unittest.TestCase):
    def test_empty_picks_returns_zero_result(self):
        from alphalens.backtest.historical_validation import (
            evaluate_historical_picks,
        )

        result = evaluate_historical_picks([], _always_accept)
        self.assertEqual(result.n_total, 0)
        self.assertEqual(result.n_accept, 0)

    def test_always_accept_gives_100_pct_accept_rate(self):
        from alphalens.backtest.historical_validation import (
            evaluate_historical_picks,
        )

        picks = [_mk_pick(f"2024-01-{d:02d}", f"T{d}") for d in range(1, 6)]
        result = evaluate_historical_picks(picks, _always_accept)
        self.assertEqual(result.n_total, 5)
        self.assertEqual(result.n_accept, 5)
        self.assertEqual(result.accept_rate, 1.0)
        self.assertAlmostEqual(result.total_llm_cost_usd, 0.05)

    def test_smart_scorer_detects_edge(self):
        """A scorer that distinguishes good from bad picks should yield positive delta."""
        from alphalens.backtest.historical_validation import (
            evaluate_historical_picks,
        )

        picks = [
            _mk_pick("2024-01-01", "AAA", fwd=0.05),  # A → accept + positive fwd
            _mk_pick("2024-01-02", "AAB", fwd=0.03),
            _mk_pick("2024-01-03", "BAD", fwd=-0.02),  # not A → reject + negative fwd
            _mk_pick("2024-01-04", "BADX", fwd=-0.01),
            _mk_pick("2024-01-05", "AAC", fwd=0.04),
        ]
        result = evaluate_historical_picks(picks, _smart_scorer)
        self.assertEqual(result.n_accept, 3)
        self.assertEqual(result.n_reject, 2)
        self.assertGreater(result.delta_accept_minus_reject, 0.02)

    def test_noisy_scorer_gives_near_zero_delta(self):
        """A random scorer should yield delta ~0 (baseline for 'no value add')."""
        import random

        from alphalens.backtest.historical_validation import (
            LLMVerdict,
            evaluate_historical_picks,
        )

        def random_scorer(ticker, asof, ctx):
            rnd = random.Random(hash(ticker) % 1000)
            v = "accept" if rnd.random() > 0.5 else "reject"
            return LLMVerdict(verdict=v, confidence=0.5, cost_usd=0.0)

        rng = [0.01, -0.02, 0.03, -0.01, 0.02, -0.03, 0.015, -0.015] * 10
        picks = [
            _mk_pick(f"2024-01-{(i % 28) + 1:02d}", f"T{i}", fwd=rng[i]) for i in range(len(rng))
        ]
        result = evaluate_historical_picks(picks, random_scorer)
        self.assertLess(abs(result.delta_accept_minus_reject), 0.03)

    def test_scorer_exception_treated_as_uncertain(self):
        from alphalens.backtest.historical_validation import (
            evaluate_historical_picks,
        )

        def broken_scorer(ticker, asof, ctx):
            raise RuntimeError("api down")

        picks = [_mk_pick("2024-01-01", "X")]
        result = evaluate_historical_picks(picks, broken_scorer)
        self.assertEqual(result.n_uncertain, 1)
        self.assertEqual(result.n_accept, 0)


class TestDecisionMatrix(unittest.TestCase):
    def test_deploy_verdict_when_clear_edge(self):
        from alphalens.backtest.historical_validation import (
            ValidationResult,
            format_decision_matrix,
        )

        r = ValidationResult(
            n_total=100,
            n_accept=40,
            n_reject=60,
            n_uncertain=0,
            accept_rate=0.4,
            accept_mean_return=0.015,
            reject_mean_return=0.002,
            delta_accept_minus_reject=0.013,
            accept_hit_rate=0.65,
            reject_hit_rate=0.45,
            accept_sharpe_proxy=1.5,
            reject_sharpe_proxy=0.2,
            total_llm_cost_usd=15.0,
            total_llm_latency_sec=100.0,
        )
        text = format_decision_matrix(r)
        self.assertIn("DEPLOY", text)

    def test_skip_verdict_when_no_edge(self):
        from alphalens.backtest.historical_validation import (
            ValidationResult,
            format_decision_matrix,
        )

        r = ValidationResult(
            n_total=100,
            n_accept=50,
            n_reject=50,
            n_uncertain=0,
            accept_rate=0.5,
            accept_mean_return=0.005,
            reject_mean_return=0.005,
            delta_accept_minus_reject=0.0,
            accept_hit_rate=0.5,
            reject_hit_rate=0.5,
            accept_sharpe_proxy=0.5,
            reject_sharpe_proxy=0.5,
            total_llm_cost_usd=15.0,
            total_llm_latency_sec=100.0,
        )
        text = format_decision_matrix(r)
        self.assertIn("SKIP", text)

    def test_iterate_verdict_marginal(self):
        from alphalens.backtest.historical_validation import (
            ValidationResult,
            format_decision_matrix,
        )

        r = ValidationResult(
            n_total=100,
            n_accept=50,
            n_reject=50,
            n_uncertain=0,
            accept_rate=0.5,
            accept_mean_return=0.008,
            reject_mean_return=0.005,
            delta_accept_minus_reject=0.003,
            accept_hit_rate=0.55,
            reject_hit_rate=0.52,
            accept_sharpe_proxy=0.7,
            reject_sharpe_proxy=0.3,
            total_llm_cost_usd=10.0,
            total_llm_latency_sec=50.0,
        )
        text = format_decision_matrix(r)
        self.assertIn("ITERATE", text)


class TestRuleBasedTractabilityScorer(unittest.TestCase):
    def test_accepts_top_rank(self):
        from alphalens.backtest.historical_validation import (
            rule_based_tractability_scorer,
        )

        verdict = rule_based_tractability_scorer(
            "AAA",
            date(2024, 1, 1),
            {"rank": 1, "momentum_score": 0.3, "themes": ["q"]},
        )
        self.assertEqual(verdict.verdict, "accept")

    def test_accepts_high_score(self):
        from alphalens.backtest.historical_validation import (
            rule_based_tractability_scorer,
        )

        verdict = rule_based_tractability_scorer(
            "XXX",
            date(2024, 1, 1),
            {"rank": 5, "momentum_score": 0.8, "themes": ["ai"]},
        )
        self.assertEqual(verdict.verdict, "accept")

    def test_rejects_low_rank_low_score(self):
        from alphalens.backtest.historical_validation import (
            rule_based_tractability_scorer,
        )

        verdict = rule_based_tractability_scorer(
            "NNN",
            date(2024, 1, 1),
            {"rank": 5, "momentum_score": 0.3, "themes": ["biotech"]},
        )
        self.assertEqual(verdict.verdict, "reject")


class TestPicksFromBacktestReport(unittest.TestCase):
    def test_extracts_picks_with_forward_returns(self):
        from alphalens.backtest.engine import BacktestReport, DailyResult
        from alphalens.backtest.historical_validation import (
            picks_from_backtest_report,
        )

        report = BacktestReport(
            scorer_config={},
            holding_period=5,
            top_n=3,
            start=date(2024, 1, 1),
            end=date(2024, 1, 5),
            benchmark="SPY",
            universe_ticker_count=100,
            daily_results=[
                DailyResult(
                    date=pd.Timestamp("2024-01-02"),
                    scored_count=50,
                    top_n_tickers=["A", "B", "C"],
                    top_n_scores=[1.0, 0.9, 0.8],
                    top_n_forward_returns=[0.02, -0.01, 0.03],
                    portfolio_return=0.01,
                    portfolio_return_holding=0.015,
                    universe_median_return=0.005,
                    ic=0.05,
                ),
            ],
        )
        picks = picks_from_backtest_report(report)
        self.assertEqual(len(picks), 3)
        self.assertEqual(picks[0].ticker, "A")
        self.assertEqual(picks[0].rank, 1)
        self.assertAlmostEqual(picks[1].forward_return, -0.01)


if __name__ == "__main__":
    unittest.main()
