"""Diagnostic tooling for post-hoc stress tests on existing strategies.

Houses reusable pure-function helpers consumed by single-purpose
diagnostic scripts under ``scripts/diagnostics/``. Each diagnostic
re-evaluates an existing pre-registered hypothesis under varying
assumptions (cost overhead, vol regime, etc.) without consuming
Bonferroni budget.

Current diagnostics:
- ``slippage_regime`` — regime-amplified bid-ask spread cost diagnostic.
  Pre-reg memo:
  ``docs/research/insider_form4_opportunistic_slippage_stress_design_2026_05_12.md``.
"""

__status__ = "ACTIVE"
