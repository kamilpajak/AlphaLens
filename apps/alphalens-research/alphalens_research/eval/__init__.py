"""Reading-quality / generation-fidelity evaluation harness (T1-T6).

Implements the LOCKED design memo
``docs/research/reading_quality_eval_design_2026_07_11.md``. The first (and
currently only) deliverable is the **T6 brief-faithfulness pilot** in
:mod:`alphalens_research.eval.faithfulness` — a deterministic-first scorer that
flags fabricated numeric/date atoms and characterization violations in the
generated brief against the injected ``<facts>`` block.

Scope guardrail (memo §2): the harness measures reading / generation-fidelity
correctness and is deliberately NOT joined to any outcome or return ledger. A
green reading dashboard never means "the tool makes money". No reading or
faithfulness metric may be cited to inform, tune, or justify a selection,
ordering, or exit decision.

Status: research telemetry only. ``eval/`` is intentionally NOT under
``LAYER_ROOTS`` in ``tests/test_layer_status.py`` (the eight roots are
screeners / gates / backtest / overlays / attribution / preaudit / diagnostics /
retrospective_audit), so ``__status__`` here is optional-but-validated-if-present.
Declaring it is good hygiene per the memo §8.
"""

__status__ = "RESEARCH_ONLY"
