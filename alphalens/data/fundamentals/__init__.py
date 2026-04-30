"""Fundamental features + soft-guardrail logic for Layer 2b scorers.

Implements issue #14: pipeline moves 50%+ of Layer 3 rejections UPSTREAM as a
score multiplier, so technical momentum picks with extreme valuation, cash
burn, or pre-profit red flags get penalised before they consume Gemini tokens.

Public API:
  - gate.fundamental_gate_score(features, config) -> float in [floor, 1.0]
  - gate.should_hard_reject(features, config) -> (bool, reason)
  - fetcher.extract_features(av_bundle) -> dict
  - fetcher.fetch_ticker_bundle(ticker, curr_date=None) -> dict
  - cache.FundamentalsCache
  - backtest_store.HistoricalFundamentalsStore

See docs/research/rejection_analysis.md for the motivating data + perplexity
literature synthesis (CAN SLIM, Quality-Minus-Junk).

Layer 2b (themed momentum) and the fundamental-gate family (#14/#15/#17) are
CLOSED. This package retained as RESEARCH_ONLY: still imported by Layer 2a
(prescreener, RESEARCH_ONLY) and the historical fundamentals store powers
backtest replay across multiple closed scorer variants.
"""

from __future__ import annotations

from typing import Literal

from .gate import fundamental_gate_score, should_hard_reject

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"

__all__ = ["fundamental_gate_score", "should_hard_reject"]
