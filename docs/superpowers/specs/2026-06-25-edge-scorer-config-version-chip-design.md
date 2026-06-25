# Design: `scorer_config_version` chip on /edge

**Date:** 2026-06-25
**Status:** DRAFT — approved in brainstorming; awaiting plan
**Author:** session (primary, main checkout — implementation in a worktree)
**Related:** follow-up to the ATR-tilt epic (PRs #673/#675/#676). Spec `docs/superpowers/specs/2026-06-25-selection-score-atr-tilt-design.md` §7 deferred this. Memory `project_selection_score_atr_tilt_2026_06_25`.

---

## 1. Goal & TL;DR

Surface `scorer_config_version` (the scorer poolability key, set per brief by the ATR-tilt pipeline) on the `/edge` dashboard, so the cohort boundary is visible **where outcomes are read**. PR-3 (#676) put the version chip on the brief card but deferred `/edge` because the `/edge` route reads `population_ladders` outcome rows, which carry no brief-signal columns.

**Approach (chosen):** carry `scorer_config_version` onto each ladder-outcome row by **stamping** it at parquet-write time — exactly the established pattern for `theme` (`_stamp_theme`) and the existing `ladder_config_version` config-version column. Then it auto-ingests to Django (field-name-driven), is serialized explicitly, and renders as a small muted per-row chip on `/edge`.

**Forward-only:** only ladder outcomes produced after deploy carry the version; older rows show blank (their briefs predate the field, so a join couldn't recover it either — which is why stamping, not a `Brief⋈LadderOutcome` join, is the right call).

---

## 2. Non-goals

- **No full selection_score breakdown on /edge** — only the `scorer_config_version` chip (the breakdown lives on the brief card per #676).
- **No `Brief ⋈ LadderOutcome` query-time join** — rejected: cross-app (sibling apps, no FK), per-request DB work, inconsistent with the denormalized-stamp pattern, and historical rows have no version to recover anyway.
- **No backfill** of historical ladder outcomes (forward-only, consistent with the rest of the epic).
- **No change** to how outcomes are selected/computed — pure provenance display.

---

## 3. Current state (from code map)

- **Pipeline** `alphalens_pipeline/feedback/population_ladder_monitor.py`: brief-level columns are stamped onto outcome rows — `_stamp_theme` (≈949-965) stamps `theme` (sourced from the brief candidate, `c.theme` ≈1558); `_terminal_row` (≈723-806) stamps `ladder_config_version` (≈793). Adding a stamped column is **explicit** (not field-driven); carried-forward rows back-fill missing columns to `None` (`_carry_prior` ≈930-946).
- **Django edge** `edge/models.py` `LadderOutcome` (composite PK `(brief_date, ticker)`) already has `theme` + `ladder_config_version` CharFields; `edge/ingest/parquet.py::_row_to_outcome` + `_payload_fields` is **field-name-driven auto-ingest** (any model field whose parquet column exists flows in).
- **Edge API** `edge/api/serializers.py::EdgeOutcomeRowSerializer` + `edge/api/views.py::EdgeOutcomesView` build the `/v1/edge/outcomes` row **explicitly** (manual dict; `theme` is included, `ladder_config_version` is not). The `LadderOutcomeSerializer` (`exclude=("pk",)`) auto-exposes all fields but the outcomes endpoint uses the explicit `EdgeOutcomeRowSerializer`.
- **SPA** `apps/web/src/lib/types.ts::EdgeOutcome` (≈325-338) + `apps/web/src/routes/edge/+page.svelte` outcomes table (≈393-602); existing per-row chip pattern = `ChipTip` on `ladder_classification` (≈500-513); `theme` rendered as a text cell (≈559-564).

---

## 4. Data flow (the change, 4 stages mirroring `theme`)

1. **Pipeline stamp** — in `population_ladder_monitor.py`, stamp `scorer_config_version` onto the outcome row from the brief candidate (same source as `theme`). Verify the candidate/brief object the monitor consumes exposes `scorer_config_version` (it is on the brief parquet from PR-1). If the brief loader doesn't surface it, add that read. Stamp in the same place/manner as `theme` (a `_stamp_*`-style write or a `_terminal_row` row-dict key), so carried-forward rows back-fill `None` via the existing path.
2. **Django model + migration** — add `scorer_config_version = models.CharField(max_length=128, blank=True, default="")` to `LadderOutcome` + an edge migration. Auto-ingests (field-name-driven) once the parquet column exists.
3. **Edge API** — add `scorer_config_version` to `EdgeOutcomeRowSerializer` (declare the field) **and** to the explicit row dict in `EdgeOutcomesView` (so `/v1/edge/outcomes` includes it).
4. **SPA** — add `scorer_config_version: string | null` to the `EdgeOutcome` type; render a **small muted, tone-neutral chip** per outcomes-table row (near the theme cell or alongside the classification chip), shown only when non-blank. It is a provenance label, not a signal — subdued styling, `whitespace-nowrap` on the version token (CLAUDE.md atomic-token rule).

---

## 5. Testing

- **Pipeline:** an outcome row built from a brief candidate carrying `scorer_config_version` stamps that value onto the row (and a candidate without it → blank/None, carried-forward path safe).
- **Django:** edge ingest round-trip — a population_ladders parquet with `scorer_config_version` lands it on `LadderOutcome`; a parquet without the column ingests to blank (no crash).
- **API:** `/v1/edge/outcomes` includes `scorer_config_version` in each row (present + blank cases).
- **SPA:** the chip renders when `scorer_config_version` is non-blank, and is omitted when blank/null. Mirror the repo's component-test convention.

---

## 6. Decisions (judgment calls, locked)

- **Per-row chip** (not a per-day/header indicator) — mirrors `theme` (also per-row); simplest, YAGNI. The version repeats across a cohort's rows, which is acceptable for a provenance label.
- **`max_length=128`** on the edge CharField — matches the brief-side `scorer_config_version` field.
- **One vertical-slice PR** (pipeline + Django + SPA) — single column end-to-end, too small to split.

---

## 7. Deploy & rollout

Operator-owned, same as the rest of the epic: VPS `alphalens-pipeline:latest` image rebuild (the stamp) + Django GHCR pull (runs the edge migration) + CF Pages auto-deploys the SPA. Forward-only — outcomes produced after deploy carry the version; pre-deploy rows show blank.

---

## 8. Risks & mitigations

- **Source not reachable at stamp time** → the plan's first step verifies the monitor's candidate/brief object exposes `scorer_config_version`; if not, add the brief-parquet read (the column exists on the brief parquet from PR-1).
- **Chip noise** (same version on every row) → muted/subdued styling; it's provenance, not a ranking signal; shown only when present.
- **Migration on populated edge table** → CharField with `default=""` is a safe metadata add (forward-only).
- **Contract drift** → the edge API row is explicit (not auto), so the serializer + view + SPA type stay in lockstep via the tests in §5.
