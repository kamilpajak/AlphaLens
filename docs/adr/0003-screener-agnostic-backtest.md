# ADR 0003 — Screener-agnostic backtest with pluggable Scorer

- **Status:** Accepted
- **Date:** 2025-12-15
- **Supersedes:** —

## Context

Each new strategy (Layer 2b momentum, Layer 2c Lean rule-based, Layer 2d
insider, Layer 2e rotation) needed the same backtest plumbing: replay loop,
factor regression, Sharpe / IC / drawdown metrics, FF3/Carhart attribution,
cost sensitivity, regime classification. Re-implementing each time would have
rotted as quickly as the strategies themselves.

We also wanted backtest comparability — running two scorers on the same
universe, same dates, same cost model, and asking "did the new one actually
add anything?"

## Decision

`alphalens/backtest/` is a single, **screener-agnostic** harness:

- `BacktestEngine(scorer, scorer_config, ...)` — replay loop with a pluggable
  callable. `Scorer = Callable[[Mapping[str, pd.DataFrame], Mapping], pd.DataFrame]`.
- `HistoryStore(histories: dict[str, pd.DataFrame])` — pure data, no I/O.
  Loading OHLCV is the caller's responsibility.
- Adapters live with their screener (`alphalens/screeners/<name>/backtest_adapter.py`),
  not inside `backtest/`. The Lean rule-based scorer needs no adapter — its
  signature already matches `Scorer`.
- Companion modules: `metrics.py`, `factor_analysis.py`, `cost_model.py`,
  `regime.py`, `theme_analysis.py`, `weighting.py`, `multiple_testing.py`,
  `historical_validation.py`, `llm_scorers.py`.

A test (`tests/test_module_dependencies.py`) enforces the rule that
`alphalens.backtest.*` cannot import from `alphalens.screeners.*` — the only
exemption is `backtest/historical_validation.py`, flagged RESEARCH-ONLY.

## Consequences

- + Validating a new candidate strategy = write a `Scorer` callable + adapter,
  reuse everything else.
- + Comparing scorers is trivial: same engine, swap the scorer argument.
- + Data loading is loosely coupled — Polygon, Lean CSV, FRED, or a fixture
  all work as long as they return `dict[ticker, OHLCV DataFrame]`.
- − Adapters live with screeners, which adds a small boilerplate per new
  strategy. Worth it: keeps the harness clean.
- ⚠ The dependency-direction rule must be honoured. New imports of
  `alphalens.screeners.*` from `backtest/*` will fail
  `tests/test_module_dependencies.py`.

## References

- `CLAUDE.md` — "Generic backtest harness" section
- `alphalens/backtest/engine.py`, `alphalens/data/store/history.py`
- `alphalens/archive/screeners/themed/backtest_adapter.py` (reference adapter)
- `tests/test_module_dependencies.py` (the enforcement)
