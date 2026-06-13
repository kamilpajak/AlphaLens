"""O'Neil momentum / technical lens — numeric-only (epic #541 PR-7).

A CANSLIM-**reduced** second expert beside Buffett. v1 ships three letters:
**N** (proximity to the 52-week high), **L** (leader / MA200 up-trend), and
**C/A** (latest-FY net-income YoY growth). **R** (relative strength) is DEFERRED
(the grouped-daily disk cache cannot support a PIT-correct, split-clean RS yet);
**S** and **I** are dropped (no usable supply / institutional data). See
``docs/research/oneil_expert_design_2026_06_13.md``.

Numeric-only: ``assess_qualitative`` returns ``None`` (zero LLM cost) and the
expert is NOT a ``QualEnrichExpert`` (no eager qualitative layer). Display-only —
no O'Neil column enters the brief sort (the PR-6 allowlist enforces this) until a
per-expert O'Neil×EDGE correlation is validated (N≥30, ~2026-09+).
"""

__status__ = "ACTIVE"
