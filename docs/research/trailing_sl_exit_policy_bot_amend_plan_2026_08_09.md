# Trailing SL exit policy (bot-amend, "Tor A") Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a bot-amend trailing stop-loss as a new `ExitPolicy` (`trailing_atr`), flag-gated + default OFF, fully hermetically tested, that ratchets the resting `StopIfTraded` UP as price makes new highs and never moves it down.

**Architecture:** The high-water peak is per-position mutable state owned by the daemon (a `PeakTracker` on `LoopDeps`, updated from the live price feed each tick); it is injected into the pure `ProtectionView` snapshot. A new pure `_maybe_trail` arm (sibling to the shipped `_maybe_reanchor`) computes a Chandelier target `peak − k·ATR` from the injected peak, applies a monotone-up ratchet (floored on the journal-folded last-trailed level) plus the shipped never-below-brief-floor clamp, and emits the same `AmendStop` action. The policy stays a pure `(avg_price, atr, peak) → target` calculator so a future ML policy is one more registry entry.

**Tech Stack:** Python 3.12, stdlib `math` only in the pure layer, frozen dataclasses, `unittest.TestCase` (research CI is `unittest discover`, NOT pytest — pytest-style tests silently skip). Two packages: `broker_contract` (pure, dependency-free leaf) and `alphalens_pipeline.brokers.automanager` (daemon).

## Global Constraints

- **Pure oracle invariant:** an `ExitPolicy` NEVER emits an Action and holds NO per-position state. Peak/ratchet state lives in the daemon (impure) layer and the `ProtectionView` snapshot. Verbatim from spec §4.1/§4.3.
- **Single-resolution invariant (PR #972):** the policy is resolved ONCE in `build_default_deps` and cached on `LoopDeps`/`ProtectionView`; NO hot path (protection/placement pass) may call `resolve_exit_policy`. Enforced by `apps/alphalens-research/tests/brokers/test_no_exit_policy_sentinel.py` (ast-strips comments/docstrings). Adding a policy must keep that test green.
- **Additive-only frozen fields:** every new field on a frozen dataclass (`ProtectionView`, `LoopDeps`) is appended with a default so the ~20 existing construction sites stay source-compatible. Mirror the `exit_policy: ExitPolicy = field(default_factory=SetupStaticPolicy)` addition at `position_manager.py:478` / `control_loop.py:218`.
- **The stop moves UP only:** two independent guards, BOTH required — the new bot-side ratchet (never below the journal-folded last-trailed level) AND the shipped `clamp_reanchor_target` (never below `plan.stop_price` = brief floor). Verbatim from spec §4.4.
- **Default OFF, existing policies byte-identical:** `setup_static` and `atr_bracket_1p5` behaviour must not change; the new kwargs on `decide_reanchor` are additive with defaults; `trails=False` on both.
- **Tests are `unittest.TestCase`; hermetic only** (weekend — no market, no broker, no network). No probe of any kind in this plan; the one open Saxo mechanic is the Tor B SIM probe (spec §8), out of scope here.
- **No AI mentions in commits; Conventional Commits; DCO sign-off** `Signed-off-by: Kamil Pająk <kamilpajak@users.noreply.github.com>` (diacritic name, noreply email). The controller commits (implementers leave changes uncommitted).
- **Commands run from the workspace venv:** research tests via `.venv/bin/python -m unittest ...`; a worktree editing package code needs its OWN `uv sync`.

---

### Task 1: Pure trailing policy + widened oracle + registry entry

**Files:**
- Modify: `apps/alphalens-broker-contract/broker_contract/exit_geometry/levels.py` (add `chandelier_target`)
- Modify: `apps/alphalens-broker-contract/broker_contract/exit_geometry/policy.py` (widen `decide_reanchor`; add `trails`; add `TrailingAtrPolicy`)
- Modify: `apps/alphalens-broker-contract/broker_contract/exit_geometry/registry.py` (register `trailing_atr` in `resolve_exit_policy`)
- Test: `apps/alphalens-research/tests/brokers/test_trailing_atr_policy.py` (create)
- Test: `apps/alphalens-research/tests/brokers/test_exit_geometry_levels.py` (extend if present; else add to the policy test file)

**Interfaces:**
- Consumes: `ExitGeometryPolicy` (registry.py:24, has `stop_atr_mult`, `tp_atr_mult`, `tp_floor_frac`, `name`, `version`, `.levels(...)`); the shipped `AtrBracketPolicy`/`SetupStaticPolicy` (policy.py:37-82).
- Produces:
  - `chandelier_target(peak: float, atr: float, *, k: float) -> float | None` (pure; `None` on degenerate input).
  - `ExitPolicy` Protocol gains `trails: bool` and the widened `decide_reanchor(self, avg_price: float, atr: float, *, peak: float | None = None, last_price: float | None = None) -> float | None`.
  - `TrailingAtrPolicy` with attrs `name`, `version`, `applies_geometry=True`, `requires_amend_stop=True`, `trails=True`, `min_stop_distance_frac`, `activation_r: float`, `k_atr: float`, and `decide_placement_geometry` delegating to a wrapped `ExitGeometryPolicy` (mirror `AtrBracketPolicy`).
  - `resolve_exit_policy("trailing_atr")` returns a `TrailingAtrPolicy` instance.

**Design notes for the implementer:**
- `TrailingAtrPolicy` wraps an `ExitGeometryPolicy` exactly like `AtrBracketPolicy` (policy.py:54-82) for placement geometry — so its initial disaster stop + TP are the same bracket. Trailing differs ONLY in `decide_reanchor`.
- `decide_reanchor` for `TrailingAtrPolicy`: activation gate first — return `None` unless the position is in enough profit, measured as `peak >= avg_price + activation_r * (stop_atr_mult * atr)` (i.e. `activation_r` R-multiples of the initial risk distance; `activation_r=0.5` per spec `+0.5R`). Both `peak` and `avg_price`/`atr` must be finite and `> 0` (mirror the finite-guards in `AtrBracketPolicy.decide_reanchor`, policy.py:74-82). If armed, return `chandelier_target(peak, atr, k=self.k_atr)`.
- `SetupStaticPolicy.decide_reanchor` / `AtrBracketPolicy.decide_reanchor`: add the `*, peak=None, last_price=None` kwargs (ignored) — behaviour byte-identical. Add `trails: bool = False` to both.
- The Protocol default for `trails` cannot be a class attribute default in a `Protocol`; declare `trails: bool` on the Protocol (structural) and set the concrete default on each impl.
- Register in `resolve_exit_policy` (registry.py:65-81): add `"trailing_atr": TrailingAtrPolicy(resolve_policy("atr_bracket_1p5"), activation_r=0.5, k_atr=0.6)` to the `registry` dict (params are `[in_sample]`; pin them here). Import `TrailingAtrPolicy` alongside the existing lazy import at registry.py:72.

- [ ] **Step 1: Write failing tests for `chandelier_target`**

```python
# test_trailing_atr_policy.py
import math
import unittest
from broker_contract.exit_geometry.levels import chandelier_target

class TestChandelierTarget(unittest.TestCase):
    def test_target_is_peak_minus_k_atr(self):
        self.assertAlmostEqual(chandelier_target(110.0, 2.0, k=0.6), 108.8)

    def test_none_on_nonpositive_or_nonfinite(self):
        self.assertIsNone(chandelier_target(0.0, 2.0, k=0.6))
        self.assertIsNone(chandelier_target(110.0, 0.0, k=0.6))
        self.assertIsNone(chandelier_target(math.nan, 2.0, k=0.6))
        self.assertIsNone(chandelier_target(110.0, math.inf, k=0.6))

    def test_none_when_target_nonpositive(self):
        self.assertIsNone(chandelier_target(1.0, 100.0, k=0.6))  # 1 - 60 < 0
```

- [ ] **Step 2: Run tests — expect ImportError/fail**

Run: `.venv/bin/python -m unittest apps.alphalens-research.tests.brokers.test_trailing_atr_policy -v` (or `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.test_trailing_atr_policy -v`)
Expected: FAIL — `chandelier_target` not defined.

- [ ] **Step 3: Implement `chandelier_target`** in `levels.py`, mirroring the finite/positive guard style of `atr_bracket_levels` (levels.py:60-73):

```python
def chandelier_target(peak: float, atr: float, *, k: float) -> float | None:
    """Trailing-stop level for a long: ``peak - k*atr`` (ratchets up via the
    caller's peak). Returns ``None`` on any degenerate input or a non-positive
    target — never a bad stop."""
    for value in (peak, atr):
        if not math.isfinite(value) or value <= 0:
            return None
    target = peak - k * atr
    if not math.isfinite(target) or target <= 0:
        return None
    return target
```

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Write failing tests for `TrailingAtrPolicy` + `trails` flag + widened signature**

```python
from broker_contract.exit_geometry.policy import (
    AtrBracketPolicy, SetupStaticPolicy, TrailingAtrPolicy)
from broker_contract.exit_geometry.registry import resolve_policy, resolve_exit_policy

class TestTrailingAtrPolicy(unittest.TestCase):
    def _policy(self):
        return TrailingAtrPolicy(resolve_policy("atr_bracket_1p5"), activation_r=0.5, k_atr=0.6)

    def test_trails_flag_true_here_false_elsewhere(self):
        self.assertTrue(self._policy().trails)
        self.assertFalse(SetupStaticPolicy().trails)
        self.assertFalse(AtrBracketPolicy(resolve_policy("atr_bracket_1p5")).trails)

    def test_dark_before_activation(self):
        # risk = stop_atr_mult(1.5)*atr(2)=3; activation 0.5R => need peak >= avg+1.5
        self.assertIsNone(self._policy().decide_reanchor(100.0, 2.0, peak=101.0))

    def test_chandelier_once_armed(self):
        # peak 110 >= 100+1.5 armed => 110 - 0.6*2 = 108.8
        self.assertAlmostEqual(self._policy().decide_reanchor(100.0, 2.0, peak=110.0), 108.8)

    def test_none_without_peak(self):
        self.assertIsNone(self._policy().decide_reanchor(100.0, 2.0))

    def test_existing_policies_ignore_peak_bytewise(self):
        atr = AtrBracketPolicy(resolve_policy("atr_bracket_1p5"))
        self.assertEqual(
            atr.decide_reanchor(100.0, 2.0, peak=999.0),
            atr.decide_reanchor(100.0, 2.0),
        )
        self.assertIsNone(SetupStaticPolicy().decide_reanchor(100.0, 2.0, peak=999.0))

    def test_registry_resolves_trailing_atr(self):
        pol = resolve_exit_policy("trailing_atr")
        self.assertTrue(pol.trails)
        self.assertTrue(pol.requires_amend_stop)
        self.assertEqual(pol.name, "atr_bracket_1p5")  # geom name (mirror AtrBracketPolicy.name)
```

- [ ] **Step 6: Run — expect FAIL** (`TrailingAtrPolicy` undefined, `trails` missing).

- [ ] **Step 7: Implement** — add `trails: bool` to the Protocol (policy.py:22-34) and `trails = False`/`True` on impls; add the `*, peak=None, last_price=None` kwargs to all three `decide_reanchor`; add `TrailingAtrPolicy` (mirror `AtrBracketPolicy` for placement + name/version props); register in `resolve_exit_policy`.

- [ ] **Step 8: Run the new test file + the existing exit-geometry/policy suites — expect all PASS.**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.test_trailing_atr_policy -v` and re-run any existing `test_*exit*` suites under `tests/brokers/`.

- [ ] **Step 9: Commit** (controller): `feat(brokers): trailing_atr exit policy — Chandelier peak−k·ATR oracle + trails flag`

---

### Task 2: `_maybe_trail` pure arm + `ProtectionView` peak/ratchet inputs + trailed fold

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/position_manager.py` (new `ProtectionView` fields; `_maybe_trail`; wire into `_reconcile_long`)
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py` (`_fold_trailed_markers`; populate the new `ProtectionView` fields in `build_protection_view`)
- Test: `apps/alphalens-research/tests/brokers/test_maybe_trail.py` (create)

**Interfaces:**
- Consumes: `TrailingAtrPolicy` + `policy.trails` (Task 1); `clamp_reanchor_target` (levels.py:76); `AmendStop`, `Position`, `PlannedExit`, `OrderState`, `_sole_standalone_stop`, `_SIDE`, `_exit_amend_ref` (position_manager.py, already used by `_maybe_reanchor`); the `reanchored_by_uic` pattern (`_fold_reanchored_markers`, control_loop.py:2420).
- Produces:
  - `ProtectionView` new trailing fields (appended, defaults): `peak_by_uic: Mapping[int, float] = field(default_factory=dict)`, `last_price_by_uic: Mapping[int, float] = field(default_factory=dict)`, `trailed_stop_by_uic: Mapping[int, float] = field(default_factory=dict)`.
  - `_maybe_trail(uic, pos, plan, legs, view) -> AmendStop | None`.
  - `_fold_trailed_markers(journal_lines) -> dict[int, float]` (control_loop.py) folding a `trailed` marker `{kind:"trailed", uic, level, ts}` to uic→latest level (mirror `_fold_reanchored_markers`).

**Design notes for the implementer (mirror the shipped `_maybe_reanchor` at position_manager.py:626-729):**
- `_maybe_trail` guards, in order: `policy = view.exit_policy`; return `None` unless `policy.trails`. Require `plan.reanchor is not None` (carries the geometry shadow stamp incl. `atr`), finite `avg_price = pos.avg_price > 0`, finite `atr = plan.reanchor.atr > 0`, `sole = _sole_standalone_stop(legs) is not None`, `uic not in view.amend_recently_failed`. (Same guards as `_maybe_reanchor` lines 673-685.)
- Read `peak = view.peak_by_uic.get(uic)`; if `peak` is `None` or not finite or `<= 0`, return `None` (feed veto / no peak yet).
- `proposed = policy.decide_reanchor(pos.avg_price, atr, peak=peak, last_price=view.last_price_by_uic.get(uic))`; if `None`, return `None` (dark before activation).
- **Ratchet:** `floor = view.trailed_stop_by_uic.get(uic)`; if `floor is not None` and `proposed <= floor + _TRAIL_STEP_EPS`, return `None` (must clear a coarse step above the last trailed level; `_TRAIL_STEP_EPS` is the coarse step — a module constant, `[in_sample]`, e.g. `0.02`). This is the never-down guarantee vs the live trailing history.
- **Clamp:** `clamped = clamp_reanchor_target(plan.stop_price, proposed, anchor_price=pos.avg_price, min_distance_frac=policy.min_stop_distance_frac)`; if `None`, log `"trail refused (below brief floor)"` and return `None` (mirror the logging at position_manager.py:699-706).
- Emit `AmendStop(uic, _SIDE, sole.order_id, sole.order_type or "StopIfTraded", pos.quantity, clamped, _exit_amend_ref(plan.entry_crid, plan.next_amend_seq()), reason="trail", reanchor_avg_price=pos.avg_price)` — same shape as `_maybe_reanchor` (lines 719-729) except `reason="trail"`.
- Wire into `_reconcile_long` (position_manager.py:782): select by the `trails` flag so the two arms never both fire:
  ```python
  if view.exit_policy.trails:
      action = _maybe_trail(uic, pos, plan, legs, view)
  else:
      action = _maybe_reanchor(uic, pos, plan, legs, view)
  if action is not None:
      return [action]
  ```
- In `build_protection_view` (control_loop.py:2406-2425) add `trailed_stop_by_uic=_fold_trailed_markers(journal_lines)`; `peak_by_uic` / `last_price_by_uic` default empty here (Task 4 injects real values through a new param).
- `_fold_trailed_markers`: mirror `_fold_reanchored_markers` exactly (latest-by-ts per uic), reading `line["level"]` instead of the avg-price.

- [ ] **Step 1: Write failing tests** for `_maybe_trail` (hand-build a `ProtectionView`; no broker/feed):

```python
# test_maybe_trail.py — assert: dark before activation; fires AmendStop once armed;
# ratchet drops a proposal <= floor+eps; clamp returns None below brief floor;
# peak None -> None; non-trailing policy -> _maybe_trail returns None (trails guard).
```
Cover at minimum: armed → `AmendStop.target ≈ peak−k·atr`, `reason=="trail"`, `amount==pos.quantity`; a `trailed_stop_by_uic[uic]` just below the proposal → `None`; a `plan.stop_price` above the proposal → `None` + the log; `peak_by_uic` empty → `None`.

- [ ] **Step 2: Run — expect FAIL** (`_maybe_trail` undefined; new fields missing).

- [ ] **Step 3: Implement** the `ProtectionView` fields, `_maybe_trail`, `_fold_trailed_markers`, and the `_reconcile_long` branch per the design notes.

- [ ] **Step 4: Run the new test + the FULL automanager suite** — expect all PASS (byte-identical for non-trailing policies).

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest discover -s tests/brokers -t . -q`

- [ ] **Step 5: Commit:** `feat(brokers): _maybe_trail arm — ratchet + never-below-brief-floor over injected peak`

---

### Task 3: `PeakTracker` on `LoopDeps` + per-tick peak update from the feed (with restart reset)

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py` (`peak_tracker` field on `LoopDeps`; `_update_peaks` helper)
- Test: `apps/alphalens-research/tests/brokers/test_peak_tracker.py` (create)

**Interfaces:**
- Consumes: `PriceFeed` / `PricePoint` (`broker_contract.price_feed`); `deps.live_exits_feed_factory` / `_default_live_exits_feed_factory` (control_loop.py:543); `_position_uic` (control_loop.py); the mutable-dict-on-frozen-deps pattern (`oco_lag_counts`, control_loop.py:191).
- Produces:
  - `LoopDeps.peak_tracker: dict[int, float] = field(default_factory=dict)` (uic → high-water; mutable dict on frozen deps, carried across ticks).
  - `_update_peaks(deps, long_positions) -> tuple[dict[int, float], dict[int, float]]` returning `(peak_by_uic, last_price_by_uic)`.

**Design notes:**
- `_update_peaks` builds `uic_to_ticker` from `long_positions` (mirror control_loop.py:589-594), gets `feed = (deps.live_exits_feed_factory or _default_live_exits_feed_factory)(uic_to_ticker)`, and for each long uic reads `point = feed.latest(uic)`.
  - `point is None` (stream veto) or non-positive: do NOT update the tracker; do NOT emit a `peak`/`last_price` for that uic this tick (so `_maybe_trail` vetoes — no move on a stale feed).
  - else: `price = point.mid` (fallback `point.bid`; use the same field the live-exits engine consumes — check `live_exit_engine.py:160` for the exact attribute and mirror it); `deps.peak_tracker[uic] = max(deps.peak_tracker.get(uic, price), price)`; set `peak_by_uic[uic] = deps.peak_tracker[uic]`, `last_price_by_uic[uic] = price`.
- **Restart reset is automatic and safe:** a fresh daemon starts with an empty `peak_tracker`, so the first post-restart tick seeds `peak = max(price, price) = price` (spec §4.4 — never invents a higher past peak). The ratchet floor (`trailed_stop_by_uic`, Task 2) is journal-folded and survives restart, so a reset peak can never loosen the stop. No explicit reset code needed — assert this in a test.
- Prune stale uics: drop `peak_tracker` keys not in the current `long_positions` (a closed position must not resurrect a stale peak if the uic is re-picked). Do this at the end of `_update_peaks`.

- [ ] **Step 1: Write failing tests** with a fake `PriceFeed` (a tiny stub returning scripted `PricePoint`s / `None`):
  - peak is monotone across ticks (rise then fall → peak stays at the high);
  - a `None` feed point leaves the peak unchanged and omits the uic from the returned maps;
  - a fresh `peak_tracker` (restart) seeds peak to the first observed price;
  - a uic no longer in `long_positions` is pruned from `peak_tracker`.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** `peak_tracker` field + `_update_peaks`.

- [ ] **Step 4: Run — expect PASS** + full automanager suite green.

- [ ] **Step 5: Commit:** `feat(brokers): PeakTracker + per-tick high-water update from the price feed`

---

### Task 4: Wire peaks into the protection pass + persist the trailed marker on confirmed amend

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py` (`build_protection_view` new params; `_run_protection_pass` peak injection; `trailed` marker write on confirmed trail amend)
- Test: `apps/alphalens-research/tests/brokers/test_trail_wiring.py` (create)

**Interfaces:**
- Consumes: `_update_peaks` (Task 3); `build_protection_view` (control_loop.py:2355, bound via `functools.partial(..., exit_policy=exit_policy)` at control_loop.py:1000); the confirmed-amend marker-write path for `reanchored` (find where `_maybe_reanchor`'s success writes the `reanchored` journal marker — the executor/`execute_protection` path referenced at position_manager.py:291 "latch ... ONLY on confirmed PATCH success"); `_fold_trailed_markers` (Task 2).
- Produces:
  - `build_protection_view(..., peak_by_uic=None, last_price_by_uic=None, ...)` populating the Task-2 `ProtectionView` fields (default empty dicts → today's dark path).
  - `_run_protection_pass` calls `_update_peaks` ONLY when `deps.exit_policy.trails`, then threads the maps into `build_protection_view`.
  - On a confirmed `AmendStop` with `reason=="trail"`, append a `trailed` journal marker `{kind:"trailed", uic, level, ts}` (mirror the `reanchored` marker write; the level = the amend target). This is BOTH the ratchet-persistence AND the telemetry record (spec §4.5 — the trailed marker carries `level`, and the caller adds `peak`/`last_price`/`k` for the later `/edge` lens; a fill-oriented `_fire_telemetry` does not fit a stop-amend, so the journal marker IS the trailing telemetry substrate).

**Design notes:**
- `deps.build_protection_view` is a `functools.partial` (control_loop.py:1000). `_run_protection_pass` calls `deps.build_protection_view(deps.broker, records)`. To inject peaks, compute them in `_run_protection_pass` and pass as kwargs: `deps.build_protection_view(deps.broker, records, peak_by_uic=..., last_price_by_uic=...)` (the partial already binds `exit_policy`; extra kwargs pass through). Guard: only fetch when `deps.exit_policy.trails` (default policy = no feed fetch, zero new behaviour).
- To get `long_positions` for `_update_peaks` without a second broker read, prefer reusing the positions already read this tick. `_run_protection_pass` currently lets `build_protection_view` read positions internally. Two acceptable options — pick the smaller diff and note it:
  1. `_update_peaks` does its own `deps.broker.get_long_positions()` inside its own `BrokerError` boundary (mirrors `_run_live_exits_pass` at control_loop.py:573). Simplest; costs one extra broker read per tick when trailing is ON. **Acceptable for the first cut** (note it as a known minor inefficiency to fold into a shared per-tick read later).
  2. Refactor `build_protection_view` to accept pre-read positions — larger blast radius; DEFER.
- The `trailed` marker write mirrors the `reanchored` write exactly. Read the existing `reanchored` marker-write site (grep `"reanchored"` under `automanager/`) and add a parallel branch keyed on `reason=="trail"` writing `kind:"trailed"` with `level=action.target`.
- Telemetry: include `peak` and `last_price` in the `trailed` marker (available in `_run_protection_pass` from `_update_peaks`; thread them to the marker writer, or write the marker from `_run_protection_pass` after the executor confirms success — mirror however `reanchored` is written).

- [ ] **Step 1: Write failing tests** (hermetic, fake broker + fake feed):
  - trailing policy ON + a rising feed → `_run_protection_pass` produces an `AmendStop(reason="trail")` and a `trailed` marker is appended with the target level;
  - default policy (`atr_bracket_1p5`) → NO `_update_peaks` call / NO feed fetch (assert the fake feed factory was never invoked) → byte-identical to today;
  - a `trailed` marker written on tick N is folded into `trailed_stop_by_uic` and vetoes a non-stepping proposal on tick N+1 (ratchet across ticks).

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** the wiring + marker write.

- [ ] **Step 4: Run — expect PASS** + full automanager suite + `test_no_exit_policy_sentinel` green (no hot-path resolve introduced).

- [ ] **Step 5: Commit:** `feat(brokers): wire high-water peaks into the protection pass + persist the trailed marker`

---

### Task 5: Flag path end-to-end + hermetic acceptance

**Files:**
- Test: `apps/alphalens-research/tests/brokers/test_trailing_acceptance.py` (create)
- Modify (only if a gap surfaces): `control_loop.py` `build_default_deps` (the `requires_amend_stop → SupportsAmendStop` fail-fast at control_loop.py:959 already covers `trailing_atr`; add a test, not code, unless it does not fire).

**Interfaces:**
- Consumes: everything above; `build_default_deps` (control_loop.py:877); `_exit_policy()` reading `ALPHALENS_BROKER_EXIT_POLICY` (position_manager.py:415); `run_once`.

**Design notes:**
- The env flag path already exists: `_exit_policy()` reads `ALPHALENS_BROKER_EXIT_POLICY`; `build_default_deps` resolves it (control_loop.py:922) and fail-fasts if `requires_amend_stop` and the broker lacks `SupportsAmendStop` (control_loop.py:959). Task 1 registered `trailing_atr`, so setting the env to `trailing_atr` now yields a `TrailingAtrPolicy` on `LoopDeps`. No new production code expected here — this task PROVES the whole path.
- Acceptance scenario (fake broker with `SupportsAmendStop`, fake feed, in-memory journal): place a covered long with a geometry `plan.reanchor`; drive the feed UP across several `run_once` ticks; assert the resting stop is amended UP in coarse steps (multiple `AmendStop(reason="trail")`), never down; then drive the feed DOWN and assert NO further amends; simulate a restart (fresh `peak_tracker`) and assert the stop does not loosen (ratchet from the folded `trailed` marker holds).
- Byte-identical guard: the same scenario under `atr_bracket_1p5` produces the one-shot reanchor path (or NoOp), never a `trail` marker.

- [ ] **Step 1: Write the acceptance test** (rising → stepped up; falling → none; restart → no loosen; default policy → no trail).

- [ ] **Step 2: Run — expect FAIL** (or PASS if all wiring is correct; if PASS, still verify each assertion is meaningful, not vacuous).

- [ ] **Step 3: Fix any wiring gap surfaced** (only if a real gap; otherwise no production change).

- [ ] **Step 4: Run the FULL automanager + broker-contract + module-deps suites + ruff** — all green.

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest discover -s tests/brokers -t . -q` ; `cd apps/alphalens-research && ../../.venv/bin/python -m unittest discover -s tests -t . -q` (module-deps) ; `.venv/bin/ruff check`.

- [ ] **Step 5: Commit:** `test(brokers): end-to-end hermetic acceptance for trailing_atr (rising/falling/restart)`

---

## Post-plan: PR + review

- Push the branch, open the PR (body: what/why/how per `~/Developer/CLAUDE.md`; a **Known issues / Behaviour notes** section listing: default OFF; bot-amend frozen while bot down (native deferred); the second per-tick broker read when trailing ON; the open Saxo upward-level-amend mechanic → Tor B SIM probe 2026-08-10 15:30).
- Mandatory zen pre-merge: `mcp__zen__codereview` with `deepseek/deepseek-v4-pro` + `thinking_mode="high"`; apply findings as additional commits.
- CI green on the latest commit (research unittest, SonarCloud, ruff) → squash-merge.
- Post-merge: update memory `project_trailing_execution_design_2026_08_07.md` (Tor A DONE, PR#, Tor B probe pending) and set the spec/plan Status to SHIPPED.

## Self-Review (against the spec)

- **Spec coverage:** §1 goal → all tasks; §2 bot-amend rationale → Task 2 (no cancel+replace); §3 real-code anchors → cited per task; §4.1 peak ownership (a) → Task 3; §4.2 `_maybe_trail` → Task 2; §4.3 policy widening + `trails` → Task 1; §4.4 ratchet + clamp + restart → Task 2 (ratchet+clamp) + Task 3 (restart) + Task 5 (proof); §4.5 flag + telemetry → Task 4 (trailed marker) + Task 5 (flag path); §5 files → covered; §6 tests → each task's tests + Task 5 acceptance; §8 open mechanic + §9 Tor B → out of scope, carried to the PR Known-issues + memory.
- **Telemetry refinement noted:** spec §4.5 said "reuse `_fire_telemetry`"; the plan implements trailing telemetry as the `trailed` journal marker instead, because `_fire_telemetry` is fill-oriented and a stop-amend has no fill. Reconcile the spec's §4.5 wording post-merge.
- **Type consistency:** `decide_reanchor(avg_price, atr, *, peak=None, last_price=None)` used identically in Tasks 1-2; `trails: bool` consistent; `AmendStop(reason="trail")` consistent Tasks 2/4; `peak_by_uic`/`last_price_by_uic`/`trailed_stop_by_uic` names consistent Tasks 2-4.
- **Placeholder scan:** the `[in_sample]` params (`activation_r=0.5`, `k_atr=0.6`, `_TRAIL_STEP_EPS≈0.02`) are pinned concrete values, not placeholders; they are tunable knobs, not gaps.
