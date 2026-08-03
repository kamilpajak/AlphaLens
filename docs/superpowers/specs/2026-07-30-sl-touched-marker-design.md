# SL_TOUCHED — dim the stop marker when the SL closed a zero remainder

**Date:** 2026-07-30
**Status:** APPROVED (user, 2026-07-30)
**Scope:** pipeline (`alphalens_pipeline/feedback/`) + `apps/web`. Mixed-stack PR, one zen pass.

## Problem

`_realized_r_with_frac` re-bases TP tranche shares over the FILLED position
(capped at 1.0). When a shallow entry fill lets an early TP consume the whole
held position (e.g. PSN brief 2026-07-19: E1-only fill, TP1 re-based share =
1.0, +0.40R), a later stop crossing closes a ZERO economic remainder — yet the
chart renders the same solid red `SL` arrow as a stop that actually cut a
position. That overstates a loss that never happened. PR #847 fixed the exact
mirror image on the TP side (`TP` vs `TP_TOUCHED`, sold-vs-touched honesty);
the SL side was left unhandled.

## Decision

Emit a distinct marker kind **`SL_TOUCHED`** when the stop was crossed but the
economic remainder at that point was zero. Classification
(`PARTIAL_TP_THEN_SL` / `SL_HIT`), R math, and all /edge aggregates are
untouched — this is display-layer honesty only.

### Pipeline (source of truth — the SPA cannot infer this)

1. **Surface the remainder from the replay** (`ladder_replay.py`):
   - `_realized_r_with_frac` additionally returns its local `remaining`
     (4-tuple `(contrib, horizon_open, realized_tp_ids, remaining)`).
   - New `LadderOutcome` field `residual_fraction: float | None = None` —
     the un-sold share of the FILLED position at exit time. Wired in
     `_finalize` (single call site ~:861 + `LadderOutcome(...)` ctor ~:873-892).
     The BAD_GEOMETRY early return never computes it → stays `None`.
   - New derived property `LadderOutcome.sl_closed_nothing: bool` =
     `self.sl_hit and self.residual_fraction is not None and
     self.residual_fraction <= _RESIDUAL_EPS` with `_RESIDUAL_EPS = 1e-9`
     (same epsilon `_realized_r_with_frac` already uses for `remaining`).
2. **Marker emission** (`ladder_chart.py`):
   - New constant `_MARKER_SL_TOUCHED = "SL_TOUCHED"` next to the existing
     marker constants (~:74-85), with a rationale comment mirroring the
     TP_TOUCHED one.
   - `_marker_kind_and_label`: the SL branch honours the existing `sold`
     keyword — `(_MARKER_SL if sold else _MARKER_SL_TOUCHED), level_id`.
     Label text stays `SL` (`level_id`); only the kind differs.
   - `_markers_from_sequence`: compute `sold` per kind — for `SL` crossings
     `sold = not outcome.sl_closed_nothing`; TP logic unchanged; other kinds
     always sold.
   - **No new per-marker JSON field** — the Django contract test pins the
     exact marker key set `{time, kind, level_id, price, label, ambiguous}`;
     a new kind VALUE is safe, a new key is not.
3. Django: opaque passthrough, no change.

### Frontend (`apps/web`)

4. `types.ts` `ChartMarker.kind` union: add `'SL_TOUCHED'`; extend the
   doc comment with the SL rationale (crossed, closed nothing).
5. `ladderChart.ts` `EXIT_KINDS`: add `'SL_TOUCHED'` — the in-trade band must
   still end at the stop crossing.
6. `LadderChart.svelte`:
   - `stopHit`: also true for `'SL_TOUCHED'` (the stop price line renders
     dimmed — the level WAS crossed).
   - `buildMarkers`: `case 'SL_TOUCHED'` → dimmed red circle, `belowBar`,
     `` `${COLOR.red}66` ``, shape `'circle'` — mirrors the TP_TOUCHED
     dim-circle pattern; update the marker-legend comment block.
7. Storybook: one new story "Partial Capture then Empty Stop" — E1-only fill,
   TP1 sold-all, TP2 touched, SL_TOUCHED at the end (the PSN 07-19 shape).

## Rejected alternatives

- **Chart-local recompute of `remaining`** from `parsed.tps` weights +
  `outcome.filled_fraction` + `realized_tp_ids`: duplicates the re-basing/cap
  logic (`min(share, 1 - cumulative)`, `share <= 0` skip) — silent drift risk.
- **Changing the classification** (`PARTIAL_TP_THEN_SL`): feeds /edge stats
  and episode aggregation; display-only change keeps verdicts stable.
- **Queryable `sl_closed_nothing` parquet/Postgres column**: no consumer today
  (YAGNI); the monitor's `captured/touched_tp_count` precedent exists if one
  appears.

## Behaviour notes / limitations

- **Forward-only:** settled rows are reuse-first (#912/#923) — their persisted
  `chart_payload_json` is not recomputed, so historical charts keep the solid
  SL arrow. New/refreshed payloads get the honest marker. No forced backfill.
- Old payloads lack the kind entirely; the SPA `default` marker branch would
  render an unknown kind as a grey circle — not applicable here since old
  payloads only carry old kinds.
- `residual_fraction` is also populated for non-SL exits (horizon-open,
  time-stop); only the SL marker consumes it today.

## Testing

- **Replay (unittest.TestCase — research CI runs `unittest discover`; pytest
  style is silently skipped):** in `tests/test_feedback_ladder_replay.py`
  (`TestRealizedTpCapture` area): (a) shallow fill, TP1 consumes all, SL later
  → `residual_fraction == 0.0` and `sl_closed_nothing is True`;
  (b) partial capture with real remainder stopped out →
  `residual_fraction > 0` and `sl_closed_nothing is False`;
  (c) no-TP straight SL → `residual_fraction == 1.0` and `sl_closed_nothing
  is False` (a straight SL closes a REAL remainder — marker stays solid `SL`).
- **Payload:** `tests/test_ladder_chart_payload.py` — mirror
  `test_touched_but_unsold_tp_markers_are_distinct_kind`: assert the SL
  crossing maps to `SL_TOUCHED` when the outcome has zero residual, and stays
  `SL` in the real-remainder case.
- **Property tests** (`tests/property/test_ladder_replay_properties.py`): add
  the invariant `0.0 <= residual_fraction <= 1.0` when not None.
- **Web (vitest):** `EXIT_KINDS` includes `SL_TOUCHED` (band ends at it);
  type union compiles.
- Gates: research `unittest discover`; from `apps/web`: `pnpm run check`,
  `pnpm exec vitest run`, `pnpm run build-storybook`.
