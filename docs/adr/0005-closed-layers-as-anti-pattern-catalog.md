# ADR 0005 — Closed layers retained as anti-pattern catalog

- **Status:** Accepted
- **Date:** 2026-04-25
- **Supersedes:** —

## Context

After ADR 0001 (research-infrastructure pivot), the question was what to do
with the code for failed paradigms: Layer 2b themed (CLOSED 2026-04-22),
Layer 2c Lean (ARCHIVED 2026-04-19), Layer 2d insider (CLOSED 2026-04-24),
Layer 2e rotation, Layer 2f events, Layer 2g guru.

Two options:

1. **Delete** — clean tree, smaller surface, no risk of someone reactivating
   a known-bad strategy.
2. **Retain with explicit status** — keep everything, mark each package with
   a lifecycle label, and treat the codebase as a learning artefact.

Deleting would also delete the postmortem trail that lives in commits and
adjacent test files. Reusable infrastructure (e.g. `alphalens/macro/` FRED
client, `alphalens/rotation/sanity_checks.py`, screener-agnostic backtest
harness) is interleaved with paradigm-specific code in those packages.

## Decision

Closed and archived layers stay in the codebase. Each package's `__init__.py`
declares an explicit lifecycle status:

```python
__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "CLOSED"
__closed_date__ = "2026-04-22"
__closed_reason__ = "Momentum overfit OOS; realistic execution cost ~100% ann eats signal"
```

A test (`tests/test_layer_status.py`) enforces that every layer in the
allowlist declares a valid status, and that CLOSED/ARCHIVED layers carry a
reason. Adding a new layer requires adding it to `LAYERS_WITH_STATUS` —
forced explicit decision.

We deliberately do **not** add `warnings.warn()` on import of CLOSED layers.
Imports are intentional (backtest replay, anti-pattern reference, reusable
infra extraction) and would produce noise without value.

## Consequences

- + Future-self / new readers can see at a glance what is live, what is dead,
  and why — without grepping CLAUDE.md and memory.
- + Reusable infrastructure inside CLOSED packages (e.g. `OverlayEngine`,
  `FREDClient`, `sanity_checks`) remains accessible.
- + The "kill-fast" methodology is preserved as evidence — postmortem
  documents reference live code.
- − Larger surface than necessary for production. Acceptable because
  production is currently just Layer 1 watchdog.
- ⚠ New screeners must be added to `LAYERS_WITH_STATUS` in
  `tests/test_layer_status.py` or the test fails. This is by design.
- ⚠ Cross-layer imports from `backtest/` to closed `screeners/*` packages
  must go through `tests/test_module_dependencies.py` exemption with a
  written reason.

## References

- ADR 0001 (research-infrastructure pivot)
- `tests/test_layer_status.py`, `tests/test_module_dependencies.py`
- `docs/research/paradigm_failures_postmortem.md`
- Memory: `project_archive_decisions.md`,
  `project_themed_screener_design.md`, `project_pivot_alt_data.md`,
  `project_tactical_rotation_closed.md`
