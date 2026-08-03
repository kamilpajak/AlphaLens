# Exit-policy registry + stateful envelope — implementation plan (INC-1)

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the SIM exit-logic layer into a named `ExitPolicy` registry behind a stateful safety envelope, so today's deterministic policies re-express through one abstraction and a future ML policy plugs in as one more registry entry — zero ML, zero RNG in this increment.

**Architecture:** A dependency-free `ExitPolicy` Protocol + two concrete policies live in the `broker_contract` leaf. The daemon resolves a policy by NAME **exactly once at startup** and caches the instance on `LoopDeps` + `ProtectionView`; the hot paths (`_journal_tier`, `_maybe_reanchor`) read the cached instance and never re-resolve. A pure, stateful `clamp_reanchor_target` enforces the economic content of a reanchor (min-distance floor + never-below-brief-floor vs the planned disaster stop). All never-naked / never-oversell / realized-qty invariants stay 100% inside the untouched `reconcile_protection` diff engine; the policy is a pure price oracle that never emits an order Action.

**Tech Stack:** Python 3.12, `broker_contract` (stdlib-only leaf), `alphalens_pipeline.brokers.automanager`, `unittest` (research tests).

**Design memo:** `docs/research/nondeterministic_exit_policy_design_2026_08_03.md` (DRAFT). INC-0 (`setup_builder_config_version`) is ALREADY shipped — this plan is INC-1 only.

## Adversarial review outcome (zen `deepseek-v4-pro`, high — 2026-08-03)

The first draft resolved the policy per-tick in the hot path. deepseek + independent code-trace found this **CRITICAL**: `resolve_exit_policy` raises `ValueError` on an unknown name, and `_maybe_reanchor` runs inside `reconcile_protection` (control_loop.py:412) which is a single unwrapped pure call — a raise there aborts the whole protection pass and starves EVERY position that tick (violates INV-3 unconditional protection + INV-4 fault isolation). Fixes folded into this plan:
- **Resolve the policy ONCE at startup, cache on `LoopDeps` + `ProtectionView`** (Task 3 below). Removes the crash path, the per-tick rebuild waste, and the lazy-import-in-hot-path. This is why Task 3 now precedes the placement/reanchor wiring.
- **`min_stop_distance_frac` is a policy attribute**, not a module constant (keeps the envelope policy-agnostic).
- **Semantics documented as "never-below-brief-floor"**, not literal "never-loosen-vs-live-stop" (`OrderState` carries no live stop price).
- **Divergence telemetry is `logger.info` for INC-1**, with a `TODO` for the append-only journal-stamp follow-up.
Confirmed sound, no change: clamp inequality direction, the (necessary) lazy import in the resolver, byte-identical placement, degenerate-input handling.

## Global Constraints

- **SIM-only.** No live-money path changes. Placement stays gated on `ALPHALENS_BROKER_ALLOW_ORDERS=1`.
- **TDD, red→green.** Research tests MUST subclass `unittest.TestCase` (pytest-style bare functions are silently skipped in CI).
- **English-only** in code, comments, docstrings.
- **`broker_contract` is a dependency-free leaf** — `policy.py` and `levels.py` import only stdlib + sibling `broker_contract` modules. No `alphalens_pipeline` import from `broker_contract`.
- **Resolve the policy ONCE at startup, never in the hot path.** `build_default_deps` resolves `resolve_exit_policy(_exit_policy())` a single time (fail-fast `ValueError` at startup on a bad name); the instance is cached on `LoopDeps` and threaded onto `ProtectionView`. `_journal_tier` and `_maybe_reanchor` read the cached instance — NO `resolve_exit_policy` / `_exit_policy()` call may appear inside `reconcile_protection` or the placement drain.
- **Byte-identical behavior EXCEPT the one accepted break:** `setup_static` stays fully inert (geometry dark, no reanchor). `atr_bracket_1p5` reproduces today's placement + reanchor EXCEPT that a reanchor may no longer move the stop below `plan.stop_price` (never-below-brief-floor, Decision 1) — this is the intended divergence, live now.
- **All safety-acceptance tests stay green:** `test_every_position_protected`, `test_no_oversell`, `test_safety_rails`, `test_resilience` (INV-1…INV-10).
- **Policy selection is name→registry→object.** No surviving `!= "setup_static"` / `== "setup_static"` test after this increment. Unknown policy name raises `ValueError` at STARTUP only.
- **DCO sign-off** on every commit (`Kamil Pająk <kamilpajak@users.noreply.github.com>`); no AI mention in commit messages.

---

### Task 1: `ExitPolicy` Protocol + concrete policies + resolver

**Files:**
- Create: `apps/alphalens-broker-contract/broker_contract/exit_geometry/policy.py`
- Modify: `apps/alphalens-broker-contract/broker_contract/exit_geometry/registry.py` (add `resolve_exit_policy`)
- Modify: `apps/alphalens-broker-contract/broker_contract/exit_geometry/__init__.py` (export new names)
- Test: `apps/alphalens-research/tests/exit_geometry/test_policy.py`

**Interfaces:**
- Consumes: `ExitGeometryPolicy` + `resolve_policy` (existing, `registry.py`); `atr_bracket_levels` (existing, `levels.py`).
- Produces:
  - `class ExitPolicy(Protocol)` with read-only attrs `name: str`, `version: int`, `applies_geometry: bool`, `requires_amend_stop: bool`, `min_stop_distance_frac: float`, and methods
    `decide_placement_geometry(self, blended: float, atr: float, *, ceiling_price: float | None) -> tuple[float, float] | None`
    `decide_reanchor(self, avg_price: float, atr: float) -> float | None`.
  - `SetupStaticPolicy` (inert): `applies_geometry=False`, `requires_amend_stop=False`, `min_stop_distance_frac=0.0`, both methods return `None`.
  - `AtrBracketPolicy(geom: ExitGeometryPolicy)`: `applies_geometry=True`, `requires_amend_stop=True`, `min_stop_distance_frac=0.002`; placement → `geom.levels(...)`; reanchor → `avg_price - geom.stop_atr_mult * atr` (or `None` on degenerate).
  - `resolve_exit_policy(name: str) -> ExitPolicy` — `{"setup_static": SetupStaticPolicy(), "atr_bracket_1p5": AtrBracketPolicy(resolve_policy("atr_bracket_1p5"))}`; unknown → `ValueError`.

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-research/tests/exit_geometry/test_policy.py
import math
import unittest

from broker_contract.exit_geometry.policy import AtrBracketPolicy, SetupStaticPolicy
from broker_contract.exit_geometry.registry import resolve_exit_policy, resolve_policy


class SetupStaticPolicyTest(unittest.TestCase):
    def test_is_inert(self):
        p = SetupStaticPolicy()
        self.assertEqual(p.name, "setup_static")
        self.assertFalse(p.applies_geometry)
        self.assertFalse(p.requires_amend_stop)
        self.assertIsNone(p.decide_placement_geometry(100.0, 2.0, ceiling_price=None))
        self.assertIsNone(p.decide_reanchor(100.0, 2.0))


class AtrBracketPolicyTest(unittest.TestCase):
    def setUp(self):
        self.p = AtrBracketPolicy(resolve_policy("atr_bracket_1p5"))

    def test_flags(self):
        self.assertEqual(self.p.name, "atr_bracket_1p5")
        self.assertTrue(self.p.applies_geometry)
        self.assertTrue(self.p.requires_amend_stop)
        self.assertGreater(self.p.min_stop_distance_frac, 0.0)

    def test_placement_matches_raw_levels(self):
        want = resolve_policy("atr_bracket_1p5").levels(100.0, 2.0, ceiling_price=None)
        self.assertEqual(self.p.decide_placement_geometry(100.0, 2.0, ceiling_price=None), want)

    def test_reanchor_target(self):
        self.assertTrue(math.isclose(self.p.decide_reanchor(101.0, 2.0), 101.0 - 3.0))

    def test_reanchor_degenerate_is_none(self):
        self.assertIsNone(self.p.decide_reanchor(101.0, 0.0))
        self.assertIsNone(self.p.decide_reanchor(101.0, float("nan")))
        self.assertIsNone(self.p.decide_reanchor(0.0, 2.0))


class ResolveExitPolicyTest(unittest.TestCase):
    def test_known_names(self):
        self.assertIsInstance(resolve_exit_policy("setup_static"), SetupStaticPolicy)
        self.assertIsInstance(resolve_exit_policy("atr_bracket_1p5"), AtrBracketPolicy)

    def test_unknown_raises_valueerror(self):
        with self.assertRaises(ValueError):
            resolve_exit_policy("nope")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.exit_geometry.test_policy -v`
Expected: FAIL — `ModuleNotFoundError: broker_contract.exit_geometry.policy`.

- [ ] **Step 3: Write minimal implementation**

```python
# apps/alphalens-broker-contract/broker_contract/exit_geometry/policy.py
"""Behavioral exit-policy abstraction (name -> placement + reanchor decisions).

An ``ExitPolicy`` is a PURE price oracle: it proposes exit levels and a reanchor
target and NEVER emits a broker Action. The daemon resolves ONE by name at
startup and routes placement + reanchor through it, so adding a policy (a future
ML policy included) is a new registry entry, not a new call-site.
``SetupStaticPolicy`` is the inert/null policy that keeps the brief's static
disaster stop/TP and never reanchors; ``AtrBracketPolicy`` wraps a numeric
``ExitGeometryPolicy``. ``min_stop_distance_frac`` lets the reanchor envelope
stay policy-agnostic (a future close-stop policy sets its own floor).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from broker_contract.exit_geometry.registry import ExitGeometryPolicy


@runtime_checkable
class ExitPolicy(Protocol):
    name: str
    version: int
    applies_geometry: bool
    requires_amend_stop: bool
    min_stop_distance_frac: float

    def decide_placement_geometry(
        self, blended: float, atr: float, *, ceiling_price: float | None
    ) -> tuple[float, float] | None: ...

    def decide_reanchor(self, avg_price: float, atr: float) -> float | None: ...


@dataclass(frozen=True)
class SetupStaticPolicy:
    name: str = "setup_static"
    version: int = 1
    applies_geometry: bool = False
    requires_amend_stop: bool = False
    min_stop_distance_frac: float = 0.0

    def decide_placement_geometry(
        self, blended: float, atr: float, *, ceiling_price: float | None
    ) -> tuple[float, float] | None:
        return None

    def decide_reanchor(self, avg_price: float, atr: float) -> float | None:
        return None


@dataclass(frozen=True)
class AtrBracketPolicy:
    geom: ExitGeometryPolicy
    applies_geometry: bool = True
    requires_amend_stop: bool = True
    min_stop_distance_frac: float = 0.002  # hair-trigger floor; never binds 1.5x ATR

    @property
    def name(self) -> str:
        return self.geom.name

    @property
    def version(self) -> int:
        return self.geom.version

    def decide_placement_geometry(
        self, blended: float, atr: float, *, ceiling_price: float | None
    ) -> tuple[float, float] | None:
        return self.geom.levels(blended, atr, ceiling_price=ceiling_price)

    def decide_reanchor(self, avg_price: float, atr: float) -> float | None:
        if not math.isfinite(avg_price) or avg_price <= 0:
            return None
        if not math.isfinite(atr) or atr <= 0:
            return None
        target = avg_price - self.geom.stop_atr_mult * atr
        if not math.isfinite(target) or target <= 0:
            return None
        return target
```

```python
# apps/alphalens-broker-contract/broker_contract/exit_geometry/registry.py (append)

def resolve_exit_policy(name: str) -> "ExitPolicy":
    """Resolve a behavioral ExitPolicy by name (fail-fast on unknown).

    CALL ONCE AT STARTUP — never inside the protection pass (a ValueError here
    would starve the unconditional protection). Lazy import of ``policy`` avoids
    a module import cycle (policy.py imports ExitGeometryPolicy from this module).
    """
    from broker_contract.exit_geometry.policy import (
        AtrBracketPolicy,
        ExitPolicy,
        SetupStaticPolicy,
    )

    registry: dict[str, ExitPolicy] = {
        "setup_static": SetupStaticPolicy(),
        "atr_bracket_1p5": AtrBracketPolicy(resolve_policy("atr_bracket_1p5")),
    }
    try:
        return registry[name]
    except KeyError:
        raise ValueError(f"unknown exit policy: {name!r}") from None
```

Add `resolve_exit_policy`, `ExitPolicy`, `SetupStaticPolicy`, `AtrBracketPolicy` to `exit_geometry/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.exit_geometry.test_policy -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-broker-contract/broker_contract/exit_geometry/policy.py \
        apps/alphalens-broker-contract/broker_contract/exit_geometry/registry.py \
        apps/alphalens-broker-contract/broker_contract/exit_geometry/__init__.py \
        apps/alphalens-research/tests/exit_geometry/test_policy.py
git commit -s -m "feat(brokers): ExitPolicy protocol + named registry (setup_static inert, atr_bracket_1p5)"
```

---

### Task 2: Stateful reanchor envelope (`clamp_reanchor_target`)

**Files:**
- Modify: `apps/alphalens-broker-contract/broker_contract/exit_geometry/levels.py` (add `clamp_reanchor_target`)
- Modify: `apps/alphalens-broker-contract/broker_contract/exit_geometry/__init__.py` (export)
- Test: `apps/alphalens-research/tests/exit_geometry/test_levels.py` (extend)

**Interfaces:**
- Produces: `clamp_reanchor_target(prior_stop: float, proposed_target: float, *, anchor_price: float, min_distance_frac: float) -> float | None`.
  - Returns `None` (meaning "do NOT reanchor — keep the resting stop") on any degenerate input OR when the clamped target would drop below `prior_stop`.
  - `prior_stop` is the placement-time planned disaster stop (`plan.stop_price`) = the brief disaster floor.

**Semantics (never-below-brief-floor, NOT never-loosen-vs-live-stop — `OrderState` has no live stop price; documented deliberately):**
1. Degrade: if any of `prior_stop`, `proposed_target`, `anchor_price` is non-finite or ≤ 0 → `None`.
2. Min-distance floor: `floor_price = anchor_price * (1 - min_distance_frac)` is the CLOSEST the stop may sit to the anchor; if `proposed_target > floor_price` → clamp DOWN to `floor_price` (pushes a too-close stop to a safer distance — hair-trigger guard).
3. Never-below-brief-floor: if the (floored) target `< prior_stop` → `None` (refuse to move the stop below the brief disaster level).
4. Else return the (floored) target.

- [ ] **Step 1: Write the failing test**

```python
# append to apps/alphalens-research/tests/exit_geometry/test_levels.py
import math
import unittest

from broker_contract.exit_geometry.levels import clamp_reanchor_target


class ClampReanchorTargetTest(unittest.TestCase):
    def test_tighter_target_passes(self):
        out = clamp_reanchor_target(98.0, 99.0, anchor_price=101.0, min_distance_frac=0.002)
        self.assertTrue(math.isclose(out, 99.0))

    def test_below_brief_floor_refused(self):
        self.assertIsNone(
            clamp_reanchor_target(98.0, 97.0, anchor_price=101.0, min_distance_frac=0.002)
        )

    def test_too_close_clamped_to_floor(self):
        out = clamp_reanchor_target(98.0, 100.9, anchor_price=101.0, min_distance_frac=0.002)
        self.assertTrue(math.isclose(out, 101.0 * (1 - 0.002)))

    def test_degenerate_inputs_return_none(self):
        self.assertIsNone(clamp_reanchor_target(0.0, 99.0, anchor_price=101.0, min_distance_frac=0.002))
        self.assertIsNone(clamp_reanchor_target(98.0, float("nan"), anchor_price=101.0, min_distance_frac=0.002))
        self.assertIsNone(clamp_reanchor_target(98.0, 99.0, anchor_price=-1.0, min_distance_frac=0.002))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.exit_geometry.test_levels.ClampReanchorTargetTest -v`
Expected: FAIL — `ImportError: cannot import name 'clamp_reanchor_target'`.

- [ ] **Step 3: Write minimal implementation**

```python
# apps/alphalens-broker-contract/broker_contract/exit_geometry/levels.py (append)

def clamp_reanchor_target(
    prior_stop: float,
    proposed_target: float,
    *,
    anchor_price: float,
    min_distance_frac: float,
) -> float | None:
    """Economic safety envelope for a reanchored disaster stop (memo section 3.1).

    ``prior_stop`` is the placement-time planned disaster stop (the brief
    disaster floor). Returns ``None`` = "do NOT reanchor — leave the resting stop
    where it is" on any degenerate input or when the target would drop below
    ``prior_stop``. NOTE: this enforces NEVER-BELOW-BRIEF-FLOOR, not
    never-loosen-vs-the-current-live-stop (``OrderState`` carries no stop price).
    The min-distance floor caps how close the stop may sit to ``anchor_price``
    (a too-close proposal is pushed FARTHER from price); it is chosen so it never
    binds the 1.5x-ATR policy and exists mainly for a future stochastic policy.
    """
    for value in (prior_stop, proposed_target, anchor_price):
        if not math.isfinite(value) or value <= 0:
            return None
    floor_price = anchor_price * (1.0 - min_distance_frac)
    target = min(proposed_target, floor_price)
    if target < prior_stop:
        return None
    return target
```

Export `clamp_reanchor_target` in `exit_geometry/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.exit_geometry.test_levels -v` → PASS (existing + new).

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-broker-contract/broker_contract/exit_geometry/levels.py \
        apps/alphalens-broker-contract/broker_contract/exit_geometry/__init__.py \
        apps/alphalens-research/tests/exit_geometry/test_levels.py
git commit -s -m "feat(brokers): stateful reanchor envelope (min-distance floor + never-below-brief-floor)"
```

---

### Task 3: Resolve the policy ONCE at startup + cache on `LoopDeps` + `ProtectionView` + capability gate

> This task lands BEFORE the placement/reanchor wiring so the cached instance exists when Tasks 4-5 read it. It removes every hot-path `resolve_exit_policy` / `_exit_policy()` call (the CRITICAL fix from the adversarial review).

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py` — `LoopDeps` (add `exit_policy: ExitPolicy` field); `build_default_deps:701-742` (resolve once, cache, use it for the capability gate); the `build_protection_view` composition so `ProtectionView.exit_policy` is stamped from `deps.exit_policy`.
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/position_manager.py` — `ProtectionView` (add `exit_policy: ExitPolicy` field, defaulted so existing test constructions stay source-compatible).
- Test: `apps/alphalens-research/tests/brokers/automanager/test_build_default_deps.py` (locate the existing capability-gate test module first; extend it).

**Interfaces:**
- Produces: `LoopDeps.exit_policy: ExitPolicy` (resolved once); `ProtectionView.exit_policy: ExitPolicy`.
- Change 1 — `build_default_deps`: after `broker = get_default_broker()`, add `exit_policy = resolve_exit_policy(_exit_policy())` (this is now the single resolution site; an unknown env name fails fast here). Replace the capability gate `if _exit_policy() != "setup_static" and not isinstance(broker, SupportsAmendStop):` with `if exit_policy.requires_amend_stop and not isinstance(broker, SupportsAmendStop):`. Store `exit_policy` on the returned `LoopDeps` and thread it into the `build_protection_view` closure.
- Change 2 — `ProtectionView`: add `exit_policy: ExitPolicy` (import the Protocol type). `build_protection_view` sets it from the injected `deps.exit_policy`.

- [ ] **Step 1: Write the failing test** — a `unittest.TestCase` asserting: (a) `atr_bracket_1p5` + broker WITHOUT `SupportsAmendStop` → `BrokerCapabilityError`; (b) `setup_static` + same broker → no raise; (c) `atr_bracket_1p5` + broker WITH `SupportsAmendStop` → no raise and `deps.exit_policy.name == "atr_bracket_1p5"`; (d) unknown env name → `ValueError` at `build_default_deps` (startup), NOT at tick time. Reuse the existing capability-gate fixture/broker doubles.

- [ ] **Step 2: Run test to verify it fails** — Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.automanager.test_build_default_deps -v` → FAIL (no `deps.exit_policy`; unknown-name path not yet failing at startup).

- [ ] **Step 3: Write minimal implementation** — apply Change 1 + Change 2; add the `exit_policy` fields; import `resolve_exit_policy` + `ExitPolicy`.

- [ ] **Step 4: Run test to verify it passes** — Run the same command → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py \
        apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/position_manager.py \
        apps/alphalens-research/tests/brokers/automanager/test_build_default_deps.py
git commit -s -m "refactor(brokers): resolve ExitPolicy once at startup, cache on LoopDeps + ProtectionView"
```

---

### Task 4: Route placement geometry through the cached policy

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/paper/sizing.py:265-273` (`build_exit_geometry_spec`)
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py:1703` (`_journal_tier` `use_geometry`)
- Test: `apps/alphalens-research/tests/brokers/automanager/` — locate the existing geometry-stamp test module; add cases.

**Interfaces:**
- Change 1 — `sizing.build_exit_geometry_spec`: replace `policy = resolve_policy("atr_bracket_1p5"); levels = policy.levels(blended, atr, ceiling_price=ceiling)` with `exit_policy = resolve_exit_policy("atr_bracket_1p5"); levels = exit_policy.decide_placement_geometry(blended, atr, ceiling_price=ceiling)`; build `ReanchorOnFill(k_atr=exit_policy.geom.stop_atr_mult, ...)` (still 1.5 → byte-identical). This resolve is at arm/build time, NOT in the protection pass, so it is not on the hot protection path. Update imports.
- Change 2 — `control_loop._journal_tier`: replace `use_geometry = _exit_policy() != "setup_static" and exit_spec is not None` with `use_geometry = deps.exit_policy.applies_geometry and exit_spec is not None` (read the CACHED instance; confirm `deps`/`exit_policy` is in scope at this call-site — thread it in if `_journal_tier` is a closure without `deps`).

- [ ] **Step 1: Write the failing test** — a `unittest.TestCase`: (a) `build_exit_geometry_spec` output unchanged for a representative setup dict (same stop/tp/reanchor as `resolve_policy("atr_bracket_1p5").levels(...)`); (b) `_journal_tier` sets `use_geometry` True when `deps.exit_policy` is `atr_bracket_1p5`, False when `setup_static`. Mirror the existing geometry-stamp test's fixtures.

- [ ] **Step 2: Run test to verify it fails** — FAIL until wiring lands.

- [ ] **Step 3: Write minimal implementation** — apply Change 1 + Change 2; import `resolve_exit_policy` in `sizing.py`.

- [ ] **Step 4: Run tests** — Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest discover -s tests/brokers/automanager -t . -v` → PASS; no `!= "setup_static"` left in `_journal_tier`.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/paper/sizing.py \
        apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py \
        apps/alphalens-research/tests/brokers/automanager/
git commit -s -m "refactor(brokers): resolve placement geometry via the cached ExitPolicy (name, not sentinel)"
```

---

### Task 5: Route reanchor through the cached policy + envelope + divergence telemetry

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/position_manager.py:652-684` (`_maybe_reanchor`)
- Test: `apps/alphalens-research/tests/brokers/automanager/` — locate the existing reanchor test module; add cases.

**Interfaces:**
- Consumes: `view.exit_policy` (cached, Task 3), `clamp_reanchor_target` (Task 2), `PlannedExit.stop_price` (existing, `position_manager.py:146`).
- Rewrite the head of `_maybe_reanchor` (which already receives `view`):
  - Replace `if _exit_policy() == "setup_static": return None` with `policy = view.exit_policy` — NO env read, NO resolve.
  - After the `avg_price` / `atr` / `sole` / latch guards, compute `proposed = policy.decide_reanchor(avg_price, atr)`; `None` → `return None` (setup_static stays inert; degenerate stays safe).
  - Replace `target = avg_price - plan.reanchor.k_atr * atr` with `clamped = clamp_reanchor_target(plan.stop_price, proposed, anchor_price=avg_price, min_distance_frac=policy.min_stop_distance_frac)`; `None` → `return None`; else `target = clamped`.
  - When `clamped is not None and not math.isclose(clamped, proposed)`: `logger.info("reanchor envelope clamped: policy=%s proposed=%.4f clamped=%.4f prior_stop=%.4f", policy.name, proposed, clamped, plan.stop_price)`. Add a `# TODO(INC-2): persist this divergence to the append-only journal (memo section 7), not log-only.`

- [ ] **Step 1: Write the failing test** — a `unittest.TestCase`:
  - `setup_static` (build `view.exit_policy = SetupStaticPolicy()`) → returns `None` (inert).
  - `atr_bracket_1p5`, `avg_price` ABOVE the planned blend (`avg_price - 1.5*atr` ≥ `plan.stop_price`) → returns an `AmendStop` with `target == avg_price - 1.5*atr` (tightening; preserved).
  - `atr_bracket_1p5`, `avg_price` BELOW the planned blend so `avg_price - 1.5*atr < plan.stop_price` → returns `None` (never-below-brief-floor refuses — the accepted Decision-1 divergence; today's code would have emitted a looser `AmendStop`).
  - degenerate `atr`/`avg_price` → `None`.
  Build the `PlannedExit` / `Position` / `ProtectionView(exit_policy=...)` / `_sole_standalone_stop` fixtures the same way the existing reanchor test does.

- [ ] **Step 2: Run test to verify it fails** — the below-blend case still emits an `AmendStop` (old loosening path) until the clamp lands → FAIL.

- [ ] **Step 3: Write minimal implementation** — apply the `_maybe_reanchor` rewrite; import `clamp_reanchor_target`.

- [ ] **Step 4: Run test to verify it passes** — below-blend now `None`; above-blend unchanged; setup_static inert → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/position_manager.py \
        apps/alphalens-research/tests/brokers/automanager/
git commit -s -m "feat(brokers): reanchor through cached ExitPolicy + envelope (never-below-brief-floor) with divergence telemetry"
```

---

### Task 6: Full safety-regression sweep + sentinel-removal check

**Files:**
- Test only: run the automanager + exit_geometry + acceptance suites; add one static-guard test.

- [ ] **Step 1: Write a static guard test** — `tests/brokers/automanager/test_no_exit_policy_sentinel.py`, a `unittest.TestCase` that reads the source of `control_loop.py` and `position_manager.py` and asserts no `!= "setup_static"` or `== "setup_static"` comparison survives, AND that `resolve_exit_policy` / `_exit_policy()` appear in NEITHER `reconcile_protection`/`_reconcile_long`/`_maybe_reanchor` nor `_journal_tier` (the hot-path-resolution guard from the adversarial review). Use `inspect.getsource` on those functions; assert the substrings are absent.

- [ ] **Step 2: Run the guard test** — Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.automanager.test_no_exit_policy_sentinel -v` → PASS.

- [ ] **Step 3: Run the full safety + geometry suites** — Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest discover -s tests/brokers -t . -v` and `../../.venv/bin/python -m unittest discover -s tests/exit_geometry -t . -v` → PASS (`test_every_position_protected`, `test_no_oversell`, `test_safety_rails`, `test_resilience` all green; INV-1…INV-10 intact).

- [ ] **Step 4: Module-dependency + lint gate** — Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_module_dependencies -v` then `../../.venv/bin/ruff check apps/alphalens-broker-contract apps/alphalens-pipeline/alphalens_pipeline/brokers` → PASS (`broker_contract` stays a dependency-free leaf; no lint regressions).

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/tests/brokers/automanager/test_no_exit_policy_sentinel.py
git commit -s -m "test(brokers): guard no exit-policy sentinel and no hot-path resolve survive the refactor"
```

---

## Post-implementation (NOT part of TDD tasks)

- **`uv sync`** in the worktree first (edits `broker_contract` + `alphalens_pipeline` package code → needs its own environment).
- **Zen pre-MERGE review** (`deepseek/deepseek-v4-pro`, `thinking_mode="high"`) after push + PR, before merge — mandatory (protection-critical broker code).
- **Deploy is a SEPARATE step** — do NOT bundle with a soak. The flip flag (`ALPHALENS_BROKER_EXIT_POLICY=atr_bracket_1p5`) is already live on the VPS; deploying this refactor runs the same geometry through the registry, with never-below-brief-floor now active. Land + soak on its own cycle.

## Self-review notes

- **Spec coverage:** Task 1 registry (+ `min_stop_distance_frac`), Task 2 envelope, Task 3 startup-resolve + cache + capability gate (the CRITICAL adversarial fix), Task 4 placement wiring, Task 5 reanchor wiring + telemetry, Task 6 regression + sentinel + hot-path-resolve guard. INC-0 already shipped. Deferred ML apparatus out of scope (memo section 7).
- **Type consistency:** `decide_placement_geometry -> tuple[float,float]|None`; `decide_reanchor -> float|None`; `clamp_reanchor_target -> float|None`; `prior_stop = plan.stop_price` in Task 5 matches Task 2's contract; `LoopDeps.exit_policy` / `ProtectionView.exit_policy` typed `ExitPolicy`.
- **Known accepted divergence:** Task 5's below-blend case changes live behavior vs today (Decision 1) — intended, test-pinned.
- **Adversarial-review fixes folded:** resolve-once/cache (was per-tick), `min_stop_distance_frac` as policy attribute, never-below-brief-floor naming, telemetry TODO.
