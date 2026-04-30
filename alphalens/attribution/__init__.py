"""Layer 5 attribution — cost-drag, factor regressions, metrics, verdict.

Consumes engine output (`alphalens.backtest.engine.BacktestReport` and the
`portfolio_returns` series) and produces risk-adjusted metrics, factor-
attribution α/β estimates, and the GO/KILL/PAPER_TRACK verdict for the
pre-registration ledger.

Modules:
- cost_model       — per-ticker bps + Almgren-Chriss participation cost
- cost_validation  — sensitivity sweeps over cost_model parameters
- factor_analysis  — Carhart-4F + FF5+UMD α/β + HAC SEs
- metrics          — Sharpe, Rank IC, turnover, concentration
- regime           — bull/bear/flat split and per-regime α/Sharpe
- decision_matrix  — 8-gate GO/KILL/PAPER_TRACK evaluator
- diagnostics      — IC by decile, horizon-IC, bear-regime decomposition
- report           — Markdown/CSV report builder consuming all the above

Layer 5 may import `alphalens.backtest.engine.BacktestReport` (the engine's
output contract). The reverse direction is forbidden by
`tests/test_module_dependencies.py` to prevent cycles.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
