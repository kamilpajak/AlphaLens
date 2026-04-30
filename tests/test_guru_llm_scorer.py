"""Tests for alphalens.archive.guru.llm_scorer — structured JSON + cost tracking + cache."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

from alphalens.archive.guru.prompt import GuruPrompt


def _fake_prompt() -> GuruPrompt:
    return GuruPrompt(
        text="You are a value investor. Score the company.",
        content_sha256="a" * 64,
        git_sha="b" * 40,
        path="/tmp/p.txt",
    )


def _mock_llm_response(content: str, input_tokens: int = 1000, output_tokens: int = 100):
    resp = MagicMock()
    resp.content = content
    resp.usage_metadata = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    return resp


class TestGuruScorer(unittest.TestCase):
    def test_returns_conviction_result_with_parsed_json(self):
        from alphalens.archive.guru.llm_scorer import GuruScorer

        llm = MagicMock()
        llm.invoke.return_value = _mock_llm_response(
            '{"conviction": 82, "rationale": "Strong moat, high ROE"}'
        )

        with tempfile.TemporaryDirectory() as tmp:
            scorer = GuruScorer(prompt=_fake_prompt(), llm=llm, cache_dir=Path(tmp))
            result = scorer.score(
                ticker="AAPL",
                asof=pd.Timestamp("2018-01-01"),
                context_text="COMPANY: AAPL...",
            )

        self.assertEqual(result.ticker, "AAPL")
        self.assertAlmostEqual(result.conviction, 82.0)
        self.assertIn("moat", result.rationale.lower())
        self.assertEqual(result.prompt_sha, "a" * 64)

    def test_parses_json_from_markdown_codeblock_if_wrapped(self):
        from alphalens.archive.guru.llm_scorer import GuruScorer

        llm = MagicMock()
        llm.invoke.return_value = _mock_llm_response(
            '```json\n{"conviction": 45, "rationale": "Cyclical business"}\n```'
        )

        with tempfile.TemporaryDirectory() as tmp:
            scorer = GuruScorer(prompt=_fake_prompt(), llm=llm, cache_dir=Path(tmp))
            result = scorer.score(ticker="F", asof=pd.Timestamp("2018-01-01"), context_text="")

        self.assertAlmostEqual(result.conviction, 45.0)

    def test_handles_malformed_json(self):
        from alphalens.archive.guru.llm_scorer import GuruScorer, ScorerError

        llm = MagicMock()
        llm.invoke.return_value = _mock_llm_response("I don't have enough info.")

        with tempfile.TemporaryDirectory() as tmp:
            scorer = GuruScorer(prompt=_fake_prompt(), llm=llm, cache_dir=Path(tmp))
            with self.assertRaises(ScorerError):
                scorer.score(ticker="X", asof=pd.Timestamp("2018-01-01"), context_text="")

    def test_clamps_conviction_to_0_100_range(self):
        from alphalens.archive.guru.llm_scorer import GuruScorer

        llm = MagicMock()
        llm.invoke.return_value = _mock_llm_response('{"conviction": 125, "rationale": "amazing"}')

        with tempfile.TemporaryDirectory() as tmp:
            scorer = GuruScorer(prompt=_fake_prompt(), llm=llm, cache_dir=Path(tmp))
            result = scorer.score(ticker="Z", asof=pd.Timestamp("2018-01-01"), context_text="")

        self.assertAlmostEqual(result.conviction, 100.0)

    def test_tracks_token_usage_and_cost(self):
        from alphalens.archive.guru.llm_scorer import GuruScorer

        llm = MagicMock()
        llm.invoke.return_value = _mock_llm_response(
            '{"conviction": 70, "rationale": "ok"}',
            input_tokens=5000,
            output_tokens=200,
        )

        with tempfile.TemporaryDirectory() as tmp:
            # Use explicit pricing: $1.25 input, $5.00 output per 1M tokens
            scorer = GuruScorer(
                prompt=_fake_prompt(),
                llm=llm,
                cache_dir=Path(tmp),
                input_price_per_1m=1.25,
                output_price_per_1m=5.0,
            )
            result = scorer.score(ticker="T", asof=pd.Timestamp("2018-01-01"), context_text="")

        self.assertEqual(result.input_tokens, 5000)
        self.assertEqual(result.output_tokens, 200)
        # Cost: 5000/1M * $1.25 + 200/1M * $5.0 = $0.00625 + $0.001 = $0.00725
        self.assertAlmostEqual(result.cost_usd, 0.00725, places=6)

    def test_disk_cache_avoids_duplicate_llm_calls(self):
        from alphalens.archive.guru.llm_scorer import GuruScorer

        llm = MagicMock()
        llm.invoke.return_value = _mock_llm_response('{"conviction": 60, "rationale": "ok"}')

        with tempfile.TemporaryDirectory() as tmp:
            scorer = GuruScorer(prompt=_fake_prompt(), llm=llm, cache_dir=Path(tmp))
            r1 = scorer.score(ticker="AAPL", asof=pd.Timestamp("2018-01-01"), context_text="x")
            r2 = scorer.score(ticker="AAPL", asof=pd.Timestamp("2018-01-01"), context_text="x")

        # Both return same result but LLM called only once
        self.assertEqual(llm.invoke.call_count, 1)
        self.assertAlmostEqual(r1.conviction, r2.conviction)

    def test_cache_miss_for_different_prompt_sha(self):
        from alphalens.archive.guru.llm_scorer import GuruScorer

        llm = MagicMock()
        llm.invoke.return_value = _mock_llm_response('{"conviction": 60, "rationale": "ok"}')

        p1 = _fake_prompt()
        p2 = GuruPrompt(
            text="different",
            content_sha256="f" * 64,
            git_sha="0" * 40,
            path="/tmp/p2.txt",
        )

        with tempfile.TemporaryDirectory() as tmp:
            s1 = GuruScorer(prompt=p1, llm=llm, cache_dir=Path(tmp))
            s2 = GuruScorer(prompt=p2, llm=llm, cache_dir=Path(tmp))
            s1.score(ticker="AAPL", asof=pd.Timestamp("2018-01-01"), context_text="x")
            s2.score(ticker="AAPL", asof=pd.Timestamp("2018-01-01"), context_text="x")

        # Different prompts → 2 calls
        self.assertEqual(llm.invoke.call_count, 2)


if __name__ == "__main__":
    unittest.main()
