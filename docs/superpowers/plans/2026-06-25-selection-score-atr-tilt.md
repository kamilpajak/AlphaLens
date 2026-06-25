# ATR-soft-tilt `selection_score` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deprioritize high-ATR (extended/volatile) candidates in the daily brief via a new continuous `selection_score`, with scoring transparency on the brief card + a `scorer_config_version` poolability chip, without touching the selection funnel or the EDGE test set.

**Architecture:** A pure module computes `selection_score = layer4_weighted_score − atr_penalty(technical_atr_pct)`; the score stage (`scorer.py`) stamps `selection_score`, `atr_penalty`, and `scorer_config_version` columns onto every candidate; the brief sort (`argumentation/orchestrator.py`) makes `selection_score` the primary sort key; Django persists + serves the three columns; the SPA brief card shows an `extended` band + a deep-read breakdown drawer, and `/edge` shows the version chip. `atr_penalty` is a per-ticker constant (ATR depends only on ticker+asof), which is what keeps `drop_duplicates(keep="first")` survivor-invariant — the test-set-safety guarantee.

**Tech Stack:** Python 3.12 / pandas (pipeline), Django REST Framework + Postgres (API), SvelteKit 5 + Tailwind (web). uv workspace, pnpm web.

**Spec:** `docs/superpowers/specs/2026-06-25-selection-score-atr-tilt-design.md`

## Global Constraints

- **TDD always** — red → green → refactor, even 2-line changes. Test first.
- **Worktree** — implement in a `git worktree` off fresh `origin/main` (this is NOT the main checkout); editing pipeline/research package code in a worktree needs its own `uv sync` in the worktree.
- **zen pre-merge** — each non-trivial PR: push → open PR → `mcp__zen__codereview` with `deepseek/deepseek-v4-pro` (`thinking_mode="high"`) → apply findings as new commits → CI green on latest → merge.
- **English-only in code** (comments/docstrings/identifiers). Math notation (α, ρ, ×, −) OK. Enforced by `test_no_polish_chars.py`.
- **Conventional Commits** (`type(scope): description`); commit messages never mention AI assistance.
- **SonarCloud**: `# NOSONAR` at end-of-line; NaN via `math.isnan`, not `x != x`; avoid float `==`.
- **Diff-coverage ≥80%** — scripts are coverage-excluded; production code moved/added needs tests in the same PR.
- **No backward compatibility shims** — solo project; rename/replace in one commit.
- **Config-version pattern** — mirror `INSIDER_SIGNAL_VERSION` exactly: a module constant stamped as a column on EVERY row.
- **gh CLI** — always `--repo kamilpajak/AlphaLens` for PR ops.
- **Frozen pre-reg files** — do NOT edit `opportunistic_form4` or any SHA-locked scorer; this work is in the thematic screening path only.

---

# PR 1 — Pipeline: `selection_score` + version stamp + sort key

**Branch:** `feature/selection-score-atr-tilt-pipeline`

## File structure (PR 1)

- Create: `apps/alphalens-pipeline/alphalens_pipeline/thematic/screening/selection_score.py` — pure ATR-penalty math + version constant (single responsibility, testable without I/O).
- Create: `apps/alphalens-research/tests/thematic/screening/test_selection_score.py` — unit tests for the pure module.
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/thematic/screening/scorer.py` — stamp `scorer_config_version` (in `_build_candidate_row`, ~line 368 beside `insider_signal_version`) + `atr_penalty`/`selection_score` (after the `layer4_weighted_score` loop, ~line 588).
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/thematic/argumentation/orchestrator.py` — prepend `selection_score` to `_BRIEF_SORT_KEYS` (~line 333).
- Modify: `apps/alphalens-research/tests/thematic/argumentation/test_sort_and_dedup.py` — `_row` default + allowlist + equality pin + primary-sort test + new demotion test.
- Modify: `apps/alphalens-research/tests/thematic/screening/test_scorer.py` — assert the three new columns on the output frame + on a WRITTEN parquet.

---

### Task 1.1: Pure `selection_score` module

**Files:**
- Create: `apps/alphalens-pipeline/alphalens_pipeline/thematic/screening/selection_score.py`
- Test: `apps/alphalens-research/tests/thematic/screening/test_selection_score.py`

**Interfaces:**
- Produces: `atr_penalty(atr_pct: float | None) -> float` (∈ [0, LAMBDA]); `selection_score(layer4_weighted_score: float, atr_pct: float | None) -> float`; constants `ATR_RAMP_LO=5.77`, `ATR_RAMP_HI=8.37`, `LAMBDA=1.0`, `SCORER_CONFIG_VERSION="scorer-v1-atrtilt-lam1.0-lo5.77-hi8.37"`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/alphalens-research/tests/thematic/screening/test_selection_score.py
"""Pure ATR-penalty / selection_score math (no I/O)."""

from __future__ import annotations

import math
import unittest

from alphalens_pipeline.thematic.screening import selection_score as ss


class TestAtrPenalty(unittest.TestCase):
    def test_zero_at_or_below_p67_onset(self):
        self.assertEqual(ss.atr_penalty(0.0), 0.0)
        self.assertEqual(ss.atr_penalty(4.0), 0.0)
        self.assertEqual(ss.atr_penalty(ss.ATR_RAMP_LO), 0.0)  # exactly p67 → 0

    def test_full_lambda_at_or_above_p90(self):
        self.assertEqual(ss.atr_penalty(ss.ATR_RAMP_HI), ss.LAMBDA)
        self.assertEqual(ss.atr_penalty(20.0), ss.LAMBDA)

    def test_linear_ramp_midpoint(self):
        mid = (ss.ATR_RAMP_LO + ss.ATR_RAMP_HI) / 2.0
        self.assertAlmostEqual(ss.atr_penalty(mid), ss.LAMBDA / 2.0, places=9)

    def test_monotonic_non_decreasing(self):
        xs = [0, 4, 5.77, 6.5, 7.1, 8.37, 12]
        ps = [ss.atr_penalty(x) for x in xs]
        self.assertEqual(ps, sorted(ps))

    def test_missing_or_nan_or_nonpositive_is_zero(self):
        self.assertEqual(ss.atr_penalty(None), 0.0)
        self.assertEqual(ss.atr_penalty(float("nan")), 0.0)
        self.assertEqual(ss.atr_penalty(-1.0), 0.0)
        self.assertEqual(ss.atr_penalty("bad"), 0.0)  # type: ignore[arg-type]


class TestSelectionScore(unittest.TestCase):
    def test_calm_name_keeps_layer4(self):
        self.assertEqual(ss.selection_score(4, 3.0), 4.0)

    def test_extended_name_loses_one_tier_at_p90(self):
        self.assertEqual(ss.selection_score(4, 9.0), 3.0)

    def test_version_string_is_pinned(self):
        self.assertEqual(ss.SCORER_CONFIG_VERSION, "scorer-v1-atrtilt-lam1.0-lo5.77-hi8.37")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m unittest apps.alphalens-research.tests.thematic.screening.test_selection_score -v` (from repo root, or `cd apps/alphalens-research && uv run python -m unittest discover -s tests/thematic/screening -t . -k test_selection_score`)
Expected: FAIL — `ModuleNotFoundError: ...selection_score`.

- [ ] **Step 3: Write the module**

```python
# apps/alphalens-pipeline/alphalens_pipeline/thematic/screening/selection_score.py
"""ATR-soft-tilt selection score: deprioritize high-ATR (extended/volatile) names.

``selection_score = layer4_weighted_score − atr_penalty(technical_atr_pct)``.

``atr_penalty`` is a function of ``technical_atr_pct`` only, which the score stage
derives from ``(ticker, asof)`` OHLCV — it carries NO theme input. So for a ticker
that hits several themes, every theme-row gets the SAME penalty, and subtracting a
per-ticker constant cannot reorder that ticker's theme-rows. That is the invariant
the EDGE test-set safety rests on: ``_sort_and_dedup_for_brief``'s
``drop_duplicates(keep="first")`` still keeps the same row, so monitored outcomes
are byte-identical pre/post. A future theme-dependent term folded in here WOULD
break it — keep selection_score per-ticker-constant in its non-layer4 part.

Breakpoints are FROZEN from the historical signal panel (N=328, 2026-06-25):
p67=5.77 (top-tercile onset), p90=8.37 (deep loser tail, car_5 −7.3%). PROVISIONAL
— a single 4-week in-sample window. Recalibrating bumps ``SCORER_CONFIG_VERSION``,
which partitions the EDGE cohort (old briefs stay a frozen pool).

Design: docs/superpowers/specs/2026-06-25-selection-score-atr-tilt-design.md
"""

from __future__ import annotations

import math

ATR_RAMP_LO = 5.77  # panel p67 — penalty onset; low+mid terciles untouched
ATR_RAMP_HI = 8.37  # panel p90 — full penalty in the loser tail
LAMBDA = 1.0  # full penalty = one layer4 point at/above p90

# Poolability key. Encodes the scorer identity + every parameter that changes the
# ordering, so two outcomes are only pooled when produced by the same policy.
SCORER_CONFIG_VERSION = "scorer-v1-atrtilt-lam1.0-lo5.77-hi8.37"


def atr_penalty(atr_pct: float | None) -> float:
    """Ramp penalty in ``[0, LAMBDA]``: 0 at/below p67, full at/above p90, linear
    between. Missing / NaN / non-positive ATR → 0 (never punish unknown ATR)."""
    if atr_pct is None:
        return 0.0
    try:
        x = float(atr_pct)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(x) or x <= ATR_RAMP_LO:
        return 0.0
    if x >= ATR_RAMP_HI:
        return LAMBDA
    return LAMBDA * (x - ATR_RAMP_LO) / (ATR_RAMP_HI - ATR_RAMP_LO)


def selection_score(layer4_weighted_score: float, atr_pct: float | None) -> float:
    """Primary brief sort key: layer4 minus the per-ticker ATR penalty."""
    return float(layer4_weighted_score) - atr_penalty(atr_pct)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m unittest apps.alphalens-research.tests.thematic.screening.test_selection_score -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/thematic/screening/selection_score.py \
        apps/alphalens-research/tests/thematic/screening/test_selection_score.py
git commit -m "feat(thematic): add ATR-penalty selection_score module"
```

---

### Task 1.2: Stamp the three columns in the score stage

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/thematic/screening/scorer.py`
- Test: `apps/alphalens-research/tests/thematic/screening/test_scorer.py`

**Interfaces:**
- Consumes: `selection_score.atr_penalty`, `selection_score.selection_score`, `selection_score.SCORER_CONFIG_VERSION` (Task 1.1).
- Produces: three new columns on the `score_candidates()` output frame and the written parquet — `scorer_config_version: str`, `atr_penalty: float`, `selection_score: float`.

- [ ] **Step 1: Write the failing tests**

Add to `test_scorer.py` (alongside the existing `insider_signal_version` assertion ~line 289). Use the same fixture the existing scorer tests build; if they call a helper like `_run_scorer(...)` returning the frame, reuse it.

```python
def test_stamps_scorer_config_version_on_every_row(self):
    out = self._run_scorer()  # reuse the existing helper used by the insider_signal_version test
    self.assertIn("scorer_config_version", out.columns)
    self.assertTrue((out["scorer_config_version"] == "scorer-v1-atrtilt-lam1.0-lo5.77-hi8.37").all())

def test_stamps_selection_score_equals_layer4_minus_atr_penalty(self):
    out = self._run_scorer()
    for _, row in out.iterrows():
        expected = float(row["layer4_weighted_score"]) - row["atr_penalty"]
        self.assertAlmostEqual(row["selection_score"], expected, places=9)

def test_atr_penalty_zero_when_atr_below_onset(self):
    out = self._run_scorer()
    calm = out[out["technical_atr_pct"] <= 5.77]
    self.assertTrue((calm["atr_penalty"] == 0.0).all())

def test_version_survives_written_parquet(self):
    import tempfile, pathlib
    import pandas as pd
    out = self._run_scorer()
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "scored.parquet"
        out.to_parquet(p, index=False)
        back = pd.read_parquet(p)
    self.assertIn("scorer_config_version", back.columns)
    self.assertIn("selection_score", back.columns)
    self.assertTrue((back["scorer_config_version"] != "").all())
```

If `test_scorer.py` has no shared `_run_scorer` helper, extract one from the existing `insider_signal_version` test body (same inputs) so these four tests reuse it — DRY.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m unittest apps.alphalens-research.tests.thematic.screening.test_scorer -v -k version or selection or atr_penalty`
Expected: FAIL — columns absent.

- [ ] **Step 3: Implement the stamps**

(a) In `_build_candidate_row` (scorer.py), right after the `"insider_signal_version": insider_signal.INSIDER_SIGNAL_VERSION,` line (~368), add:

```python
        # Poolability key for the ordering policy (mirrors insider_signal_version).
        # Stamped on EVERY row so the deferred selection×EDGE attribution partitions
        # old vs new ordering and never pools across versions.
        "scorer_config_version": selection_score_mod.SCORER_CONFIG_VERSION,
```

(b) Add the import at the top of `scorer.py` (with the other `from ... import` lines):

```python
from alphalens_pipeline.thematic.screening import selection_score as selection_score_mod
```

(c) Right after the `enrichment["layer4_weighted_score"] = [...]` list-comprehension block and its `enrichment.drop(columns=["_fcff_positive", "_technicals_positive"])` (~line 600), add:

```python
    # ATR-soft-tilt: penalize high-ATR (extended/volatile) names so they sort
    # lower in the brief. Per-ticker constant (no theme input) — see selection_score.
    enrichment["atr_penalty"] = [
        selection_score_mod.atr_penalty(a) for a in enrichment["technical_atr_pct"]
    ]
    enrichment["selection_score"] = (
        enrichment["layer4_weighted_score"].astype(float) - enrichment["atr_penalty"]
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run python -m unittest apps.alphalens-research.tests.thematic.screening.test_scorer -v`
Expected: PASS (existing + 4 new).

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/thematic/screening/scorer.py \
        apps/alphalens-research/tests/thematic/screening/test_scorer.py
git commit -m "feat(thematic): stamp selection_score, atr_penalty, scorer_config_version"
```

---

### Task 1.3: Make `selection_score` the primary brief sort key + lockstep tests

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/thematic/argumentation/orchestrator.py`
- Test: `apps/alphalens-research/tests/thematic/argumentation/test_sort_and_dedup.py`

**Interfaces:**
- Consumes: the `selection_score` column produced by Task 1.2 (read as a sort column; the sort does NOT recompute the penalty).
- Produces: `_BRIEF_SORT_KEYS[0] == ("selection_score", False, 0.0)`.

- [ ] **Step 1: Update the test `_row` default + write the failing demotion test**

In `test_sort_and_dedup.py`, change `_row()` so a row carries a `selection_score` mirroring `layer4` unless overridden (keeps every existing layer4-primary test valid, since `selection_score == layer4` there):

```python
    base.update(overrides)
    if "selection_score" not in overrides:
        base["selection_score"] = float(base["layer4_weighted_score"])
    return base
```

Rename `test_primary_sort_is_layer4_weighted_score_desc` → `test_primary_sort_is_selection_score_desc` (body unchanged — with the `_row` default those rows have `selection_score == layer4`, so `["HIGH","MID","LOW"]` still holds). Add a NEW test proving the tilt demotes an equal-`layer4` high-ATR name:

```python
    def test_high_atr_name_demoted_below_equal_layer4_calm_name(self):
        # Equal layer4; the high-ATR name carries a lower selection_score and must
        # sort BELOW the calm one — the whole point of the ATR tilt.
        df = pd.DataFrame(
            [
                _row(ticker="VOLATILE", layer4_weighted_score=4, selection_score=3.0),
                _row(ticker="CALM", layer4_weighted_score=4, selection_score=4.0),
            ]
        )
        out = orchestrator._sort_and_dedup_for_brief(df)
        self.assertEqual(list(out["ticker"]), ["CALM", "VOLATILE"])
```

- [ ] **Step 2: Update the two sort-lock guards to the new chain**

In `_NON_EXPERT_SORT_ALLOWLIST` add `"selection_score",` as the first entry. In `test_sort_chain_is_exactly_the_documented_set`, prepend `"selection_score",` as the first element of the expected ordered tuple. (`selection_score` carries no `buffett_`/`oneil_`/`expert_` prefix, so `test_no_expert_prefixed_key_in_sort_chain` and the prefix guards stay green untouched.)

- [ ] **Step 3: Run to verify the demotion test + guards fail**

Run: `cd apps/alphalens-research && uv run python -m unittest discover -s tests/thematic/argumentation -t . -v`
Expected: FAIL — `test_high_atr_name_demoted...` (sort still layer4-primary, tie falls to catalyst → order is input order `["VOLATILE","CALM"]`); allowlist/equality-pin tests fail until the chain changes.

- [ ] **Step 4: Prepend the sort key in the orchestrator**

In `orchestrator.py`, change `_BRIEF_SORT_KEYS` to prepend `selection_score` and update the comment:

```python
# Primary on selection_score (= layer4_weighted_score − ATR penalty; see
# screening/selection_score.py). Penalty is 0 for ~2/3 of names (ATR ≤ p67), so
# the continuous primary key ties for most rows and the layer4 → catalyst → …
# chain below stays load-bearing; it only separates the top ATR tercile. All
# keys DESC except magic_formula_rank. Neutral defaults backfill missing columns.
_BRIEF_SORT_KEYS: tuple[tuple[str, bool, float | int | bool], ...] = (
    ("selection_score", False, 0.0),
    ("layer4_weighted_score", False, 0.0),
    ("catalyst_strength", False, 0.0),
    ("insider_score_usd", False, 0.0),
    ("deep_drawdown_reversal", False, False),
    ("magic_formula_rank", True, float("inf")),
    ("n_gates_passed", False, 0),
    ("llm_confidence", False, 0.0),
    ("_template_facts_richness", False, 0),
)
```

(Default `0.0` means an old parquet with no `selection_score` column gets a constant key → ties → `layer4_weighted_score` decides → identical to pre-tilt behavior. Fresh briefs always carry the column.)

- [ ] **Step 5: Run the full argumentation suite to verify green**

Run: `cd apps/alphalens-research && uv run python -m unittest discover -s tests/thematic/argumentation -t . -v`
Expected: PASS (existing + demotion + updated guards).

- [ ] **Step 6: Run the whole research suite (catch the worktree-masks-CI pattern)**

Run: `cd apps/alphalens-research && uv run python -m unittest discover -s tests -t . -v 2>&1 | tail -20`
Expected: PASS / OK.

- [ ] **Step 7: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/thematic/argumentation/orchestrator.py \
        apps/alphalens-research/tests/thematic/argumentation/test_sort_and_dedup.py
git commit -m "feat(thematic): make selection_score the primary brief sort key"
```

---

### Task 1.4: Survivor-invariance regression test (drop_duplicates guard)

**Files:**
- Test: `apps/alphalens-research/tests/thematic/argumentation/test_sort_and_dedup.py`

- [ ] **Step 1: Write the test**

```python
    def test_atr_tilt_does_not_change_multi_theme_dedup_survivor(self):
        # The test-set-safety invariant: atr_penalty is a per-ticker constant, so a
        # ticker's theme-rows keep the SAME within-ticker order under selection_score
        # → drop_duplicates(keep="first") keeps the same theme-row as a layer4-primary
        # sort would. Here ABC hits two themes with different catalyst-driven layer4;
        # both rows share one ATR-driven selection_score offset.
        rows = [
            _row(ticker="ABC", theme="ai", layer4_weighted_score=5,
                 catalyst_strength=0.9, selection_score=4.0),
            _row(ticker="ABC", theme="space", layer4_weighted_score=3,
                 catalyst_strength=0.2, selection_score=2.0),
        ]
        out = orchestrator._sort_and_dedup_for_brief(pd.DataFrame(rows))
        survivor = out[out["ticker"] == "ABC"].iloc[0]
        self.assertEqual(survivor["theme"], "ai")  # the strong-catalyst row, as before
        self.assertEqual(sorted(survivor["also_in_themes"]), ["space"])
```

- [ ] **Step 2: Run to verify it passes** (it should pass immediately — guards the invariant)

Run: `cd apps/alphalens-research && uv run python -m unittest discover -s tests/thematic/argumentation -t . -v -k survivor`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add apps/alphalens-research/tests/thematic/argumentation/test_sort_and_dedup.py
git commit -m "test(thematic): pin drop_duplicates survivor-invariance under ATR tilt"
```

- [ ] **Step 4: Push, open PR, zen review, merge**

```bash
git push -u origin feature/selection-score-atr-tilt-pipeline
gh pr create --repo kamilpajak/AlphaLens --title "feat(thematic): ATR-soft-tilt selection_score (pipeline)" \
  --body "$(cat <<'EOF'
- New `selection_score = layer4 − atr_penalty(technical_atr_pct)`; primary brief sort key.
- `scorer_config_version` poolability key stamped on every row.
- ATR penalty ramps p67→p90 (frozen panel breakpoints, λ=1), 0 below p67, NaN→0.

## Behaviour notes
- Pure ordering change; selection funnel + monitored EDGE population untouched (test set byte-identical). `atr_penalty` is a per-ticker constant → drop_duplicates survivor invariant (regression-tested).
- Breakpoints PROVISIONAL (single 4-week in-sample window); a bump resets the cohort.
- `atr_penalty` column doubles as the pure-ATR ordering signal for the forward A/B (spec §8).

Links: spec docs/superpowers/specs/2026-06-25-selection-score-atr-tilt-design.md

## Test plan
- [ ] selection_score module units
- [ ] scorer stamps + written-parquet survival
- [ ] sort demotion + survivor-invariance + updated sort-lock guards
EOF
)"
# then: mcp__zen__codereview deepseek/deepseek-v4-pro thinking=high → fix as new commits → CI green → merge
```

---

# PR 2 — Django (briefs app): persist + serve the three columns

**Branch:** `feature/selection-score-atr-tilt-django`

## File structure (PR 2)

- Modify: `apps/alphalens-django/briefs/models.py` — add 3 fields to the `Brief` model (~after line 79/128).
- Create: `apps/alphalens-django/briefs/migrations/0014_brief_selection_score.py` — schema migration.
- Modify: `apps/alphalens-django/briefs/api/serializers.py` — expose the 3 fields (detail serializer for the drawer; `selection_score`/`atr_penalty` on the list serializer for the card band).
- Modify: `apps/alphalens-django/briefs/tests/test_schema_parity.py` — add the 3 names to `LEGACY_CONTRACT_COLUMNS` (~line 25).
- Modify/inspect: `apps/alphalens-django/briefs/<ingest>.py` — confirm field-name-driven ingest maps the new parquet columns (add mapping only if ingest is explicit, not reflective).

**Decision (locked):** the API display order (`_BRIEF_DISPLAY_ORDER`, `views.py:88`, already `rank_in_day` primary) is UNCHANGED — `rank_in_day` is stamped from the new sort. The `min_score` filter STAYS on `layer4_weighted_score__gte` (the tilt reorders, never hides).

---

### Task 2.1: Model fields + migration

**Files:**
- Modify: `apps/alphalens-django/briefs/models.py`
- Create: `apps/alphalens-django/briefs/migrations/0014_brief_selection_score.py`
- Test: `apps/alphalens-django/briefs/tests/test_models.py` (or the existing model test module)

**Interfaces:**
- Produces: `Brief.selection_score: float` (null=True), `Brief.atr_penalty: float` (null=True), `Brief.scorer_config_version: str` (default "").

- [ ] **Step 1: Write the failing test**

```python
# in briefs/tests/test_models.py (or nearest model test module)
def test_brief_has_selection_score_fields(self):
    from briefs.models import Brief
    f = {x.name for x in Brief._meta.get_fields()}
    self.assertIn("selection_score", f)
    self.assertIn("atr_penalty", f)
    self.assertIn("scorer_config_version", f)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/alphalens-django && uv run python manage.py test briefs.tests.test_models -v 2`
Expected: FAIL — fields absent.

- [ ] **Step 3: Add the fields**

In `briefs/models.py`, next to `layer4_weighted_score = models.IntegerField(default=0)` (~128) and `insider_signal_version` (~79):

```python
    selection_score = models.FloatField(null=True, blank=True)
    atr_penalty = models.FloatField(null=True, blank=True)
    scorer_config_version = models.CharField(max_length=128, blank=True, default="")
```

- [ ] **Step 4: Generate the migration**

Run: `cd apps/alphalens-django && uv run python manage.py makemigrations briefs -n brief_selection_score`
Expected: creates `0014_brief_selection_score.py` adding the three fields.

- [ ] **Step 5: Run to verify it passes**

Run: `cd apps/alphalens-django && uv run python manage.py test briefs.tests.test_models -v 2`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/alphalens-django/briefs/models.py apps/alphalens-django/briefs/migrations/0014_brief_selection_score.py \
        apps/alphalens-django/briefs/tests/test_models.py
git commit -m "feat(briefs): add selection_score, atr_penalty, scorer_config_version fields"
```

---

### Task 2.2: Schema-parity contract + serializer

**Files:**
- Modify: `apps/alphalens-django/briefs/tests/test_schema_parity.py`
- Modify: `apps/alphalens-django/briefs/api/serializers.py`
- Test: `apps/alphalens-django/briefs/tests/test_api.py` (or nearest serializer/api test)

**Interfaces:**
- Produces: the candidate detail JSON includes `selection_score`, `atr_penalty`, `scorer_config_version`; the list JSON includes `selection_score`, `atr_penalty` (for the card band).

- [ ] **Step 1: Write the failing tests**

```python
# briefs/tests/test_api.py — extend an existing candidate-detail test or add:
def test_detail_serializes_selection_fields(self):
    data = self._get_candidate_detail_json()  # reuse the existing helper
    self.assertIn("selection_score", data)
    self.assertIn("atr_penalty", data)
    self.assertIn("scorer_config_version", data)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/alphalens-django && uv run python manage.py test briefs.tests.test_api -v 2`
Expected: FAIL — keys absent; also `test_schema_parity` now FAILS (model fields not in `LEGACY_CONTRACT_COLUMNS`).

- [ ] **Step 3: Register the contract columns + expose in serializers**

(a) In `test_schema_parity.py`, add to `LEGACY_CONTRACT_COLUMNS`:

```python
    "selection_score",
    "atr_penalty",
    "scorer_config_version",
```

(b) In `serializers.py`, add the three to `CandidateDetailSerializer.fields` (the drawer) and `selection_score` + `atr_penalty` to `CandidateSerializer.fields` (the card band). If `fields` is a tuple, append the names; if `fields = "__all__"`, no change is needed beyond the model.

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/alphalens-django && uv run python manage.py test briefs -v 2 2>&1 | tail -15`
Expected: PASS (api + schema_parity).

- [ ] **Step 5: Regenerate OpenAPI if the repo pins a generated schema**

Run: `cd apps/alphalens-django && uv run python manage.py spectacular --file ../web/openapi.json 2>/dev/null || true` (only if an `openapi.*` artifact is tracked; otherwise skip — PR 3 regenerates client types).

- [ ] **Step 6: Commit**

```bash
git add apps/alphalens-django/briefs/api/serializers.py apps/alphalens-django/briefs/tests/test_schema_parity.py \
        apps/alphalens-django/briefs/tests/test_api.py
git commit -m "feat(briefs): serve selection_score/atr_penalty/scorer_config_version"
```

- [ ] **Step 7: Push, open PR, zen review, merge** (same workflow as PR 1; body notes: forward-only, `min_score` stays on layer4, display order unchanged via rank_in_day).

---

# PR 3 — SPA: brief-card band + breakdown drawer + version chips

**Branch:** `feature/selection-score-atr-tilt-web`

## File structure (PR 3)

- Modify: `apps/web/src/lib/types.ts` — add 3 fields to the candidate type (~line 73).
- Regenerate: `apps/web/src/lib/api-types.gen.ts` — via the repo's codegen.
- Modify: `apps/web/src/lib/components/CandidateCard.svelte` — `extended` band (~line 199 area) + breakdown lines in the deep-read drawer.
- Modify: `apps/web/src/routes/+page.svelte` — label the top-picks panel "top 8 of N".
- Modify: `apps/web/src/routes/edge/+page.svelte` — show the `scorer_config_version` chip (if the field is carried to the edge payload; else defer to a follow-up — see note).
- Test: nearest component test / Storybook story for `CandidateCard`.

---

### Task 3.1: Types + generated client

- [ ] **Step 1:** Add to the candidate type in `types.ts` (near `layer4_weighted_score`):

```typescript
	selection_score: number | null;
	atr_penalty: number | null;
	scorer_config_version: string | null;
```

- [ ] **Step 2:** Regenerate the API client types.

Run: `cd apps/web && pnpm run gen:api 2>/dev/null || pnpm openapi-typescript ../alphalens-django/openapi.json -o src/lib/api-types.gen.ts` (use the repo's actual script name from `apps/web/package.json`).
Expected: `selection_score?`, `atr_penalty?`, `scorer_config_version?` appear in `api-types.gen.ts`.

- [ ] **Step 3: Commit**

```bash
cd ../.. && git add apps/web/src/lib/types.ts apps/web/src/lib/api-types.gen.ts
git commit -m "feat(web): type selection_score/atr_penalty/scorer_config_version"
```

---

### Task 3.2: `extended` band + breakdown drawer on the card

**Files:**
- Modify: `apps/web/src/lib/components/CandidateCard.svelte`
- Test: the card's component test / story (e.g. `*.stories.svelte` per the Storybook catalog).

- [ ] **Step 1: Write the failing component test / story assertion**

Add a story/test rendering a candidate with `selection_score: 3.0, atr_penalty: 1.0, technical_atr_pct: 9.0` and assert the `extended` chip renders; and one with `atr_penalty: 0.0` asserting it does NOT. (Match the existing card test harness — `@testing-library/svelte` or the `.stories.svelte` pattern from the tooltip catalog.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/web && pnpm test 2>&1 | tail -20` (or `pnpm test:unit`)
Expected: FAIL — no `extended` chip.

- [ ] **Step 3: Implement**

(a) Coarse on-card band — near the `layer4_weighted_score` render (~line 199). Tone-neutral, no precise number on the card:

```svelte
{#if (c.atr_penalty ?? 0) > 0}
	<span class="whitespace-nowrap rounded px-1.5 py-0.5 text-[10px] uppercase tracking-widest
	             bg-fg-muted/10 text-fg-muted" title="High realized volatility / extended at entry — deprioritized (suggestive, not yet validated)">
		extended
	</span>
{/if}
```

(b) Deep-read drawer breakdown (precise number + version live HERE, behind the not-validated label). In the drawer section:

```svelte
<div class="text-[11px] text-fg-muted">
	<div class="flex justify-between"><span>layer4</span><span class="whitespace-nowrap">{c.layer4_weighted_score ?? '—'}</span></div>
	<div class="flex justify-between"><span>− ATR penalty</span><span class="whitespace-nowrap">−{(c.atr_penalty ?? 0).toFixed(2)}</span></div>
	<div class="flex justify-between font-semibold"><span>selection_score</span><span class="whitespace-nowrap">{(c.selection_score ?? c.layer4_weighted_score ?? 0).toFixed(2)}</span></div>
	{#if c.scorer_config_version}
		<div class="mt-1 text-[10px] opacity-70 whitespace-nowrap">{c.scorer_config_version}</div>
	{/if}
	<div class="mt-1 text-[10px] italic opacity-70">suggestive — not yet validated</div>
</div>
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/web && pnpm test 2>&1 | tail -20`
Expected: PASS.

- [ ] **Step 5: Lint + svelte-check**

Run: `cd apps/web && pnpm run check && pnpm run lint`
Expected: no new errors (verify `whitespace-nowrap` on the version string + numeric tokens).

- [ ] **Step 6: Commit**

```bash
cd ../.. && git add apps/web/src/lib/components/CandidateCard.svelte apps/web/src/lib
git commit -m "feat(web): extended band + selection_score breakdown on candidate card"
```

---

### Task 3.3: Homepage "top 8 of N" + `/edge` version chip

**Files:**
- Modify: `apps/web/src/routes/+page.svelte`
- Modify: `apps/web/src/routes/edge/+page.svelte`

- [ ] **Step 1:** In `+page.svelte`, label the top-picks panel. Near the `topCandidates` heading (the "top picks" panel, ~line 186-198), show the count:

```svelte
<span class="text-fg-muted">top 8 of {data.latestBrief?.n_candidates ?? 0}</span>
```

- [ ] **Step 2:** `/edge` version chip — only if the edge payload carries `scorer_config_version`. The `/edge` app ingests `population_ladders` (no brief columns), so check whether `LadderOutcome`/the edge serializer exposes it.

  - If yes: render a small chip with `o.scorer_config_version` next to the candidate row.
  - **If not (likely):** the chip needs the column carried through the monitor/ladder store or a `Brief ⋈ LadderOutcome` lookup — that is a separate, larger change. **Defer it:** add a one-line `// TODO(follow-up): carry scorer_config_version to edge payload` is NOT allowed (no placeholders) — instead, record the deferral in the PR body "Known issues" section and DO NOT add dead UI. Ship the homepage label only in this task.

- [ ] **Step 3:** Run web build + tests.

Run: `cd apps/web && pnpm build && pnpm test 2>&1 | tail -15`
Expected: build OK, tests PASS.

- [ ] **Step 4: Commit**

```bash
cd ../.. && git add apps/web/src/routes/+page.svelte
git commit -m "feat(web): label homepage top-picks as 'top 8 of N'"
```

- [ ] **Step 5: Push, open PR, zen review, merge.** PR body "Known issues": `/edge` `scorer_config_version` chip deferred — needs the column carried from briefs to the ladder/edge payload (`Brief ⋈ LadderOutcome` on `(brief_date, ticker)`); tracked for a follow-up. Note forward-only: columns populate from the next VPS thematic-build image rebuild; older briefs stay pre-tilt (old cohort).

---

## Post-merge (operator-owned, not in this plan)

- VPS: rebuild `alphalens-pipeline:latest` image (bakes the scorer change) + `docker compose pull && up -d` for Django (runs migration 0014) + `rebuild_briefs_cache --force`. New columns populate forward only.
- Validation (spec §8): after **≥30 distinct matured brief-dates** on the new `scorer_config_version` cohort (~early-Aug), re-run the signal-attribution workflow partitioned by version; forward-A/B `selection_score` (blend) vs `atr_penalty` (pure-ATR) incremental rank-IC over a `layer4`-only baseline; SUCCESS = high-ATR tercile separates (CI excludes 0), KILL = revert `_BRIEF_SORT_KEYS` to layer4-primary + keep ATR as a display band.

## Self-review notes (spec coverage)

- Spec §4 selection_score + ramp + λ + NaN→0 → Tasks 1.1, 1.2. §4a pure-ATR column → `atr_penalty` doubles as it (Task 1.2). §5 version + one must-not-drop hop + written-parquet test → Tasks 1.2 (test_version_survives_written_parquet). §3/§9.1 sort-lock lockstep + survivor invariant → Tasks 1.3, 1.4. §6 (no shadow columns) → honored (none added). §7 brief-card band + drawer + homepage "8 of N" + /edge chip → Tasks 3.2, 3.3 (chip deferred w/ Known-issues note). §2 min_score stays layer4 + no order_by change → Task 2 decision block. §8 validation → Post-merge. **Deliberate scope trim vs spec §7:** the drawer shows `layer4 → −penalty → selection_score` (not a 4-way decomposition of layer4 into fcff/value/technicals/catalyst), because those components are dropped in scorer.py (`_fcff_positive`/`_technicals_positive`) and persisting them is unbudgeted YAGNI; the underlying per-signal fields are already on the card. Flag for the user.
