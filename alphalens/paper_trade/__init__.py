"""Prospective paper-trade infrastructure for v9D long-only forward-walk.

Replaces retrospective burnt-window experimentation with weekly forward-walk
tracking on fresh post-2026-05-04 data. Pre-registered as
``v9d_long_only_paper_trade_2026_05_04`` in signal class
``prospective_replication_2026_05_04`` (see
``docs/research/v9d_paper_trade_design_2026_05_04.md``).

Design (frozen):
- Scorer: v9D cross-sectional residual (−IVP30 orthogonalised against
  reversal_1m, momentum_6m, rv_30d).
- Selection: long-only top decile, equal-weighted, 5d rebalance.
- Benchmark: MDY.
- Cost: 30bps RT (long_only_30bps profile).
- Decision rule: 26w + 52w gates at unadjusted αt ≥ 1.96.

This module exposes only state + ledger + verdict primitives. The
scoring pipeline lives in ``alphalens.paper_trade.scorer_v9d``; CLI
entry points live in ``alphalens_cli.commands.paper_trade``.
"""

from typing import Literal

from alphalens.paper_trade.ledger import (
    LedgerEntry,
    append_ledger_entry,
    default_ledger_path,
    load_ledger,
)
from alphalens.paper_trade.state import PaperTradeState, default_state_path
from alphalens.paper_trade.verdict import (
    DecisionRuleResult,
    compute_running_stats,
    evaluate_decision_rule,
)

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"

__all__ = [
    "DecisionRuleResult",
    "LedgerEntry",
    "PaperTradeState",
    "append_ledger_entry",
    "compute_running_stats",
    "default_ledger_path",
    "default_state_path",
    "evaluate_decision_rule",
    "load_ledger",
]
