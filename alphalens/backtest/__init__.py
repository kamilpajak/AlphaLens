"""Generic backtest harness — screener-agnostic analytics + replay engine.

Pure pandas/numpy/scipy/statsmodels modules reusable across any Layer 2
screener. The `BacktestEngine` takes a pluggable `scorer(histories, config)`
callable, so Layer 2b momentum, the archived Lean rule-based scorer, or any
future scorer can all be replayed through the same harness. Loading OHLCV
histories is the caller's responsibility — e.g.
`alphalens.screeners.lean.lean_csv_loader.load_lean_histories` for the Lean
zip-CSV store.
"""
