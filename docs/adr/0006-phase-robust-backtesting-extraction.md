# ADR 0006 — Phase-robust-backtesting extracted as standalone OSS toolkit

- **Status:** Implemented (2026-05-06)
- **Date:** 2026-04-29
- **Implemented:** 2026-05-06 (OSS v0.2.0 + AlphaLens consumption)
- **Supersedes:** —
- **Issue:** [#39](https://github.com/kamilpajak/AlphaLens/issues/39) Phase 1

## Context

Three modules built during the AlphaLens methodology audit had standalone value
beyond AlphaLens itself:

- `alphalens/preregistration/` — frozen-hypothesis ledger with class-conditional
  Bonferroni thresholds (Harvey-Liu-Zhu 2016 framework, enforced by code rather
  than by footnote).
- `alphalens/backtest/multi_phase.py` — multi-phase audit aggregator that
  collapses phase-aliasing in strided rebalance backtests; `robust_verdict`
  returns PASS/MID/FAIL using the full phase distribution.
- `alphalens/backtest/multiple_testing.py` — Bonferroni critical |t| + BH-FDR
  helpers.
- `scripts/audit_multi_phase.py` — subprocess driver that loops a backtest
  experiment script over every phase.

The bundle is genuinely self-contained (verified pre-flight: zero imports from
other AlphaLens modules outside the bundle). After 5/5 paradigm failures, this
infrastructure represents the most reusable artifact of the AlphaLens pivot to
research infrastructure (per ADR 0001).

## Decision

Extract the bundle to a public repository under the same owner:
[`kamilpajak/phase-robust-backtesting`](https://github.com/kamilpajak/phase-robust-backtesting),
MIT-licensed, pip-installable, with its own minimal CI (ruff + unittest discover).

### Mechanics

- `git filter-repo --path …` produced a history-preserving subset clone for the
  bundle paths.
- Restructured to a flat layout: `phase_robust_backtesting/{ledger.py,
  multi_phase.py, multiple_testing.py, audit_multi_phase.py, __init__.py}`.
  Subpackage `preregistration/` was dropped as it was an AlphaLens-internal
  layering convention; the OSS layout is intentionally smaller.
- Generalized the audit driver: dropped the AlphaLens-specific `_SCRIPTS`
  enum, accept `--script PATH` so any phase-offset-aware experiment can be
  audited.
- Replaced the AlphaLens `__status__` literal with a library docstring and
  `__version__ = "0.1.0"`. The status convention is project-local and not
  meaningful outside AlphaLens.
- Anti-pattern catalog (`docs/anti_patterns.md` in the new repo) summarises
  five mechanisms (phase-aliasing, single-phase point-estimate verdict,
  multiple-testing inflation, IS→OOS regime overfit, liquidity illusion) with
  concrete numbers from the AlphaLens postmortem.

### Consumption policy (2026-05-06 update — supersedes vendoring)

**Status:** AlphaLens now consumes the methodology bundle as an external
dependency, pinned to a git tag in `pyproject.toml`:

```toml
"phase-robust-backtesting @ git+https://github.com/kamilpajak/phase-robust-backtesting.git@v0.2.0",
```

Local copies of `alphalens/preregistration/`, `alphalens/backtest/multi_phase.py`,
and `alphalens/backtest/multiple_testing.py` were deleted on 2026-05-06.
`scripts/audit_multi_phase.py` was deleted on 2026-05-07 and replaced by
the first-class CLI command `alphalens audit <strategy>` (implementation
at `alphalens_cli/commands/audit.py`). The command resolves an
AlphaLens-specific strategy-name dict to a path before delegating to
`phase_robust_backtesting.audit_multi_phase.run_audit` in-process (no
subprocess wrapping — preserves traceback fidelity and Ctrl+C signal
propagation).

**Forward-flow workflow** (replacing the earlier `git subtree pull` policy,
which is now deprecated):

1. Improvements to ledger / multi_phase / multiple_testing / audit_multi_phase
   land first as PRs against `kamilpajak/phase-robust-backtesting`.
2. OSS PR merges → maintainer cuts a new tag (e.g. `v0.3.0`).
3. AlphaLens PR bumps the dep version in `pyproject.toml`, runs `uv sync`,
   commits the updated `uv.lock`. CI runs `tests/test_methodology_integration.py`
   to verify the API contract still holds.

The previous `git subtree pull --prefix=alphalens/preregistration ...`
recipe is **superseded** — do not use. Subtree vendoring would re-introduce
the drift surface this consumption policy eliminates.

## Consequences

- **Positive.** The methodology infrastructure has a clean public face,
  reusable independent of AlphaLens's strategy outcomes. Anti-pattern catalog
  is citable for anyone hitting the same retail-quant traps. Closes #39
  Phase 1.
- **Positive.** The OSS repo's CI is minimal (no SonarCloud / bandit), so
  it's far cheaper to maintain than AlphaLens.
- **Negative (mild).** Future commits that touch both repos require two PRs.
  Acceptable cost for clean OSS surface and zero drift surface (per the
  2026-05-06 consumption-policy update above).

### 2026-05-06 implementation note

After 5 days of operating with the original "two-copy" arrangement, two
real fixes (utf-8 ledger I/O, dispersion gate in `robust_verdict`) had
accumulated locally without backporting. The drift surface motivated the
shift to consuming the OSS bundle as an external dep — see the updated
"Consumption policy" section above. OSS v0.2.0 includes the backported
fixes plus a new `run_audit()` programmatic entry point that lets the
AlphaLens wrapper delegate without spawning a subprocess.

## Alternatives considered

- **Replace in-repo copies with `pip install phase-robust-backtesting` as a
  hard dependency.** Rejected for now — adds a release cadence + version-pin
  burden disproportionate to the gain.
- **Keep the toolkit in AlphaLens, link OSS users at the AlphaLens repo.**
  Rejected — AlphaLens has too much paradigm-failure-specific surface to be
  a useful OSS entry point for a methodology toolkit.
- **Apache 2.0 license** (per Perplexity recommendation in #39 strategic
  consult). Rejected — MIT matches AlphaLens's existing license and is
  simpler. No patent considerations apply to a small validation library.

## References

- Issue: [#39](https://github.com/kamilpajak/AlphaLens/issues/39)
- New repo: <https://github.com/kamilpajak/phase-robust-backtesting>
- Anti-pattern catalog: <https://github.com/kamilpajak/phase-robust-backtesting/blob/main/docs/anti_patterns.md>
- Closed-layer policy: [ADR 0005](0005-closed-layers-as-anti-pattern-catalog.md)

## Amendment 2026-05-13 — `Registration.extras` (PRB v0.2.2)

PRB v0.2.2 adds a `Registration.extras: dict[str, Any]` field and a
`Ledger.complete(outcome_extras=...)` kwarg ([phase-robust-backtesting#1](https://github.com/kamilpajak/phase-robust-backtesting/pull/1)).
The hook addresses [AlphaLens#105 H3](https://github.com/kamilpajak/AlphaLens/issues/105):
two ledger entries had drifted to carry top-level `phase_a_result` forensic
data outside the v0.2.1 closed schema, breaking `Ledger._load()` on the next
reload. The fix preserves PRB's methodology-agnostic posture (no AlphaLens-
specific fields landed in the dataclass) while letting paradigm orchestrators
attach structured forensics — phase-A pre-screen results, pod compute logs,
postmortem links, `windows_evaluated` — through the API.

Resolution order on read: declared fields first, then unknown top-level keys
route into `extras`. On write: `to_dict()` flattens `extras` back to
top-level so the on-disk JSON shape stays identical to pre-v0.2.2 entries
(zero git-diff churn on round-trip).

AlphaLens consumer wiring:
- `alphalens preregister complete --extras-json <path>` accepts paradigm-
  specific forensic JSON merged into the outcome dict.
- Paradigm orchestrators (template: `scripts/run_ev_fcff_yield_audit.py`)
  pass `outcome_extras=` through the API rather than patching `ledger.json`
  manually post-hoc.

Same-PR ledger cleanup forced canonical statuses on 3 drift-entries
(`audited`, `completed_pass_marginal`, `execution_aborted_units_mismatch`
→ `completed`/`abandoned`); rich values preserved under `status_extended`
top-level key (auto-routed into `extras` via PRB v0.2.2). Also renamed the
ev_fcff_yield outcome's `mean_excess_net_ann` → canonical `mean_excess_net`.
