# `scorer_config_version` on /edge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the scorer poolability key `scorer_config_version` per row on the `/edge` dashboard, by stamping it onto each ladder-outcome row (mirroring how `theme` already flows brief → ladder → edge API → SPA).

**Architecture:** Brief-provenance column carried by stamping at parquet-write time (NOT a query-time join). `CandidateBrief` gains the field (read from the brief parquet); the population-ladder monitor stamps it onto every outcome row alongside `theme`; the Django `LadderOutcome` model gains a CharField (field-name-driven auto-ingest); the edge API row dict + serializer expose it explicitly; the SPA renders a small muted per-row chip. Forward-only.

**Tech Stack:** Python/pandas (pipeline), Django REST Framework + Postgres (edge API), SvelteKit 5 + Tailwind (web). uv workspace, pnpm web. Django tests = pytest.

**Spec:** `docs/superpowers/specs/2026-06-25-edge-scorer-config-version-chip-design.md`

## Global Constraints

- **TDD always** — red → green → refactor; test first.
- **Worktree** — implement in a `git worktree` off fresh `origin/main` (NOT the main checkout); pipeline/research package edits need their own `uv sync` in the worktree.
- **One PR**, branch `feature/edge-scorer-config-version-chip`; **zen pre-merge** (`deepseek/deepseek-v4-pro` thinking=high; fallback `gemini-3.1-pro-preview` on OpenRouter empty/outage) → apply findings as commits → CI green → merge.
- **DCO sign-off MANDATORY** on every commit: `git commit --signoff` (CI enforces; the trailer must read `Signed-off-by: Kamil Pająk <kamilpajak@users.noreply.github.com>`, which the git config already produces). If you forget, fix the whole branch with `git rebase --signoff <merge-base>` + `--force-with-lease` (allowed pre-review-comments).
- **English-only** in code (no Polish chars). **Conventional Commits**, no AI mention.
- **Web atomic-token rule** — the version string token carries Tailwind `whitespace-nowrap` (a `-`-laden version like `scorer-v1-atrtilt-...` must not break mid-string).
- **SonarCloud** — `math.isnan`/`pd.isna` not `x!=x`; no float `==` on computed reals.
- **Forward-only / no backfill** — older outcomes show blank; that is correct.
- Pre-commit `ruff-format` may reformat + abort the commit → re-`git add` + re-commit.

## File structure

- Modify: `apps/alphalens-pipeline/alphalens_pipeline/paper/brief_loader.py` — `CandidateBrief` dataclass + loader read the new field.
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/feedback/population_ladder_monitor.py` — stamp the version onto outcome rows + carry-forward back-fill.
- Modify: `apps/alphalens-django/edge/models.py` + new `edge/migrations/00NN_*.py` — `LadderOutcome.scorer_config_version`.
- Modify: `apps/alphalens-django/edge/api/serializers.py` + `edge/api/views.py` — expose it on `/v1/edge/outcomes`.
- Modify: `apps/web/src/lib/types.ts` (`EdgeOutcome`) + `apps/web/src/routes/edge/+page.svelte` — render the chip.
- Tests: `apps/alphalens-research/tests/paper/test_brief_loader.py`, `apps/alphalens-research/tests/test_population_ladder_monitor.py`, `apps/alphalens-django/edge/tests/` (model+ingest+api), `apps/web` component test.

---

### Task 1: `CandidateBrief` carries `scorer_config_version`

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/paper/brief_loader.py` (dataclass ~28-44; loader `CandidateBrief(...)` ~98-108)
- Test: `apps/alphalens-research/tests/paper/test_brief_loader.py`

**Interfaces:**
- Produces: `CandidateBrief.scorer_config_version: str` (defaults `""` when the brief parquet omits the column).

- [ ] **Step 1: Write the failing test**

Add to `test_brief_loader.py` (mirror an existing loader test that builds a one-row brief DataFrame/parquet and calls the row→CandidateBrief decoder; reuse that helper). Two cases:

```python
def test_candidate_brief_carries_scorer_config_version(self):
    cb = self._decode_row({"ticker": "NVDA", "theme": "ai",
                           "scorer_config_version": "scorer-v1-atrtilt-lam1.0-lo5.77-hi8.37"})
    assert cb.scorer_config_version == "scorer-v1-atrtilt-lam1.0-lo5.77-hi8.37"

def test_candidate_brief_scorer_config_version_defaults_blank(self):
    cb = self._decode_row({"ticker": "NVDA", "theme": "ai"})  # pre-tilt brief, no column
    assert cb.scorer_config_version == ""
```

If `test_brief_loader.py` has no reusable single-row decode helper, call the module's public loader the way the existing tests do (write a parquet via the existing fixture helper, then `load_brief`), and assert on the returned `CandidateBrief`.

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/alphalens-research && uv run python -m unittest discover -s tests/paper -t . -k scorer_config_version -v`
Expected: FAIL — `CandidateBrief` has no attribute `scorer_config_version`.

- [ ] **Step 3: Add the field + loader read**

In `brief_loader.py`, add to the `CandidateBrief` dataclass (after `layer4_weighted_score: float | None`):

```python
    scorer_config_version: str
```

In the `CandidateBrief(...)` constructor in the row decoder (after `layer4_weighted_score=...`):

```python
        scorer_config_version=str(row.get("scorer_config_version", "") or ""),
```

(`row.get(..., "")` handles a missing column; `or ""` coerces a NaN/None cell to `""`.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/alphalens-research && uv run python -m unittest discover -s tests/paper -t . -k scorer_config_version -v`
Expected: PASS (2 tests). Then the whole brief_loader module: `... -k test_brief_loader` stays green.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/paper/brief_loader.py \
        apps/alphalens-research/tests/paper/test_brief_loader.py
git commit --signoff -m "feat(paper): CandidateBrief carries scorer_config_version"
```

---

### Task 2: Stamp `scorer_config_version` onto ladder-outcome rows

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/feedback/population_ladder_monitor.py`
- Test: `apps/alphalens-research/tests/test_population_ladder_monitor.py`

**Interfaces:**
- Consumes: `CandidateBrief.scorer_config_version` (Task 1).
- Produces: every outcome row dict carries a `scorer_config_version` key (the brief's value, or `None` when blank/carried-forward).

**Context:** `theme` is sourced once per candidate (`theme = c.theme or None`, ~1558; batch path `theme = item.candidate.theme`, ~1873) and stamped via `_stamp_theme(row, theme)` at ~6 return sites. `scorer_config_version` is brief-provenance like `theme`, so it travels the same way. `_carry_prior` (~930) back-fills new columns from column-group tuples; `_CONFIG_COLUMNS = ("ladder_config_version",)` (~186).

- [ ] **Step 1: Write the failing test**

In `test_population_ladder_monitor.py`, mirror an existing test that screens one candidate to a row and asserts a stamped column (e.g. how `theme` is asserted). Add:

```python
def test_outcome_row_carries_scorer_config_version_from_brief(self):
    # build a CandidateBrief with scorer_config_version (use the test's existing
    # candidate-builder helper; set scorer_config_version="scorer-v1-test")
    row = self._screen_one(scorer_config_version="scorer-v1-test")  # reuse existing screen helper
    assert row["scorer_config_version"] == "scorer-v1-test"

def test_carried_forward_row_backfills_scorer_config_version(self):
    # a prior row lacking the column, carried forward, must back-fill to None (schema-stable)
    carried = monitor._carry_prior({"ticker": "X"})
    assert carried["scorer_config_version"] is None
```

Adapt `self._screen_one(...)` / candidate-builder to whatever the file already uses; the key assertions are (a) the stamped value on a screened row and (b) `_carry_prior` back-fills the key.

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/alphalens-research && uv run python -m unittest discover -s tests -t . -k scorer_config_version -v`
Expected: FAIL — key absent on the row / `_carry_prior` does not set it.

- [ ] **Step 3: Implement the stamp**

(a) Add a sibling stamp helper next to `_stamp_theme` (keep `_stamp_theme`'s slug logic isolated):

```python
def _stamp_scorer_version(row: dict[str, Any], scorer_config_version: str | None) -> dict[str, Any]:
    """Stamp the scorer poolability key onto a store row (brief provenance, like theme).

    Travels WITH the outcome record rather than re-joined downstream from the mutable
    briefs cache. ``None`` for an empty value so the read side renders an em dash.
    """
    row["scorer_config_version"] = scorer_config_version or None
    return row
```

(b) Add a provenance column-group + carry it forward. Near `_CONFIG_COLUMNS` (~186):

```python
_PROVENANCE_COLUMNS = ("scorer_config_version",)
```

and in `_carry_prior` (~941) add `*_PROVENANCE_COLUMNS,` to the back-fill tuple list, and in any other place that lists `*_CONFIG_COLUMNS` for schema stability (e.g. ~1725) add `*_PROVENANCE_COLUMNS,` alongside.

(c) Thread the value from the candidate and stamp it at every site that stamps theme. In `_screen_candidate` (~1558) after `theme = c.theme or None` add `scorer_version = c.scorer_config_version or None`; at each `_stamp_theme(row, theme)` return, wrap: `_stamp_scorer_version(_stamp_theme(row, theme), scorer_version)`. In the batch path (~1873) after `theme = item.candidate.theme or None` add `scorer_version = item.candidate.scorer_config_version or None` and wrap the same way at its `_stamp_theme(...)` sites (~1890, ~1910). (Grep `_stamp_theme(` to find all sites — there are ~6; every one gets wrapped.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/alphalens-research && uv run python -m unittest discover -s tests -t . -k scorer_config_version -v`
Expected: PASS. Then the whole monitor module: `... -k test_population_ladder_monitor` green.

- [ ] **Step 5: Run the full research suite (catch cross-module breakage)**

Run: `cd apps/alphalens-research && uv run python -m unittest discover -s tests -t . 2>&1 | tail -8`
Expected: OK (no failures). (Brief-loader golden / store-schema tests must stay green; if a store-schema snapshot pins columns, add `scorer_config_version` there too.)

- [ ] **Step 6: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/feedback/population_ladder_monitor.py \
        apps/alphalens-research/tests/test_population_ladder_monitor.py
git commit --signoff -m "feat(feedback): stamp scorer_config_version onto ladder-outcome rows"
```

---

### Task 3: Django `LadderOutcome.scorer_config_version` + migration

**Files:**
- Modify: `apps/alphalens-django/edge/models.py` (`LadderOutcome`, near `ladder_config_version` ~100)
- Create: `apps/alphalens-django/edge/migrations/00NN_ladderoutcome_scorer_config_version.py`
- Test: `apps/alphalens-django/edge/tests/test_ingest.py` (+ a model field assertion)

**Interfaces:**
- Produces: `LadderOutcome.scorer_config_version: str` (CharField, blank, default `""`); auto-ingested (field-name-driven).

- [ ] **Step 1: Write the failing tests**

In `edge/tests/test_ingest.py` (mirror the existing theme/ladder ingest test that writes a population_ladders parquet and runs the edge ingest):

```python
def test_ingest_carries_scorer_config_version(self):
    # write a ladder parquet row including scorer_config_version, run edge ingest
    self._ingest_one({"brief_date": "2026-06-25", "ticker": "NVDA",
                      "scorer_config_version": "scorer-v1-test"})  # reuse existing ingest helper
    from edge.models import LadderOutcome
    o = LadderOutcome.objects.get(ticker="NVDA")
    assert o.scorer_config_version == "scorer-v1-test"

def test_ingest_missing_scorer_config_version_defaults_blank(self):
    self._ingest_one({"brief_date": "2026-06-25", "ticker": "OLD"})  # no column
    from edge.models import LadderOutcome
    assert LadderOutcome.objects.get(ticker="OLD").scorer_config_version == ""
```

Adapt `self._ingest_one(...)` to the file's existing parquet-write + `rebuild_*`/ingest entrypoint.

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/alphalens-django && uv run pytest edge/tests/test_ingest.py -q -k scorer_config_version`
Expected: FAIL — model has no `scorer_config_version`.

- [ ] **Step 3: Add the field + migration**

In `edge/models.py` `LadderOutcome`, next to `ladder_config_version` (~100):

```python
    scorer_config_version = models.CharField(max_length=128, blank=True, default="")
```

Generate the migration:

Run: `cd apps/alphalens-django && uv run python manage.py makemigrations edge -n ladderoutcome_scorer_config_version`
Expected: creates `00NN_ladderoutcome_scorer_config_version.py` adding the one field. Commit the generated file.

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/alphalens-django && uv run pytest edge -q 2>&1 | tail -6`
Expected: PASS (ingest tests + the rest of the edge suite; if a migration-head/contract test pins state, advance it as it was for prior fields).

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-django/edge/models.py \
        apps/alphalens-django/edge/migrations/00NN_ladderoutcome_scorer_config_version.py \
        apps/alphalens-django/edge/tests/test_ingest.py
git commit --signoff -m "feat(edge): persist scorer_config_version on LadderOutcome"
```

---

### Task 4: Edge API exposes `scorer_config_version`

**Files:**
- Modify: `apps/alphalens-django/edge/api/serializers.py` (`EdgeOutcomeRowSerializer` ~25-40)
- Modify: `apps/alphalens-django/edge/api/views.py` (`EdgeOutcomesView` row dict ~128-147)
- Test: `apps/alphalens-django/edge/tests/test_api.py`

**Interfaces:**
- Consumes: `LadderOutcome.scorer_config_version` (Task 3).
- Produces: each `/v1/edge/outcomes` row includes `scorer_config_version` (string; `""`/null when absent).

- [ ] **Step 1: Write the failing test**

In `edge/tests/test_api.py` (mirror the existing test asserting `theme` is in the outcomes response):

```python
def test_outcomes_row_includes_scorer_config_version(self):
    # create a LadderOutcome with scorer_config_version, hit /v1/edge/outcomes
    data = self._get_outcomes_json(scorer_config_version="scorer-v1-test")  # reuse existing helper
    assert data[0]["scorer_config_version"] == "scorer-v1-test"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/alphalens-django && uv run pytest edge/tests/test_api.py -q -k scorer_config_version`
Expected: FAIL — key absent in the row.

- [ ] **Step 3: Add to serializer + view**

In `serializers.py` `EdgeOutcomeRowSerializer` (alongside `theme`):

```python
    scorer_config_version = serializers.CharField(allow_null=True, required=False)
```

In `views.py` `EdgeOutcomesView`, in the explicit per-row dict (alongside the `theme` key):

```python
            "scorer_config_version": o.scorer_config_version or None,
```

(`or None` → the read side renders an em dash for blank, matching `theme`.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/alphalens-django && uv run pytest edge -q 2>&1 | tail -6`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-django/edge/api/serializers.py apps/alphalens-django/edge/api/views.py \
        apps/alphalens-django/edge/tests/test_api.py
git commit --signoff -m "feat(edge): serve scorer_config_version on /v1/edge/outcomes"
```

---

### Task 5: SPA — type + muted per-row chip

**Files:**
- Modify: `apps/web/src/lib/types.ts` (`EdgeOutcome` interface ~325-338)
- Modify: `apps/web/src/routes/edge/+page.svelte` (outcomes table row ~463-598; theme cell ~559-564; existing `ChipTip` pattern ~500-513)
- Test: an `/edge` component/unit test mirroring the repo convention (check `apps/web/tests/` for an existing edge/outcomes test; if none, a small vitest predicate test like the candidateCard pattern).

**Interfaces:**
- Consumes: the API field `scorer_config_version` (Task 4).
- Produces: `EdgeOutcome.scorer_config_version: string | null`; a muted chip rendered per row only when non-blank.

- [ ] **Step 1: Write the failing test**

Add a test asserting the row shows the version chip when `scorer_config_version` is non-blank and omits it when null/"". Mirror the repo's existing component-test style (predicate-level is acceptable, as in `candidateCard.test.ts`): e.g. a `showsScorerVersionChip = (o) => !!o.scorer_config_version` helper exercised over present/blank/null.

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/web && pnpm test:unit 2>&1 | tail -6`
Expected: FAIL (new test red) — or, if predicate-style, write the predicate test first so it fails before the template wires it.

- [ ] **Step 3: Add the type + chip**

In `types.ts` `EdgeOutcome` (after `theme`):

```typescript
	scorer_config_version: string | null;
```

In `edge/+page.svelte`, render a small muted chip in the outcomes-table row near the theme cell (mirror the muted, tone-neutral styling; provenance, not a signal). Only when present:

```svelte
{#if row.scorer_config_version}
	<span class="whitespace-nowrap rounded-sm border border-fg-muted/30 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-fg-muted"
	      title="Scorer config version (cohort / poolability key)">{row.scorer_config_version}</span>
{/if}
```

(Place it in the theme cell's column or as a small secondary line; keep it subdued. `whitespace-nowrap` is mandatory — the version has hyphens.)

- [ ] **Step 4: Run to verify it passes + checks**

Run: `cd apps/web && pnpm test:unit 2>&1 | tail -6` (PASS) ; `pnpm check` (0 errors) ; `pnpm build` (succeeds).

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/lib/types.ts apps/web/src/routes/edge/+page.svelte apps/web/tests
git commit --signoff -m "feat(web): show scorer_config_version chip on /edge outcomes"
```

- [ ] **Step 6: Push, open PR, zen review, merge**

```bash
git push -u origin feature/edge-scorer-config-version-chip
gh pr create --repo kamilpajak/AlphaLens --title "feat: scorer_config_version chip on /edge" --body "<summary + Behaviour notes: forward-only stamp; mirrors theme; deploy = VPS image rebuild + Django pull (migration) + CF Pages>"
# mcp__zen__codereview deepseek/deepseek-v4-pro thinking=high → apply findings as commits → CI green → merge
```

---

## Post-merge (operator-owned)

VPS `alphalens-pipeline:latest` image rebuild (the stamp) + Django GHCR pull (runs the edge migration) + CF Pages auto-deploys the SPA. Forward-only — outcomes produced after deploy carry the version; pre-deploy rows show blank.

## Self-review notes (spec coverage)

- Spec §4 stage 1 (pipeline stamp + source) → Tasks 1 (CandidateBrief field/loader) + 2 (monitor stamp + carry-forward). §4 stage 2 (model+migration, auto-ingest) → Task 3. §4 stage 3 (API serializer+view explicit) → Task 4. §4 stage 4 (SPA type + muted chip + nowrap) → Task 5. §5 testing → each task's test. §6 decisions (per-row chip, max_length=128, one PR) → honored (Task 3 = 128; one branch). §7 deploy → Post-merge. §8 risk "source not reachable" → resolved by Task 1 (the verified gap: CandidateBrief lacked the field). Reviewer interaction across apps: tasks ordered pipeline → Django → SPA so each builds on the prior; all in one PR, CI runs research + django + web suites together.
