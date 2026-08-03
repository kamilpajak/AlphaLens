# Exit-policy registry + stateful envelope — implementation plan (INC-1)

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the SIM exit-logic layer into a named `ExitPolicy` registry behind a stateful safety envelope, so today's deterministic policies re-express through one abstraction and a future ML policy plugs in as one more registry entry — zero ML, zero RNG in this increment.

**Architecture:** A dependency-free `ExitPolicy` Protocol + two concrete policies live in the `broker_contract` leaf. The daemon resolves a policy by NAME (never a not-equal string test) and calls it through one placement path and one reanchor path. A pure, stateful `clamp_reanchor_target` enforces the economic content of a reanchor (min-distance floor + monotone-tighten vs the planned disaster stop). All never-naked / never-oversell / realized-qty invariants stay 100% inside the untouched `reconcile_protection` diff engine; the policy is a pure price oracle that never emits an order Action.

**Tech Stack:** Python 3.12, `broker_contract` (stdlib-only leaf), `alphalens_pipeline.brokers.automanager`, `unittest` (research tests).

**Design memo:** `docs/research/nondeterministic_exit_policy_design_2026_08_03.md` (DRAFT). INC-0 (`setup_builder_config_version`) is ALREADY shipped — this plan is INC-1 only.

## Global Constraints

- **SIM-only.** No live-money path changes. Placement stays gated on `ALPHALENS_BROKER_ALLOW_ORDERS=1`.
- **TDD, red→green.** Research tests MUST subclass `unittest.TestCase` (pytest-style bare functions are silently skipped in CI).
- **English-only** in code, comments, docstrings.
- **`broker_contract` is a dependency-free leaf** — `policy.py` and `levels.py` import only stdlib + sibling `broker_contract` modules. No `alphalens_pipeline` import from `broker_contract`.
- **Byte-identical behavior EXCEPT the one accepted break:** `setup_static` stays fully inert (geometry dark, no reanchor). `atr_bracket_1p5` reproduces today's placement + reanchor EXCEPT that a reanchor may no longer move the stop looser than `plan.stop_price` (monotone-tighten, Decision 1) — this is the intended divergence, live now.
- **All safety-acceptance tests stay green:** `test_every_position_protected`, `test_no_oversell`, `test_safety_rails`, `test_resilience` (INV-1…INV-10).
- **Policy selection is name→registry→object.** No surviving `!= "setup_static"` / `== "setup_static"` test after this increment. An unknown policy name raises `ValueError` (fail-fast).
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
  - `class ExitPolicy(Protocol)` with read-only attrs `name: str`, `version: int`, `applies_geometry: bool`, `requires_amend_stop: bool`, and methods
    `decide_placement_geometry(self, blended: float, atr: float, *, ceiling_price: float | None) -> tuple[float, float] | None`
    `decide_reanchor(self, avg_price: float, atr: float) -> float | None`.
  - `SetupStaticPolicy` (inert): `applies_geometry=False`, `requires_amend_stop=False`, both methods return `None`.
  - `AtrBracketPolicy(geom: ExitGeometryPolicy)`: `applies_geometry=True`, `requires_amend_stop=True`; placement → `geom.levels(...)`; reanchor → `avg_price - geom.stop_atr_mult * atr` (or `None` on degenerate).
  - `resolve_exit_policy(name: str) -> ExitPolicy` — `{"setup_static": SetupStaticPolicy(), "atr_bracket_1p5": AtrBracketPolicy(resolve_policy("atr_bracket_1p5"))}`; unknown → `ValueError`.

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-research/tests/exit_geometry/test_policy.py
import math
import unittest

from broker_contract.exit_geometry.policy import (
    AtrBracketPolicy,
    SetupStaticPolicy,
)
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

    def test_placement_matches_raw_levels(self):
        blended, atr = 100.0, 2.0
        want = resolve_policy("atr_bracket_1p5").levels(blended, atr, ceiling_price=None)
        self.assertEqual(self.p.decide_placement_geometry(blended, atr, ceiling_price=None), want)

    def test_reanchor_target(self):
        # avg_price - 1.5 * atr
        self.assertTrue(math.isclose(self.p.decide_reanchor(101.0, 2.0), 101.0 - 3.0))

    def test_reanchor_degenerate_atr_is_none(self):
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
Expected: FAIL — `ModuleNotFoundError: broker_contract.exit_geometry.policy` / `ImportError: resolve_exit_policy`.

- [ ] **Step 3: Write minimal implementation**

```python
# apps/alphalens-broker-contract/broker_contract/exit_geometry/policy.py
"""Behavioral exit-policy abstraction (name -> placement + reanchor decisions).

An ``ExitPolicy`` is a PURE price oracle: it proposes exit levels and a reanchor
target and NEVER emits a broker Action. The daemon resolves one by name and
routes placement + reanchor through it, so adding a policy (a future ML policy
included) is a new registry entry, not a new call-site. ``SetupStaticPolicy`` is
the inert/null policy that keeps the brief's static disaster stop/TP and never
reanchors; ``AtrBracketPolicy`` wraps a numeric ``ExitGeometryPolicy``.
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
# apps/alphalens-broker-contract/broker_contract/exit_geometry/registry.py
# (append below the existing resolve_policy)

def resolve_exit_policy(name: str) -> "ExitPolicy":
    """Resolve a behavioral ExitPolicy by name (fail-fast on unknown).

    Lazy import of ``policy`` avoids a module import cycle (policy.py imports
    ExitGeometryPolicy from this module).
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

Add `resolve_exit_policy`, `ExitPolicy`, `SetupStaticPolicy`, `AtrBracketPolicy` to `exit_geometry/__init__.py` `__all__` + imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.exit_geometry.test_policy -v`
Expected: PASS (all cases).

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
  - Returns `None` (meaning "do NOT reanchor — keep the resting stop") on any degenerate input OR when the clamped target would loosen the stop below `prior_stop`.
  - `prior_stop` is the placement-time planned disaster stop (`plan.stop_price`), i.e. the brief disaster floor.

**Semantics (enforce in this order):**
1. Degrade: if any of `prior_stop`, `proposed_target`, `anchor_price` is non-finite or ≤ 0 → `None`.
2. Min-distance floor: `floor_price = anchor_price * (1 - min_distance_frac)` is the CLOSEST the stop may sit to the anchor; if `proposed_target > floor_price` → clamp down to `floor_price` (guards hair-trigger).
3. Monotone-tighten: if the (floored) target `< prior_stop` → `None` (refuse to loosen below the brief disaster level).
4. Else return the (floored) target.

- [ ] **Step 1: Write the failing test**

```python
# append to apps/alphalens-research/tests/exit_geometry/test_levels.py
import math
import unittest

from broker_contract.exit_geometry.levels import clamp_reanchor_target


class ClampReanchorTargetTest(unittest.TestCase):
    def test_tighter_target_passes(self):
        # proposed (99.0) is above prior_stop (98.0) => tightening => allowed
        out = clamp_reanchor_target(98.0, 99.0, anchor_price=101.0, min_distance_frac=0.002)
        self.assertTrue(math.isclose(out, 99.0))

    def test_loosening_below_prior_stop_refused(self):
        # proposed (97.0) is below prior_stop (98.0) => would loosen => None
        self.assertIsNone(
            clamp_reanchor_target(98.0, 97.0, anchor_price=101.0, min_distance_frac=0.002)
        )

    def test_too_close_clamped_to_floor(self):
        # proposed (100.9) is closer than floor (101*0.998=100.798) => clamp to floor
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
    """Economic safety envelope for a reanchored disaster stop (memo §3.1).

    ``prior_stop`` is the placement-time planned disaster stop (the brief
    disaster floor). Returns ``None`` to mean "do NOT reanchor — leave the
    resting stop where it is" on any degenerate input or when the target would
    loosen the stop below ``prior_stop``. The min-distance floor caps how close
    the stop may sit to ``anchor_price`` (hair-trigger guard); it is chosen so
    it never binds the current 1.5x-ATR policy and exists mainly for a future
    stochastic policy.
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

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.exit_geometry.test_levels -v`
Expected: PASS (existing `atr_bracket_levels` tests + new clamp tests).

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-broker-contract/broker_contract/exit_geometry/levels.py \
        apps/alphalens-broker-contract/broker_contract/exit_geometry/__init__.py \
        apps/alphalens-research/tests/exit_geometry/test_levels.py
git commit -s -m "feat(brokers): stateful reanchor envelope (min-distance floor + monotone-tighten)"
```

---

### Task 3: Route placement geometry through the policy

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/paper/sizing.py:265-273` (`build_exit_geometry_spec`)
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py:1703` (`_journal_tier` `use_geometry`)
- Test: `apps/alphalens-research/tests/brokers/automanager/test_control_loop_geometry.py` (add case) + existing sizing test

**Interfaces:**
- Consumes: `resolve_exit_policy` (Task 1), `_exit_policy()` (existing).
- Change 1 — `sizing.build_exit_geometry_spec`: replace `policy = resolve_policy("atr_bracket_1p5"); levels = policy.levels(blended, atr, ceiling_price=ceiling)` with `exit_policy = resolve_exit_policy("atr_bracket_1p5"); levels = exit_policy.decide_placement_geometry(blended, atr, ceiling_price=ceiling)`; the `ReanchorOnFill(k_atr=...)` argument reads `exit_policy.geom.stop_atr_mult` (byte-identical, still 1.5). Update the import.
- Change 2 — `control_loop._journal_tier`: replace `use_geometry = _exit_policy() != "setup_static" and exit_spec is not None` with `use_geometry = resolve_exit_policy(_exit_policy()).applies_geometry and exit_spec is not None`.

- [ ] **Step 1: Write the failing test** — a `unittest.TestCase` asserting (a) `build_exit_geometry_spec` output is unchanged for a representative setup dict (same stop/tp/reanchor as `resolve_policy("atr_bracket_1p5").levels(...)`), and (b) `_journal_tier` sets `use_geometry` True under `ALPHALENS_BROKER_EXIT_POLICY=atr_bracket_1p5` and False under `setup_static`, and that an unknown policy name raises `ValueError`. (Mirror the existing geometry-stamp test in `tests/brokers/automanager/`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.automanager.test_control_loop_geometry -v`
Expected: FAIL (assertion or import until wiring lands).

- [ ] **Step 3: Write minimal implementation** — apply Change 1 and Change 2 above; import `resolve_exit_policy` in both modules.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.automanager -v`
Expected: PASS — geometry stamp + placement tests green; no `!= "setup_static"` left in `_journal_tier`.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/paper/sizing.py \
        apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py \
        apps/alphalens-research/tests/brokers/automanager/test_control_loop_geometry.py
git commit -s -m "refactor(brokers): resolve placement geometry via the ExitPolicy registry (name, not sentinel)"
```

---

### Task 4: Route reanchor through the policy + envelope + divergence telemetry

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/position_manager.py:652-684` (`_maybe_reanchor`) + add `_MIN_REANCHOR_DISTANCE_FRAC` constant near the other `_EXIT_*` constants
- Test: `apps/alphalens-research/tests/brokers/automanager/test_reanchor.py` (or the existing reanchor test module)

**Interfaces:**
- Consumes: `resolve_exit_policy`, `clamp_reanchor_target`, `_exit_policy()`, `PlannedExit.stop_price` (existing, `position_manager.py:146`).
- Rewrite the head of `_maybe_reanchor`:
  - Replace `if _exit_policy() == "setup_static": return None` with
    `policy = resolve_exit_policy(_exit_policy())` then, after the `avg_price` / `atr` / `sole` / latch guards, compute
    `proposed = policy.decide_reanchor(avg_price, atr)` — `None` → `return None` (setup_static stays inert; degenerate stays safe).
  - Replace the raw `target = avg_price - plan.reanchor.k_atr * atr` with
    `clamped = clamp_reanchor_target(plan.stop_price, proposed, anchor_price=avg_price, min_distance_frac=_MIN_REANCHOR_DISTANCE_FRAC)`; `None` → `return None` (monotone-tighten refused / degenerate); else `target = clamped`.
  - When `clamped is not None and not math.isclose(clamped, proposed)`, emit a divergence telemetry log: `logger.info("reanchor envelope clamped: policy=%s proposed=%.4f clamped=%.4f prior_stop=%.4f", policy.name, proposed, clamped, plan.stop_price)`. (Read-side / parquet persistence of this divergence is DEFERRED — see memo §7.)
- `_MIN_REANCHOR_DISTANCE_FRAC = 0.002` with a comment that it is a future-facing hair-trigger floor that never binds the 1.5x-ATR policy.

- [ ] **Step 1: Write the failing test** — a `unittest.TestCase` with cases:
  - `setup_static` → `_maybe_reanchor` returns `None` (inert, unchanged).
  - `atr_bracket_1p5`, `avg_price` ABOVE the planned blend (`avg_price - 1.5*atr` ≥ `plan.stop_price`) → returns an `AmendStop` with `target == avg_price - 1.5*atr` (tightening; today's behavior preserved).
  - `atr_bracket_1p5`, `avg_price` BELOW the planned blend so `avg_price - 1.5*atr < plan.stop_price` → returns `None` (monotone-tighten refuses to loosen — the accepted Decision-1 divergence; today's code would have emitted a looser `AmendStop`).
  - degenerate `atr`/`avg_price` → `None`.
  Build the `PlannedExit` / `Position` / `ProtectionView` / `_sole_standalone_stop` fixtures the same way the existing reanchor test does.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.automanager.test_reanchor -v`
Expected: FAIL — the below-blend case still returns an `AmendStop` (old loosening path) until the clamp lands.

- [ ] **Step 3: Write minimal implementation** — apply the `_maybe_reanchor` rewrite + constant above.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.automanager.test_reanchor -v`
Expected: PASS — below-blend now returns `None`; above-blend unchanged; setup_static inert.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/position_manager.py \
        apps/alphalens-research/tests/brokers/automanager/test_reanchor.py
git commit -s -m "feat(brokers): reanchor through ExitPolicy + envelope (monotone-tighten live) with divergence telemetry"
```

---

### Task 5: Capability gate via the policy

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py:738-742` (`build_default_deps`)
- Test: `apps/alphalens-research/tests/brokers/automanager/test_build_default_deps.py` (or existing capability-gate test)

**Interfaces:**
- Consumes: `resolve_exit_policy`, `_exit_policy()`, `SupportsAmendStop` (existing).
- Change: replace `if _exit_policy() != "setup_static" and not isinstance(broker, SupportsAmendStop):` with
  `if resolve_exit_policy(_exit_policy()).requires_amend_stop and not isinstance(broker, SupportsAmendStop):`. Keep the identical `BrokerCapabilityError` message class + INV-8 semantics. An unknown policy name now raises `ValueError` at startup (a stricter, still-fail-fast outcome — assert it).

- [ ] **Step 1: Write the failing test** — a `unittest.TestCase`: (a) `atr_bracket_1p5` + broker WITHOUT `SupportsAmendStop` → `BrokerCapabilityError`; (b) `setup_static` + same broker → no raise; (c) `atr_bracket_1p5` + broker WITH `SupportsAmendStop` → no raise; (d) unknown policy name → `ValueError`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.automanager.test_build_default_deps -v`
Expected: FAIL on case (d) (`ValueError` not raised) until the wiring lands.

- [ ] **Step 3: Write minimal implementation** — apply the gate change.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.automanager.test_build_default_deps -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/control_loop.py \
        apps/alphalens-research/tests/brokers/automanager/test_build_default_deps.py
git commit -s -m "refactor(brokers): capability gate keys on ExitPolicy.requires_amend_stop, not a sentinel"
```

---

### Task 6: Full safety-regression sweep + sentinel-removal check

**Files:**
- Test only: run the automanager + exit_geometry + acceptance suites; add one static-guard test.

**Interfaces:** none (verification task).

- [ ] **Step 1: Write a static guard test** — a `unittest.TestCase` in `tests/brokers/automanager/test_no_exit_policy_sentinel.py` that reads the source of `control_loop.py` and `position_manager.py` and asserts no surviving `!= "setup_static"` or `== "setup_static"` comparison remains (the abstraction must be complete). Use `inspect.getsource` on the two modules or read the files; assert the substrings are absent.

- [ ] **Step 2: Run the guard test**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.brokers.automanager.test_no_exit_policy_sentinel -v`
Expected: PASS (fails if any Task left a sentinel behind).

- [ ] **Step 3: Run the full safety + geometry suites**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest discover -s tests/brokers -t . -v` and `../../.venv/bin/python -m unittest discover -s tests/exit_geometry -t . -v`
Expected: PASS — `test_every_position_protected`, `test_no_oversell`, `test_safety_rails`, `test_resilience` all green (INV-1…INV-10 intact).

- [ ] **Step 4: Module-dependency + lint gate**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_module_dependencies -v` then `../../.venv/bin/ruff check apps/alphalens-broker-contract apps/alphalens-pipeline/alphalens_pipeline/brokers`
Expected: PASS — `broker_contract` stays a dependency-free leaf; no lint regressions.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/tests/brokers/automanager/test_no_exit_policy_sentinel.py
git commit -s -m "test(brokers): guard that no exit-policy sentinel comparison survives the registry refactor"
```

---

## Post-implementation (NOT part of TDD tasks)

- **`uv sync`** in the worktree before running (the worktree edits package code — `broker_contract` + `alphalens_pipeline` — and needs its own environment).
- **Zen pre-MERGE review** (`deepseek/deepseek-v4-pro`, `thinking_mode="high"`) after push + PR, before merge (this touches protection-critical broker code — mandatory).
- **Deploy is a SEPARATE step** — do NOT bundle with today's soak. The flip flag (`ALPHALENS_BROKER_EXIT_POLICY=atr_bracket_1p5`) is already live on the VPS; deploying this refactor means the daemon runs the same geometry through the new registry, with the monotone-tighten rail now active. Land + soak on its own cycle.

## Self-review notes

- **Spec coverage:** INC-1 registry (Task 1), stateful envelope (Task 2), placement wiring (Task 3), reanchor wiring + telemetry (Task 4), capability gate (Task 5), safety regression + sentinel removal (Task 6). INC-0 already shipped (dropped). Deferred ML apparatus is explicitly out of scope (memo §7).
- **Type consistency:** `decide_placement_geometry` returns `tuple[float,float] | None` everywhere; `decide_reanchor` returns `float | None`; `clamp_reanchor_target` returns `float | None`. `prior_stop` = `plan.stop_price` in Task 4 matches Task 2's parameter contract.
- **Known accepted divergence:** Task 4's below-blend case changes live behavior vs today (Decision 1) — this is intended and test-pinned, not a regression.
