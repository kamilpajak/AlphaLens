"""Backtest harness for MVP1 evaluation of the rule-based scorer.

Replay-based daily cross-sectional simulation over the Lean-format zip store.
Pure pandas — does not require Lean Docker — so calibration iterations are
seconds instead of hours.
"""
