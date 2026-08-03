# SL_TOUCHED Marker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the stop was crossed but closed a ZERO economic remainder (an early TP consumed the whole held position), emit a distinct `SL_TOUCHED` marker kind and render it dimmed — mirroring the TP/TP_TOUCHED sold-vs-touched honesty.

**Architecture:** The replay engine surfaces its already-computed `remaining` as `LadderOutcome.residual_fraction` + derived `sl_closed_nothing` property; the chart payload builder maps SL crossings through the existing `sold=` hook to `SL_TOUCHED`; the SPA renders it as a dimmed red circle. Classification, R math, /edge aggregates untouched.

**Tech Stack:** Python (dataclasses, unittest.TestCase), SvelteKit (Svelte 5), lightweight-charts v5, Vitest, Storybook 10.

**Spec:** `docs/superpowers/specs/2026-07-30-sl-touched-marker-design.md`

## Global Constraints

- All work in the worktree `/Users/jacoren/Developer/Personal/AlphaLens/.claude/worktrees/ladder-sl-touched` on branch `feature/ladder-sl-touched-marker`. Never touch the main checkout.
- Marker kind string is exactly `"SL_TOUCHED"`; epsilon constant `_RESIDUAL_EPS = 1e-9`.
- **No new per-marker JSON key** — the Django contract test pins the marker key set `{time, kind, level_id, price, label, ambiguous}`. Only the `kind` VALUE changes.
- Python tests MUST be `unittest.TestCase` (research CI runs `unittest discover`; pytest-style is silently skipped).
- Code comments/identifiers English only. Conventional Commits + DCO sign-off (`git commit -s`).
- Python gate (from worktree root, AFTER the worktree-local `uv sync` in Task 1): full research suite `.venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research`. Web gates run from `apps/web`: `pnpm run check`, `pnpm exec vitest run`, `pnpm run build-storybook`.
- Classification strings (`PARTIAL_TP_THEN_SL`, `SL_HIT`) and `realized_r` math must NOT change.

---

### Task 1: Replay — surface `residual_fraction` + `sl_closed_nothing` (TDD)

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_replay.py` (LadderOutcome ~:71-115; `_finalize` unpack :861-863 + ctor :873-892; `_realized_r_with_frac` :895-950; what-if call sites :1063, :1177)
- Test: `apps/alphalens-research/tests/test_feedback_ladder_replay.py` (append a class; reuse the file's `_bar(t, low, high, close)`, `_setup(...)`, `_EQUAL_3` helpers)
- Test: `apps/alphalens-research/tests/property/test_ladder_replay_properties.py` (one new property in `TestReplayInvariants`)

**Interfaces:**
- Consumes: existing `_realized_r_with_frac` locals (`remaining` at :939), `replay_ladder(setup, bars, entry_expiry_ms=...)`.
- Produces: `LadderOutcome.residual_fraction: float | None` and `LadderOutcome.sl_closed_nothing: bool` (property) — Task 2 reads `outcome.sl_closed_nothing`.

- [ ] **Step 1: Worktree-local environment**

```bash
cd /Users/jacoren/Developer/Personal/AlphaLens/.claude/worktrees/ladder-sl-touched
uv sync
.venv/bin/python -c "import alphalens_pipeline, sys; print(alphalens_pipeline.__file__)"
```

The printed path MUST be inside this worktree (known gotcha: a worktree editing package code needs its own venv). If it points at the main checkout, stop and report BLOCKED.

- [ ] **Step 2: Write the failing tests**

Append to `apps/alphalens-research/tests/test_feedback_ladder_replay.py`:

```python
class TestResidualFraction(unittest.TestCase):
    """``residual_fraction`` = the un-sold share of the FILLED position at exit
    time, surfaced so the chart can distinguish an SL that closed a real
    remainder from one that fired economically empty (the PSN 2026-07-19 case:
    TP1's re-based share consumed the whole held position days before the
    crash bar crossed the stop). ``sl_closed_nothing`` is the derived flag the
    chart consumes."""

    def test_sl_after_tp_consumed_all_is_economically_empty(self):
        # Shallow fill (E1 only, filled_frac=1/3) -> TP1's re-based share is
        # 1.0 and consumes the whole held position. The crash bar would cross
        # E2/E3 too, so the entry TTL cutoff (ts >= 3) keeps them from
        # refilling -- exactly the live PSN shape.
        setup = _setup(**_EQUAL_3)
        bars = [
            _bar(1, low=98.0, high=100.0, close=99.0),  # fills E1 (99) only
            _bar(2, low=100.0, high=103.0, close=102.5),  # touches TP1 (102) only
            _bar(3, low=91.0, high=100.0, close=91.5),  # crash through E2/E3/SL
        ]
        outcome = replay_ladder(setup, bars, entry_expiry_ms=3)
        self.assertEqual(outcome.entries_filled, ("E1",))
        self.assertEqual(outcome.classification, "PARTIAL_TP_THEN_SL")
        self.assertEqual(outcome.realized_tp_ids, ("TP1",))
        self.assertAlmostEqual(outcome.residual_fraction, 0.0, places=9)
        self.assertTrue(outcome.sl_closed_nothing)
        self.assertAlmostEqual(outcome.realized_r, 3.0 / 7.0, places=3)  # TP1 only

    def test_sl_with_real_remainder_is_not_empty(self):
        # Full single-tier fill: TP1 sells 1/3, the SL closes the other 2/3.
        setup = _setup(
            entries=[(99.0, 100.0)],
            tps=[(102.0, 33.3), (107.0, 33.3), (112.0, 33.3)],
            stop=92.0,
        )
        bars = [
            _bar(1, low=98.0, high=100.0, close=99.0),  # fills E1 fully
            _bar(2, low=100.0, high=103.0, close=102.5),  # TP1 sells its tranche
            _bar(3, low=91.0, high=100.0, close=91.5),  # SL closes the rest
        ]
        outcome = replay_ladder(setup, bars)
        self.assertEqual(outcome.classification, "PARTIAL_TP_THEN_SL")
        self.assertAlmostEqual(outcome.residual_fraction, 2.0 / 3.0, places=6)
        self.assertFalse(outcome.sl_closed_nothing)

    def test_straight_sl_has_full_remainder(self):
        setup = _setup(entries=[(99.0, 100.0)], tps=[(110.0, 100.0)], stop=92.0)
        bars = [
            _bar(1, low=98.0, high=100.0, close=99.0),  # fills E1
            _bar(2, low=91.0, high=99.0, close=91.5),  # SL
        ]
        outcome = replay_ladder(setup, bars)
        self.assertEqual(outcome.classification, "SL_HIT")
        self.assertAlmostEqual(outcome.residual_fraction, 1.0, places=9)
        self.assertFalse(outcome.sl_closed_nothing)

    def test_bad_geometry_leaves_residual_none(self):
        # BAD_GEOMETRY early-returns before realized-R is computed.
        setup = _setup(entries=[(99.0, 100.0)], tps=[(110.0, 100.0)], stop=100.0)
        bars = [_bar(1, low=98.0, high=99.0, close=98.5)]
        outcome = replay_ladder(setup, bars)
        self.assertEqual(outcome.classification, "BAD_GEOMETRY")
        self.assertIsNone(outcome.residual_fraction)
        self.assertFalse(outcome.sl_closed_nothing)
```

Append to `TestReplayInvariants` in `apps/alphalens-research/tests/property/test_ladder_replay_properties.py` (mirror the decorator/import style of the neighbouring properties in that class):

```python
    @given(ladder_and_bars())
    def test_residual_fraction_bounds(self, lab: Any) -> None:
        setup, bars = lab
        o = replay_ladder(setup, bars)
        if o.residual_fraction is not None:
            self.assertGreaterEqual(o.residual_fraction, 0.0)
            self.assertLessEqual(o.residual_fraction, 1.0)
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_feedback_ladder_replay.TestResidualFraction -v
```

Expected: FAIL/ERROR — `LadderOutcome` has no `residual_fraction`.

- [ ] **Step 4: Implement**

In `apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_replay.py`:

(a) Module constant directly above the `LadderOutcome` dataclass:

```python
# Epsilon under which a residual fraction counts as economically zero (matches
# the ``remaining > 1e-9`` guard inside the realized-R computation).
_RESIDUAL_EPS = 1e-9
```

(b) `LadderOutcome`: add the field right after `realized_tp_ids` (~:101) and the property after `touched_tp_count` (~:115):

```python
    # Un-sold share of the FILLED position at exit time (the ``remaining`` of
    # the realized-R re-basing). 0.0 = TP tranches consumed everything before
    # the exit; 1.0 = nothing sold before it. None when realized-R was never
    # computed (the BAD_GEOMETRY early return, or nothing ever filled).
    residual_fraction: float | None = None
```

```python
    @property
    def sl_closed_nothing(self) -> bool:
        """True when the stop was crossed but closed a ZERO economic remainder.

        The chart maps this to the dimmed ``SL_TOUCHED`` marker: a solid red
        arrow must not overstate a loss that never happened -- the exact
        mirror of the TP / TP_TOUCHED sold-vs-touched honesty above.
        """
        return (
            self.sl_hit
            and self.residual_fraction is not None
            and self.residual_fraction <= _RESIDUAL_EPS
        )
```

(c) `_realized_r_with_frac` (:895-950): change the signature's return annotation to `tuple[float, bool, tuple[str, ...], float]`, append one line to the docstring ("Also returns ``remaining`` -- the un-sold share at exit time."), and change the final return to:

```python
    return contrib, horizon_open, tuple(realized_tp_ids), remaining
```

(d) Update ALL THREE call sites (verify with `grep -n "_realized_r_with_frac(" ...`):
- `:861-863` (`_finalize` headline pass): `realized_r, horizon_open, realized_tp_ids, residual_fraction = _realized_r_with_frac(...)` and add `residual_fraction=residual_fraction,` to the `LadderOutcome(...)` ctor at :873-892. The BAD_GEOMETRY early return at :842-859 is NOT touched (field defaults to None).
- `:1063` and `:1177` (what-if passes): `contrib, _open, _tp_ids, _residual = _realized_r_with_frac(...)` — residual intentionally unused there.

- [ ] **Step 5: Run the focused tests, then the full research suite**

```bash
cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_feedback_ladder_replay -v
cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.property.test_ladder_replay_properties -v
cd /Users/jacoren/Developer/Personal/AlphaLens/.claude/worktrees/ladder-sl-touched && .venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research
```

Expected: all PASS (the discover run collects-but-skips live probes; that is normal).

- [ ] **Step 6: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_replay.py apps/alphalens-research/tests/test_feedback_ladder_replay.py apps/alphalens-research/tests/property/test_ladder_replay_properties.py
git commit -s -m "feat(feedback): surface replay residual_fraction + sl_closed_nothing on LadderOutcome"
```

---

### Task 2: Payload — emit `SL_TOUCHED` for an economically-empty stop (TDD)

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_chart.py` (marker constants :74-85; `_marker_kind_and_label` :265-279; `_markers_from_sequence` :282-313)
- Test: `apps/alphalens-research/tests/test_ladder_chart_payload.py` (append to `TestBuildChartPayload`; reuse `_bar`, `_session_open_ms`, `_payload`, `_ARRIVAL`, `_NEXT_SESSION`)

**Interfaces:**
- Consumes: `outcome.sl_closed_nothing` from Task 1; `replay_ladder(setup, bars, entry_expiry_ms=...)`.
- Produces: marker dicts with `kind == "SL_TOUCHED"` (same key set as before) — Task 3 consumes the kind string in the SPA.

- [ ] **Step 1: Write the failing tests**

Append to `TestBuildChartPayload` in `apps/alphalens-research/tests/test_ladder_chart_payload.py`:

```python
    def test_sl_that_closed_nothing_is_distinct_kind(self) -> None:
        """An SL crossing that closed a ZERO economic remainder (an early TP
        consumed the whole held position; entry TTL kept deeper tiers from
        refilling on the crash bar) must render ``SL_TOUCHED`` -- a solid red
        arrow would overstate a loss that never happened."""
        setup = {
            "status": "OK",
            "schema_version": "1.0.0",
            "suggested_size_pct": 2.0,
            "disaster_stop": 70.0,
            "atr": 2.0,
            "order_ttl_days": 7,
            "entry_tiers": [
                {"limit": 100.0, "alloc_pct": 33.3},
                {"limit": 90.0, "alloc_pct": 33.3},
                {"limit": 80.0, "alloc_pct": 33.3},
            ],
            "tp_tranches": [
                {"target": 110.0, "tranche_pct": 33.3},
                {"target": 120.0, "tranche_pct": 33.3},
                {"target": 130.0, "tranche_pct": 33.3},
            ],
        }
        arrival_open = _session_open_ms(_ARRIVAL)
        bars = [
            _bar(arrival_open, o=101.0, h=102.0, low=99.0, c=100.5),  # E1 only
            _bar(arrival_open + 60_000, o=105.0, h=111.0, low=104.0, c=110.5),  # TP1 touch only
            # Crash bar next session: would cross E2/E3 and the stop -- the
            # TTL cutoff at the session open blocks the refills.
            _bar(_session_open_ms(_NEXT_SESSION), o=95.0, h=96.0, low=69.0, c=70.5),
        ]
        outcome = replay_ladder(
            setup, bars, entry_expiry_ms=_session_open_ms(_NEXT_SESSION)
        )
        self.assertEqual(outcome.realized_tp_ids, ("TP1",))  # TP1 sold it all
        self.assertTrue(outcome.sl_hit)
        self.assertTrue(outcome.sl_closed_nothing)  # precondition from Task 1
        payload = _payload(bars, outcome, setup=setup)
        sl_markers = [m for m in payload["markers"] if m["level_id"] == "SL"]
        self.assertEqual(len(sl_markers), 1)
        self.assertEqual(sl_markers[0]["kind"], "SL_TOUCHED")
        self.assertEqual(sl_markers[0]["label"], "SL")  # label text unchanged

    def test_sl_with_real_remainder_stays_solid_kind(self) -> None:
        """A stop that closed a REAL remainder keeps the solid ``SL`` kind."""
        setup = {
            "status": "OK",
            "schema_version": "1.0.0",
            "suggested_size_pct": 2.0,
            "disaster_stop": 70.0,
            "atr": 2.0,
            "order_ttl_days": 7,
            "entry_tiers": [{"limit": 100.0, "alloc_pct": 100.0}],
            "tp_tranches": [
                {"target": 110.0, "tranche_pct": 33.3},
                {"target": 120.0, "tranche_pct": 33.3},
                {"target": 130.0, "tranche_pct": 33.3},
            ],
        }
        arrival_open = _session_open_ms(_ARRIVAL)
        bars = [
            _bar(arrival_open, o=101.0, h=102.0, low=99.0, c=100.5),  # full fill
            _bar(arrival_open + 60_000, o=105.0, h=111.0, low=104.0, c=110.5),  # TP1 sells 1/3
            _bar(_session_open_ms(_NEXT_SESSION), o=95.0, h=96.0, low=69.0, c=70.5),  # SL closes 2/3
        ]
        outcome = replay_ladder(setup, bars)
        self.assertTrue(outcome.sl_hit)
        self.assertFalse(outcome.sl_closed_nothing)
        payload = _payload(bars, outcome, setup=setup)
        sl_markers = [m for m in payload["markers"] if m["level_id"] == "SL"]
        self.assertEqual(len(sl_markers), 1)
        self.assertEqual(sl_markers[0]["kind"], "SL")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_ladder_chart_payload -v
```

Expected: the two new tests FAIL (kind is `SL`, not `SL_TOUCHED`); everything else passes.

- [ ] **Step 3: Implement**

In `apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_chart.py`:

(a) After `_MARKER_SL = "SL"` (:84):

```python
# An SL crossing that closed a ZERO economic remainder: the stop line WAS
# crossed, but an earlier TP tranche had already consumed the whole held
# position (LadderOutcome.sl_closed_nothing). Drawn dimmed so a solid red
# arrow never overstates a loss that did not happen -- the exact mirror of
# the TP_TOUCHED sold-vs-touched honesty above.
_MARKER_SL_TOUCHED = "SL_TOUCHED"
```

(b) `_marker_kind_and_label` (:265-279): change the SL branch to
`return (_MARKER_SL if sold else _MARKER_SL_TOUCHED), level_id` and extend the
docstring: "An SL crossing that closed no remainder (``sold=False``) maps to
``SL_TOUCHED``."

(c) `_markers_from_sequence` (:299-301): replace the single `sold = ...` line
(and its comment) with:

```python
        # A TP crossing counts as SOLD only if it appears in realized_tp_ids
        # (positive re-based share); an SL counts as SOLD only if it closed a
        # real remainder. Every other kind passes sold=True unchanged.
        if crossing.kind == "TP":
            sold = crossing.level_id in realized_tps
        elif crossing.kind == "SL":
            sold = not outcome.sl_closed_nothing
        else:
            sold = True
```

- [ ] **Step 4: Run the focused file, then the full research suite**

```bash
cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_ladder_chart_payload -v
cd /Users/jacoren/Developer/Personal/AlphaLens/.claude/worktrees/ladder-sl-touched && .venv/bin/python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_chart.py apps/alphalens-research/tests/test_ladder_chart_payload.py
git commit -s -m "feat(feedback): emit SL_TOUCHED marker when the stop closed a zero remainder"
```

---

### Task 3: SPA — render `SL_TOUCHED` dimmed + story

**Files:**
- Modify: `apps/web/src/lib/types.ts` (`ChartMarker.kind` union ~:450 + doc comment ~:442-447)
- Modify: `apps/web/src/lib/components/ladderChart.ts` (`EXIT_KINDS` :8-13)
- Modify: `apps/web/src/lib/components/LadderChart.svelte` (`stopHit` ~:114-116; `buildMarkers` switch + legend comment ~:410-442)
- Modify: `apps/web/src/lib/components/LadderChart.stories.svelte` (one new story)
- Test: `apps/web/tests/unit/ladderChart.test.ts`

**Interfaces:**
- Consumes: payload markers with `kind: 'SL_TOUCHED'` (Task 2).
- Produces: nothing consumed later (Task 4 is process only).

- [ ] **Step 1: Install web deps in the worktree**

```bash
cd /Users/jacoren/Developer/Personal/AlphaLens/.claude/worktrees/ladder-sl-touched && pnpm install
```

- [ ] **Step 2: Write the failing vitest test**

Append to the `finalExitMarkerTime` describe block in `apps/web/tests/unit/ladderChart.test.ts`:

```ts
	it('ends the band at an SL_TOUCHED crossing (economically empty stop)', () => {
		// The stop was crossed but closed nothing (earlier TPs consumed the
		// whole held position) — the in-trade window still ends there.
		const markers = [
			marker('ENTRY', '2026-06-13', 'E1'),
			marker('TP', '2026-06-15', 'TP1'),
			marker('SL_TOUCHED', '2026-06-18', 'SL')
		];
		expect(finalExitMarkerTime(markers)).toBe('2026-06-18');
	});
```

Run: `cd apps/web && pnpm exec vitest run tests/unit/ladderChart.test.ts` — expected: FAIL (type error / EXIT_KINDS misses the kind; if the type union blocks compilation, that IS the red state).

- [ ] **Step 3: Implement**

(a) `types.ts` ~:450 — extend the union to
`'ENTRY' | 'TP' | 'TP_TOUCHED' | 'SL' | 'TIME_STOP' | 'SL_TOUCHED'` and append to
the doc comment above: `SL_TOUCHED = the stop was crossed but closed a ZERO
economic remainder (earlier TPs had consumed the whole held position) — drawn
dimmed so a solid red arrow never overstates a loss that did not happen.`

(b) `ladderChart.ts` `EXIT_KINDS` — add `'SL_TOUCHED'` to the set and extend the
comment: an economically-empty stop still ends the in-trade window.

(c) `LadderChart.svelte`:
- `stopHit` (~:114-116): `payload.markers.some((m) => m.kind === 'SL' || m.kind === 'SL_TOUCHED' || m.kind === 'TIME_STOP')` — the stop price line dims because the level WAS crossed.
- `buildMarkers` legend comment block (~:410-414): add a row
`//   SL_TOUCHED → dim red circle belowBar (stop crossed, closed nothing)`.
- `buildMarkers` switch — insert after `case 'SL'`:

```ts
				case 'SL_TOUCHED':
					// Stop crossed but the position was already fully sold by earlier
					// TPs — economically empty. Dimmed circle (no arrow), mirroring
					// TP_TOUCHED: a solid red arrow must not overstate a loss that
					// never happened.
					return { ...base, position: 'belowBar', color: `${COLOR.red}66`, shape: 'circle' };
```

- [ ] **Step 4: Run the vitest test to verify it passes**

```bash
cd apps/web && pnpm exec vitest run tests/unit/ladderChart.test.ts
```

Expected: PASS.

- [ ] **Step 5: Add the story**

In `LadderChart.stories.svelte`, after the existing "Partial Capture" payload/story, add (follow the file's `{#snippet template()}` + sized-div `<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">` pattern used by EVERY story):

```ts
	// PARTIAL CAPTURE THEN EMPTY STOP: a shallow fill let TP1 sell the WHOLE
	// held position; the stop was crossed later on a crash bar but closed
	// nothing (the PSN 2026-07-19 shape). The SL renders as a dimmed circle
	// (SL_TOUCHED), not a solid red arrow — the loss never happened
	// economically.
	const EMPTY_STOP_PAYLOAD: ChartPayload = {
		...CLOSED_PAYLOAD,
		ticker: 'PSN',
		realized_r: 0.4,
		markers: [
			marker('ENTRY', '2026-06-16', 'E1', 'e1'),
			marker('TP', '2026-06-19', 'TP1', 'tp1'),
			marker('TP_TOUCHED', '2026-06-20', 'TP2', 'tp2'),
			marker('SL_TOUCHED', '2026-06-23', 'SL', 'sl')
		]
	};
```

```svelte
<Story name="Empty Stop (SL touched, nothing closed)">
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart payload={EMPTY_STOP_PAYLOAD} />
		</div>
	{/snippet}
</Story>
```

(All marker times are existing `CLOSED_PAYLOAD` bar dates — 2026-06-16/19/20/23 — so every marker lands on a bar.)

- [ ] **Step 6: Run all web gates**

```bash
cd apps/web && pnpm run check && pnpm exec vitest run && pnpm run build-storybook
```

Expected: check 0 errors (6 pre-existing warnings are known), vitest all pass, storybook build pass.

- [ ] **Step 7: Visual sanity check**

Start `pnpm run storybook` (from `apps/web`), open LadderChart → "Empty Stop (SL touched, nothing closed)": the SL renders as a dim red circle below the last bar (not a solid arrow), TP2 as a dim green circle, and the stop price line is dimmed. Screenshot via Playwright MCP browser tools if available; otherwise report the check as not-verified in your report (do not skip silently). Stop the server afterwards.

- [ ] **Step 8: Commit**

```bash
git add apps/web/src/lib/types.ts apps/web/src/lib/components/ladderChart.ts apps/web/src/lib/components/LadderChart.svelte apps/web/src/lib/components/LadderChart.stories.svelte apps/web/tests/unit/ladderChart.test.ts
git commit -s -m "feat(web): render SL_TOUCHED as a dimmed stop marker"
```

---

### Task 4: Push branch and open the PR

**Files:** none (process only).

**Interfaces:**
- Consumes: commits from Tasks 1-3 on `feature/ladder-sl-touched-marker`.
- Produces: an open PR on `kamilpajak/AlphaLens` (zen pre-merge review + CI watch happen orchestrator-side).

- [ ] **Step 1: Push**

```bash
git push -u origin feature/ladder-sl-touched-marker
```

- [ ] **Step 2: Open the PR**

```bash
gh pr create --repo kamilpajak/AlphaLens \
  --title "feat: SL_TOUCHED — dim the stop marker when it closed a zero remainder" \
  --body "$(cat <<'EOF'
Feature: distinguish a stop that actually cut a position from one that fired economically empty, on the ladder replay chart.

## How it works today / problem
- The realized-R re-basing lets an early TP consume the WHOLE filled position when the entry fill was shallow (e.g. PSN 2026-07-19: E1-only fill, TP1 sold everything, +0.40R).
- A later stop crossing then closes a ZERO remainder — yet the chart draws the same solid red `SL` arrow as a real stop-out. That overstates a loss that never happened. PR #847 fixed the exact mirror image on the TP side (`TP` vs `TP_TOUCHED`).

## Goal
- Sold-vs-touched honesty on the stop side: the viewer should see at a glance whether the SL actually cost anything.

## How
- Replay surfaces its already-computed remainder: `LadderOutcome.residual_fraction` + derived `sl_closed_nothing` (`sl_hit` and residual ≤ 1e-9).
- Payload builder maps such SL crossings to a new marker kind `SL_TOUCHED` via the existing `sold=` hook; marker key set unchanged (Django passthrough untouched).
- SPA renders `SL_TOUCHED` as a dimmed red circle (mirror of TP_TOUCHED), counts it for `stopHit` (price line dims) and for the in-trade band end.
- Classification (`PARTIAL_TP_THEN_SL`/`SL_HIT`), R math, and /edge aggregates are untouched — display-layer honesty only.

Design spec: `docs/superpowers/specs/2026-07-30-sl-touched-marker-design.md`

## Test plan
- [ ] Research suite (`unittest discover`): new `TestResidualFraction` (empty / real-remainder / straight-SL / BAD_GEOMETRY) + payload kind tests + residual bounds property
- [ ] `apps/web`: `pnpm run check`, `pnpm exec vitest run` (band ends at SL_TOUCHED), `pnpm run build-storybook`
- [ ] Manual: Storybook "Empty Stop (SL touched, nothing closed)" story
- Not covered: no DOM/canvas render test for the component (pre-existing gap).

## Known issues / behaviour notes
- Forward-only: settled rows are reuse-first (#912/#923), so persisted payloads keep the solid SL arrow; new/refreshed payloads get the honest marker. No backfill.
- `residual_fraction` is populated for non-SL exits too (horizon-open, time-stop); only the SL marker consumes it today.
- Takes effect in production after the next VPS-local `alphalens-pipeline:latest` image rebuild (manual, per deploy runbook).
EOF
)"
```

- [ ] **Step 3: Report the PR URL**

Return the PR number/URL to the orchestrator.
