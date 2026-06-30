# /edge what-if lens registry — implementation plan (revised)

**Date:** 2026-06-30
**Status:** PLAN (revised from "single breakeven_realized_r column" → registry-driven lens selector)
**Why revised:** Workflow + Perplexity (BI/UX + SEC-marketing-rule + overconfidence literature) + frontend-design skill converged: a static column is the weakest option; the right shape is a default-realized production view + a demarcated what-if SANDBOX with a REGISTRY-driven lens selector, persistent in-sample labelling, and progressive disclosure. Doctrine: memory `reference_edge_stamp_and_whatif_ui_doctrine_2026_06_30`.

## Design invariants (non-negotiable)
1. **Default REALIZED.** Primary EDGE/portfolio/deployment cards stay realized-only; what-if never appears in a primary KPI card.
2. **What-if is a LENS** (alternative value of an already-pooled metric on the SAME picks + price paths), surfaced in its own demarcated section/sandbox.
3. **Registry-driven, not hard-coded toggles.** A what-if MAP `{lens_id: realized_r}` per row + a lens registry `{id, label, mfe_trigger_r, trail_frac, category, status}`. New lens = registry entry, ZERO UI change. `status ∈ {in_sample, validated}`; a lens graduates in_sample→validated when forward N≥30 (no code change, just the flag).
4. **Persistent honest label.** A standing banner "WHAT-IF · in-sample · N=<n> · not validated" whenever a lens is active + a per-lens badge. Visual: realized solid/primary, what-if dashed/muted.
5. **Progressive disclosure / overconfidence guard.** Casual view shows validated lenses; in_sample lenses behind an "experimental" expander. No cockpit of toggles.

## Lens registry (MVP)
`BREAKEVEN_LENSES` (pipeline const, mirrored to a TS const or served): MVP ships ONE — `be_0p5r` (label "break-even +0.5R", mfe_trigger_r=0.5, trail_frac=None, category="exit-stop", status="in_sample"). Architecturally ready for `be_0p5r_trail0p6`, `be_0p3r`, etc. (the script grid) — adding them is a registry-list edit, no schema change (JSON map).

## Sites to touch (mirrors the ratchet_realized_r path; map verified 2026-06-30)

### Stage A — pipeline + Django (data)
**alphalens-pipeline:**
- `feedback/breakeven_lenses.py` (NEW): `BREAKEVEN_LENSES` registry + `breakeven_grid(setup, bars)` → `{lens_id: replay_ladder_breakeven(...)}` (reuses the merged `replay_ladder_breakeven`, PR #722).
- `feedback/population_ladder_monitor.py`: call `breakeven_grid(...)` alongside `replay_ladder_grid(...)` (~line 2158); stamp `"breakeven_realized_r_json": json.dumps(grid)` onto the row dict (~line 846); add `_BREAKEVEN_COLUMNS = ("breakeven_realized_r_json",)` and include it in `_carry_prior` (~line 1004), `_nonplannable_row` (~935), `_placeholder_row` (~979). (Mirror `_GRID_COLUMNS`.)
- TDD: extend `apps/alphalens-research/tests/test_population_ladder_monitor.py` (row carries the JSON map; carried-prior back-fills it) + the existing `test_feedback_ladder_replay.py` already covers `replay_ladder_breakeven`.

**alphalens-django (edge):**
- `edge/models.py`: `breakeven_realized_r_json = models.JSONField(null=True, blank=True)` (mirror `grid_realized_r_json` if it is JSONField; else TextField) + migration.
- ingest: auto via `_payload_fields()` (model-field introspection) — confirm the JSON coercion path matches `grid_realized_r_json`.
- `edge/api/summary.py`: add a `whatif` block — per registered lens, the mean R over terminal rows (gated like the edge panel: nulled below N_GATE) + the lens metadata. Namespaced clearly as what-if; realized aggregates untouched.
- `edge/api/serializers.py`: add the `whatif` field to the summary serializer (NOT to the primary EdgeOutcomeRow unless a per-row what-if column is shown).
- Regenerate `docs/openapi-parity/django.json` + `apps/web/src/lib/api-types.gen.ts`; the parity test (#721) gates it.
- TDD: `edge/tests/test_summary.py` (whatif block present + gated) + `test_ingest.py` fixture carries the column.

### Stage B — SPA (apps/web)
- `src/routes/edge/+page.svelte`: a NEW demarcated `// what-if (counterfactual · in-sample)` section BELOW the realized panels. A registry-driven lens selector (segmented control), default "off / realized". When a lens is active: persistent banner + badge, show the lens's mean-R aggregate (muted/dashed styling), and optionally a what-if column beside `%book` in the terminals table.
- Registry on the client: read lens metadata from the summary `whatif` block (or a small static TS mirror). Progressive disclosure: `status==='in_sample'` lenses behind an "experimental" expander.
- Aesthetic (frontend skill): consistent dark-terminal; what-if encoded muted/dashed; the banner is unmissable but not alarmist; one tasteful reveal, no toggle cockpit.
- TDD: web unit/integration — selector renders from registry, banner shows when active, realized panels unchanged, `status` gating.

## Honest-presentation acceptance checks
- Primary cards show ONLY realized. ✓
- A what-if number is NEVER shown without the persistent "in-sample · not validated" label. ✓
- Adding a second lens requires only a `BREAKEVEN_LENSES` entry (no UI/schema change). ✓
- Realized %book column is never overwritten. ✓

## NOT in this PR (deferred)
- Forward production stop change (that is the heavy PR #2: trade_setup change + `ladder_config_version` exit_stop_policy key + forward N reset + group comms + joint entry×exit validation). This PR is display-only; zero production-process change; generates no new pooled outcomes.
- Config-version cohort FILTERS and high-ATR are out of scope (different affordances per the doctrine).

## Suggested split
Stage A (pipeline+Django data) and Stage B (SPA) as two PRs (project pattern, e.g. expert-panel PR-8a/8b), each TDD + zen. Stage A ships the registry + stamped column (invisible until B); Stage B surfaces it.
