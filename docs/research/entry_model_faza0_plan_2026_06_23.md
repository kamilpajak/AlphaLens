# Entry-model Faza 0 (entry-grid substrate + offline comparison) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the counterfactual entry-grid substrate (replay 5 entry primitives over the same cached minute bars, exit held fixed) plus an offline research script that prints per-arm cost-adjusted market-excess with day-blocked bootstrap CIs — the GO/NO-GO evidence for whether any static entry arm beats the dip-buy baseline.

**Architecture:** Pipeline-side substrate (`execution_cost.py`, `entry_primitives.py`, `replay_entry_grid` in `ladder_replay.py`) computes a flat `{arm: cost_adjusted_market_excess|None}` per event. Research-side adds `day_block_bootstrap_ci` to `fixed_horizon.py` and a `diagnose_entry_grid.py` script. Telemetry-only; NO stamped parquet column, NO config_version, NO learned policy, NO Django/product change.

**Tech Stack:** Python, pandas, unittest; reuses `alphalens_pipeline.feedback.{ladder_replay,bar_window,benchmark_excess}`, `alphalens_pipeline.thematic.trade_setup.{ladder,builder}`, `alphalens_research.diagnostics.{edge_stores,fixed_horizon}`.

## Global Constraints

- TDD always — red→green→refactor; behavior lives in importable modules (scripts are coverage-excluded → collection helpers go in modules/tested).
- ADR-0011 placement: substrate is PIPELINE-side; the script + bootstrap are RESEARCH-side. `alphalens_pipeline.*` imports NOTHING from `alphalens_research.*`. The research script may import `alphalens_pipeline.feedback.*`.
- English-only in code; math notation OK. No new data vendor, no network in pure functions.
- NaN-safe + no float `==` (Sonar S1244 → use `> 0` / `math.isnan`); seeded RNG hotspot carries `# NOSONAR`.
- Faza-0 scope is STRICT: substrate + offline script ONLY. No `entry_grid_config_version`, no stamped column, no `entry_policy` package, no nightly refit.
- Reward is locked: raw market-excess on the arm's OWN blended entry, horizon k=10, exit fixed by ABSOLUTE TP/SL prices, equal size, NO_FILL/BAD_GEOMETRY = cash (= `-benchmark_window_return`) identically across arms, common-support headline, per-arm execution-cost haircut (always-fill arms only).

## Assumptions (resolved during recon)

- **Minute-bar coverage is ~complete** — 333 cache files for 316 plannable events on the VPS (the arrival-window bars are cached for every event to compute `reference_close` = arrival VWAP). So the entry-grid N is ~full, not starved by the Tier-1/Tier-2 split.
- **Haircut constants are an unvalidated proxy** (impact 2/5/12/25 bps by mcap bucket; first-bar high-low half-spread; 12/25 bps fallbacks). No real fills exist post-ADR-0012. The script reports results BOTH pre- and post-haircut so the sensitivity is visible. Tunable later; not load-bearing for Faza-0 GO/NO-GO direction.
- **SPY leg** = grouped-daily SPY closes over `[previous_trading_day(arrival), horizon]` (fetch-free, consistent with `fixed_horizon.car_for_event`); injected as a scalar into the pure substrate. Minute-VWAP SPY anchor is a Faza-1 refinement.

## Interface contract (canonical — every task uses these exact signatures)

**`alphalens_pipeline/feedback/execution_cost.py`** (pure, no I/O):
- `RESTING_LIMIT_ARMS: frozenset[str] = frozenset({"baseline","narrow_tiers","single_at_close"})`
- `ALWAYS_FILL_ARMS: frozenset[str] = frozenset({"market_at_arrival","vwap_arrival"})`
- `impact_bps_for_mcap(market_cap: float | None) -> float`
- `half_spread_bps_from_bar(first_bar: Mapping[str, Any] | None) -> float`
- `arm_haircut_bps(arm: str, *, market_cap: float | None, first_rth_bar: Mapping[str, Any] | None) -> float`
- `apply_haircut_to_excess(raw_excess: float | None, *, arm: str, market_cap: float | None, first_rth_bar: Mapping[str, Any] | None) -> float | None`

**`alphalens_pipeline/thematic/trade_setup/entry_primitives.py`**:
- `@dataclass(frozen=True) ArmFill: fill_price: float | None; fill_ts_ms: int | None; late_open: bool = False; status: str = "OK"`
- `@dataclass(frozen=True) ArmSetup: arm: str; status: str; arm_blended: float | None; disaster_stop: float | None; entry_tiers: tuple[Mapping[str, Any], ...]; tp_tranches: tuple[Mapping[str, Any], ...]; geometry_collapsed: bool = False`
- `market_at_arrival_fill(bars, *, arrival_open_ms: int, arrival_close_ms: int) -> ArmFill`
- `vwap_arrival_fill(bars, *, arrival_open_ms: int, window_min: int = bar_window.ARRIVAL_VWAP_WINDOW_MIN) -> ArmFill`
- `STOP_ATR_BUFFER_K: float` (re-export of `builder._STOP_ATR_BUFFER`)
- `arm_disaster_stop(arm_blended: float, atr: float, close: float, *, k: float = STOP_ATR_BUFFER_K) -> float`
- `build_baseline_arm(trade_setup) -> ArmSetup`
- `build_narrow_tiers_arm(*, close, atr, mults=(0.10,0.175,0.25), min_spacing_mult, min_stop_dist_mult) -> ArmSetup`
- `build_single_at_close_arm(*, close, atr, just_below_mult=0.0) -> ArmSetup`

**`alphalens_pipeline/feedback/ladder_replay.py`** (additions):
- `ENTRY_GRID_ARMS: tuple[str, ...] = ("baseline","narrow_tiers","single_at_close","market_at_arrival","vwap_arrival")`
- `_with_disaster_stop(trade_setup, stop) -> dict[str, Any]` (shallow-copy mirror of `_with_entry_tiers`)
- `_replay_synthetic_fill(trade_setup, bars, *, fill_price, fill_ts_ms, own_stop, position_expiry_ms) -> LadderOutcome`
- `replay_entry_grid(trade_setup, bars, *, arrival_open_ms, arrival_close_ms, benchmark_window_return, market_cap, entry_expiry_ms=None, position_expiry_ms=None) -> dict[str, float | None]`

**`alphalens_research/diagnostics/fixed_horizon.py`** (addition, beside untouched `bootstrap_ci`):
- `day_block_bootstrap_ci(values_by_day: Mapping[object, Sequence[float | None]], *, n_resamples=10_000, ci=0.90, seed=0) -> tuple[float | None, float | None, float | None]` — resamples whole days, **grand-mean** point estimate (equals `bootstrap_ci` mean on the flattened values).

**`alphalens_research/scripts/diagnose_entry_grid.py`** — CLI mirroring `diagnose_selection.main()`.

---

### Task 1: `execution_cost.py` — pure per-arm haircut

**Files:**
- Create: `apps/alphalens-pipeline/alphalens_pipeline/feedback/execution_cost.py`
- Test: `apps/alphalens-research/tests/feedback/test_execution_cost.py`

**Interfaces:** Produces the 4 `execution_cost.*` functions + 2 arm frozensets (see contract).

- [ ] **Step 1: Write the failing tests**

```python
# apps/alphalens-research/tests/feedback/test_execution_cost.py
import math
import unittest

from alphalens_pipeline.feedback import execution_cost as ec


class TestImpactBps(unittest.TestCase):
    def test_buckets_and_default(self):
        self.assertAlmostEqual(ec.impact_bps_for_mcap(500e9), 2.0)    # mega
        self.assertAlmostEqual(ec.impact_bps_for_mcap(20e9), 5.0)     # mid
        self.assertAlmostEqual(ec.impact_bps_for_mcap(3e9), 12.0)     # small
        self.assertAlmostEqual(ec.impact_bps_for_mcap(3e8), 25.0)     # micro
        for bad in (None, float("nan"), 0.0, -1.0):
            self.assertAlmostEqual(ec.impact_bps_for_mcap(bad), 12.0)  # conservative default, not cheapest


class TestHalfSpreadBps(unittest.TestCase):
    def test_proxy_and_fallback(self):
        # mid=100, (h-l)=2 -> half-spread = 0.5*2/100 = 1% = 100 bps
        self.assertAlmostEqual(ec.half_spread_bps_from_bar({"h": 101.0, "l": 99.0}), 100.0)
        # tight: mid=100, (h-l)=0.2 -> 10 bps
        self.assertAlmostEqual(ec.half_spread_bps_from_bar({"h": 100.1, "l": 99.9}), 10.0)
        for bad in (None, {}, {"h": 1.0}, {"h": 1.0, "l": 2.0}, {"h": 0.0, "l": 0.0}):
            self.assertAlmostEqual(ec.half_spread_bps_from_bar(bad), 25.0)


class TestArmHaircut(unittest.TestCase):
    def test_resting_arms_zero(self):
        for arm in ec.RESTING_LIMIT_ARMS:
            self.assertEqual(ec.arm_haircut_bps(arm, market_cap=3e8, first_rth_bar={"h": 101.0, "l": 99.0}), 0.0)

    def test_always_fill_sums_spread_and_impact(self):
        bps = ec.arm_haircut_bps("market_at_arrival", market_cap=3e9, first_rth_bar={"h": 101.0, "l": 99.0})
        self.assertAlmostEqual(bps, 100.0 + 12.0)

    def test_unknown_arm_raises(self):
        with self.assertRaises(ValueError):
            ec.arm_haircut_bps("nope", market_cap=1e9, first_rth_bar=None)


class TestApplyHaircut(unittest.TestCase):
    def test_none_passthrough(self):
        self.assertIsNone(ec.apply_haircut_to_excess(None, arm="market_at_arrival", market_cap=1e9, first_rth_bar=None))

    def test_resting_unchanged(self):
        self.assertAlmostEqual(
            ec.apply_haircut_to_excess(0.05, arm="baseline", market_cap=3e8, first_rth_bar={"h": 9.0, "l": 1.0}), 0.05
        )

    def test_always_fill_strictly_lower(self):
        out = ec.apply_haircut_to_excess(0.05, arm="vwap_arrival", market_cap=3e9, first_rth_bar={"h": 101.0, "l": 99.0})
        self.assertLess(out, 0.05)
        self.assertAlmostEqual(out, 0.05 - (100.0 + 12.0) / 10_000)

    def test_negative_excess_still_charged(self):
        out = ec.apply_haircut_to_excess(-0.02, arm="market_at_arrival", market_cap=3e9, first_rth_bar={"h": 101.0, "l": 99.0})
        self.assertLess(out, -0.02)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → RED**

Run: `uv run python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -p test_execution_cost.py`
Expected: FAIL (`ModuleNotFoundError: ...execution_cost`).

- [ ] **Step 3: Implement**

```python
# apps/alphalens-pipeline/alphalens_pipeline/feedback/execution_cost.py
"""Per-arm execution-cost haircut for the entry-grid (Faza 0).

Resting-limit arms keep their touch price (price-improvement preserved, 0 haircut).
Always-fill arms (market_at_arrival, vwap_arrival) pay a half-spread + market-impact
haircut, charged ONE-WAY (entry only) in return space. All constants are an
UNVALIDATED proxy — no real fills exist post-ADR-0012; the offline script reports
pre- and post-haircut so the sensitivity is visible.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

RESTING_LIMIT_ARMS: frozenset[str] = frozenset({"baseline", "narrow_tiers", "single_at_close"})
ALWAYS_FILL_ARMS: frozenset[str] = frozenset({"market_at_arrival", "vwap_arrival"})

# (lower_bound_usd, impact_bps) descending; first match wins.
_MCAP_BUCKETS: tuple[tuple[float, float], ...] = (
    (1e11, 2.0),   # mega
    (1e10, 5.0),   # mid
    (1e9, 12.0),   # small
)
_MICRO_IMPACT_BPS = 25.0
_DEFAULT_IMPACT_BPS = 12.0       # conservative when mcap unknown (NOT cheapest)
_DEFAULT_HALF_SPREAD_BPS = 25.0  # conservative when the bar proxy is unusable


def impact_bps_for_mcap(market_cap: float | None) -> float:
    if market_cap is None or (isinstance(market_cap, float) and math.isnan(market_cap)) or market_cap <= 0.0:
        return _DEFAULT_IMPACT_BPS
    for lower, bps in _MCAP_BUCKETS:
        if market_cap >= lower:
            return bps
    return _MICRO_IMPACT_BPS


def half_spread_bps_from_bar(first_bar: Mapping[str, Any] | None) -> float:
    if not first_bar:
        return _DEFAULT_HALF_SPREAD_BPS
    try:
        h = float(first_bar["h"])
        low = float(first_bar["l"])
    except (KeyError, TypeError, ValueError):
        return _DEFAULT_HALF_SPREAD_BPS
    if math.isnan(h) or math.isnan(low) or h < low:
        return _DEFAULT_HALF_SPREAD_BPS
    mid = (h + low) / 2.0
    if mid <= 0.0:
        return _DEFAULT_HALF_SPREAD_BPS
    return 10_000.0 * 0.5 * (h - low) / mid


def arm_haircut_bps(arm: str, *, market_cap: float | None, first_rth_bar: Mapping[str, Any] | None) -> float:
    if arm in RESTING_LIMIT_ARMS:
        return 0.0
    if arm in ALWAYS_FILL_ARMS:
        return half_spread_bps_from_bar(first_rth_bar) + impact_bps_for_mcap(market_cap)
    raise ValueError(f"unknown arm: {arm!r}")


def apply_haircut_to_excess(
    raw_excess: float | None,
    *,
    arm: str,
    market_cap: float | None,
    first_rth_bar: Mapping[str, Any] | None,
) -> float | None:
    if raw_excess is None:
        return None
    return raw_excess - arm_haircut_bps(arm, market_cap=market_cap, first_rth_bar=first_rth_bar) / 10_000.0
```

- [ ] **Step 4: Run → GREEN**, then **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/feedback/execution_cost.py apps/alphalens-research/tests/feedback/test_execution_cost.py
git commit -m "feat(feedback): per-arm execution-cost haircut for the entry-grid (Faza 0)"
```

---

### Task 2: `entry_primitives.py` — non-touch fill primitives (`market_at_arrival`, `vwap_arrival`) + `ArmFill`

**Files:**
- Create: `apps/alphalens-pipeline/alphalens_pipeline/thematic/trade_setup/entry_primitives.py`
- Test: `apps/alphalens-pipeline/tests/thematic/trade_setup/test_entry_primitives.py`

**Interfaces:** Consumes `bar_window.ARRIVAL_VWAP_WINDOW_MIN` + `bar_window._window_vwap`. Produces `ArmFill`, `market_at_arrival_fill`, `vwap_arrival_fill`.

- [ ] **Step 1: Write the failing tests** (concrete, from recon)

```python
# apps/alphalens-pipeline/tests/thematic/trade_setup/test_entry_primitives.py
import unittest

from alphalens_pipeline.thematic.trade_setup.entry_primitives import (
    ArmFill,
    market_at_arrival_fill,
    vwap_arrival_fill,
)

_OPEN = 1_700_000_000_000  # arrival session open (epoch ms)
_MIN = 60_000


def _bar(ts, o, h, low, c, v=1000.0):
    return {"t": ts, "o": o, "h": h, "l": low, "c": c, "v": v}


class TestMarketAtArrival(unittest.TestCase):
    def _close(self):
        return _OPEN + 6 * 3600 * 1000  # ~6h later

    def test_fills_at_first_rth_open_gap_up(self):
        bars = [_bar(_OPEN, 110.0, 111.0, 109.0, 110.5), _bar(_OPEN + _MIN, 110.5, 112.0, 110.0, 111.0)]
        f = market_at_arrival_fill(bars, arrival_open_ms=_OPEN, arrival_close_ms=self._close())
        self.assertEqual(f.status, "OK")
        self.assertEqual(f.fill_price, 110.0)  # open of first in-window bar, not low/close
        self.assertFalse(f.late_open)

    def test_fills_at_open_gap_down(self):
        bars = [_bar(_OPEN, 92.0, 93.0, 91.0, 92.5)]
        f = market_at_arrival_fill(bars, arrival_open_ms=_OPEN, arrival_close_ms=self._close())
        self.assertEqual(f.fill_price, 92.0)

    def test_ignores_premarket_bar(self):
        bars = [_bar(_OPEN - _MIN, 999.0, 999.0, 999.0, 999.0), _bar(_OPEN, 110.0, 111.0, 109.0, 110.5)]
        f = market_at_arrival_fill(bars, arrival_open_ms=_OPEN, arrival_close_ms=self._close())
        self.assertEqual(f.fill_price, 110.0)  # pre-market 999 ignored

    def test_no_bar_in_window_is_no_fill_not_next_session(self):
        next_session = _OPEN + 24 * 3600 * 1000
        bars = [_bar(next_session, 50.0, 51.0, 49.0, 50.0)]
        f = market_at_arrival_fill(bars, arrival_open_ms=_OPEN, arrival_close_ms=self._close())
        self.assertEqual(f.status, "NO_FILL")
        self.assertIsNone(f.fill_price)

    def test_late_open_flag(self):
        bars = [_bar(_OPEN + 45 * _MIN, 110.0, 111.0, 109.0, 110.5)]
        f = market_at_arrival_fill(bars, arrival_open_ms=_OPEN, arrival_close_ms=self._close())
        self.assertEqual(f.status, "OK")
        self.assertTrue(f.late_open)


class TestVwapArrival(unittest.TestCase):
    def _close(self):
        return _OPEN + 6 * 3600 * 1000

    def test_volume_weighted_vwap(self):
        # two 1-min bars in the 30-min window; typical price=(h+l+c)/3 per bar_window._window_vwap
        bars = [_bar(_OPEN, 100.0, 100.0, 100.0, 100.0, v=100.0), _bar(_OPEN + _MIN, 110.0, 110.0, 110.0, 110.0, v=300.0)]
        f = vwap_arrival_fill(bars, arrival_open_ms=_OPEN)
        self.assertEqual(f.status, "OK")
        self.assertAlmostEqual(f.fill_price, (100.0 * 100.0 + 110.0 * 300.0) / 400.0)

    def test_empty_window_no_fill(self):
        bars = [_bar(_OPEN + 40 * _MIN, 100.0, 100.0, 100.0, 100.0)]  # outside 30-min window
        f = vwap_arrival_fill(bars, arrival_open_ms=_OPEN)
        self.assertEqual(f.status, "NO_FILL")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → RED.**

- [ ] **Step 3: Implement.** Create `entry_primitives.py` with the two frozen dataclasses (`ArmFill`, and `ArmSetup` used by Task 3 — define both now) and the two fill functions. `market_at_arrival_fill`: filter bars to `arrival_open_ms <= t <= arrival_close_ms` (drops pre-market `t < arrival_open_ms` and any later session), pick the earliest by `t`; `NO_FILL` if none; `fill_price = float(bar["o"])`; `late_open = first_ts > arrival_open_ms + tolerance` (tolerance = 2 min). `vwap_arrival_fill`: `fill = bar_window._window_vwap(bars, arrival_open_ms, arrival_open_ms + window_min*60_000)`; `NO_FILL` when `_window_vwap` returns `None`. Verify `_window_vwap`'s typical-price + zero-volume-mean behavior at execution time and align the VWAP test's expected value to it.

- [ ] **Step 4: Run → GREEN.** **Step 5: Commit** (`feat(trade_setup): entry-grid non-touch fill primitives + ArmFill/ArmSetup`).

---

### Task 3: `entry_primitives.py` — arm builders + own-stop (`baseline`, `narrow_tiers`, `single_at_close`)

**Files:** Modify `entry_primitives.py`; extend `test_entry_primitives.py`.

**Interfaces:** Consumes `builder._jitter_stop`, `builder._STOP_ATR_BUFFER`, `builder._DISASTER_FLOOR_FRAC`, `ladder.build_entry_tiers` + the `_MIN_SPACING_MULT`/`_MIN_STOP_DIST_MULT` params (verify exact names at execution). Produces `STOP_ATR_BUFFER_K`, `arm_disaster_stop`, `build_baseline_arm`, `build_narrow_tiers_arm`, `build_single_at_close_arm`.

- [ ] **Step 1: Write failing tests**

```python
# append to test_entry_primitives.py
from alphalens_pipeline.thematic.trade_setup.entry_primitives import (
    arm_disaster_stop,
    build_narrow_tiers_arm,
    build_single_at_close_arm,
)


class TestArmBuilders(unittest.TestCase):
    def test_narrow_tiers_collapse_under_default_spacing(self):
        # default 0.5*ATR spacing collapses 0.10/0.175/0.25*ATR tiers to <=1
        arm = build_narrow_tiers_arm(close=100.0, atr=10.0, min_spacing_mult=0.5, min_stop_dist_mult=0.5)
        self.assertTrue(arm.geometry_collapsed)

    def test_narrow_tiers_keep_all_three_with_small_spacing(self):
        arm = build_narrow_tiers_arm(close=100.0, atr=10.0, min_spacing_mult=0.05, min_stop_dist_mult=0.05)
        self.assertEqual(len(arm.entry_tiers), 3)
        self.assertFalse(arm.geometry_collapsed)

    def test_single_at_close_one_tier_at_close(self):
        arm = build_single_at_close_arm(close=100.0, atr=5.0)
        self.assertEqual(arm.status, "OK")
        self.assertEqual(len(arm.entry_tiers), 1)
        self.assertAlmostEqual(float(arm.entry_tiers[0]["limit"]), 100.0)

    def test_arm_disaster_stop_below_blended_and_positive_risk(self):
        stop = arm_disaster_stop(arm_blended=100.0, atr=5.0, close=100.0)
        self.assertLess(stop, 100.0)
        self.assertGreater(100.0 - stop, 0.0)
```

- [ ] **Step 2: RED.** **Step 3: Implement** `arm_disaster_stop` (= `builder._jitter_stop(close, arm_blended - k*atr, atr)` floored at `arm_blended * builder._DISASTER_FLOOR_FRAC`; re-validate the −25% floor → set `ArmSetup.status="BAD_GEOMETRY"` when the floored stop violates min-stop-distance), and the three builders (baseline = verbatim passthrough of the source setup; narrow_tiers = `ladder.build_entry_tiers(close, atr, candidates=[(close-m*atr,"narrow") for m in mults], stop=..., min_spacing_mult=..., min_stop_dist_mult=...)`, `geometry_collapsed = len(chosen) < len(mults)`; single_at_close = one tier at `close - just_below_mult*atr`). Surface `NO_STRUCTURE` when `atr<=0` / `close<=0`. **Step 4: GREEN.** **Step 5: Commit** (`feat(trade_setup): entry-grid arm builders + own-stop geometry`).

---

### Task 4: `ladder_replay.py` — `_with_disaster_stop` + `_replay_synthetic_fill`

**Files:** Modify `apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_replay.py`; Test `apps/alphalens-pipeline/tests/feedback/test_replay_entry_grid.py`.

**Interfaces:** Consumes existing `_with_entry_tiers`, `replay_ladder`, `_LadderWalk`, `parse_ladder`. Produces `_with_disaster_stop`, `_replay_synthetic_fill`.

- [ ] **Step 1: Write failing tests** — `_replay_synthetic_fill` fills at the injected `fill_price` even when no bar low ≤ fill_price (touch gate bypassed); `own_stop` is the SL used (not the source `disaster_stop`); `exit_mark` comes from the fixed absolute TP target; `_with_disaster_stop` swaps only `disaster_stop`, leaves `entry_tiers`/`tp_tranches` untouched, does not mutate the source dict. (Use the synthetic-bar helpers from `test_ladder_chart_payload.py` as a model.)

- [ ] **Step 2: RED.** **Step 3: Implement** `_with_disaster_stop` (shallow copy swapping `disaster_stop`) and `_replay_synthetic_fill` (pre-seed a single filled `_Level` at `fill_price` into a `_LadderWalk`, skipping the `low <= limit` gate, then run the existing per-bar exit walk with `own_stop` + `position_expiry_ms`; return a `LadderOutcome` whose terminal state yields the exit mark). **Step 4: GREEN.** **Step 5: Commit** (`feat(feedback): synthetic-fill replay + disaster-stop swap for entry-grid`).

---

### Task 5: `ladder_replay.py` — `replay_entry_grid` (5-arm reward + haircut)

**Files:** Modify `ladder_replay.py`; extend `test_replay_entry_grid.py`.

**Interfaces:** Consumes Tasks 1-4 + `entry_primitives.*` + `execution_cost.apply_haircut_to_excess`. Produces `ENTRY_GRID_ARMS`, `replay_entry_grid`.

- [ ] **Step 1: Write failing tests** (the load-bearing behavior pins):
  - **touch-arm reward** = hand-computed `(exit_mark - blended)/blended - benchmark_window_return`, and is **NOT** `replay_ladder(...).realized_r` (proves realized_r dropped).
  - **NO_FILL = cash identical across all 5 arms** = `-benchmark_window_return` (empty arrival window → every arm cash).
  - **BAD_GEOMETRY parity**: a forced-BAD_GEOMETRY arm gets the same cash handling as NO_FILL; baseline same event computes normally.
  - **exit-held-fixed**: two arms with different entry denominators both reaching absolute TP=110 share the same `exit_mark`; only the denominator differs.
  - **haircut asymmetry**: identical prices → `market_at_arrival` reward strictly < `baseline`.
  - **shared unevaluability**: missing bars / un-elapsed horizon → ALL arms `None`.

- [ ] **Step 2: RED.** **Step 3: Implement** `replay_entry_grid`: for each arm in `ENTRY_GRID_ARMS`, build the arm (touch arms via `entry_primitives.build_*` → `_with_entry_tiers` + `_with_disaster_stop` → `replay_ladder`; non-touch arms via `market_at_arrival_fill`/`vwap_arrival_fill` → `_replay_synthetic_fill` with `own_stop = arm_disaster_stop(fill_price, atr, close)`), derive `exit_mark` + `arm_blended` from the outcome, compute `raw = (exit_mark - arm_blended)/arm_blended - benchmark_window_return`, then `apply_haircut_to_excess(raw, arm=arm, market_cap=market_cap, first_rth_bar=<first arrival bar>)`. Map `NO_FILL`/`BAD_GEOMETRY` → `-benchmark_window_return` identically. Return `None` for **all** arms when the event is structurally unevaluable (no bars / unparseable setup / `benchmark_window_return is None`). Reuse the existing implausible-return split guard. **Step 4: GREEN** (+ run the full feedback suite). **Step 5: Commit** (`feat(feedback): replay_entry_grid — 5-arm counterfactual entry-grid reward`).

---

### Task 6: `fixed_horizon.py` — `day_block_bootstrap_ci`

**Files:** Modify `apps/alphalens-research/alphalens_research/diagnostics/fixed_horizon.py`; extend `apps/alphalens-research/tests/test_fixed_horizon.py`.

**Interfaces:** Produces `day_block_bootstrap_ci`. Leaves `bootstrap_ci` untouched.

- [ ] **Step 1: Write failing tests**
  - empty / all-None → `(None, None, None)`;
  - single non-empty day with many rows → degenerate `(m, m, m)` (n_eff=1) — the whole point of day-blocking;
  - **grand-mean equality**: `day_block_bootstrap_ci({d1:[...], d2:[...]})[1] == bootstrap_ci(flattened)[1]` (NOT mean-of-day-means);
  - **CI-width contrast**: 5 rows all in ONE day → `day_block` degenerate while `bootstrap_ci` on the same 5 rows is non-degenerate;
  - two single-row days → a real (non-degenerate) CI;
  - determinism by seed; different seeds differ; None dropped within a day.

- [ ] **Step 2: RED.** **Step 3: Implement** `day_block_bootstrap_ci`: list the days (dict insertion order for determinism), each replicate resamples `len(days)` days with replacement (seeded `random.Random(seed)` # NOSONAR — non-crypto), pools the drawn days' non-None values, takes the replicate mean; point estimate = grand mean over all non-None rows; percentile CI tails like `bootstrap_ci`. **Step 4: GREEN.** **Step 5: Commit** (`feat(diagnostics): day-block bootstrap CI (resample days, grand-mean)`).

---

### Task 7: `diagnose_entry_grid.py` — Faza-0 offline script

**Files:** Create `apps/alphalens-research/scripts/diagnose_entry_grid.py`; Test `apps/alphalens-research/tests/diagnostics/test_diagnose_entry_grid.py` (test the collection helpers, not the `main()` I/O).

**Interfaces:** Consumes `edge_stores.*`, `replay_entry_grid`, `day_block_bootstrap_ci`, `fixed_horizon.car_for_event` building blocks. Produces a `main()` + small pure helpers `_common_support(rows)` and `_market_cap_index(briefs)`.

- [ ] **Step 1: Write failing tests** for the pure helpers:
  - `_common_support`: keeps only events where all 5 arms are non-None; an event with 4/5 is dropped;
  - **equal-fill-rate simulation proof**: two arms with the same fill-conditional reward but different fill rates → equal mean on the common-support subset, unequal on the full set (pins why common-support is the headline);
  - `_market_cap_index`: maps `(brief_date, TICKER) -> market_cap` from the briefs store.

- [ ] **Step 2: RED.** **Step 3: Implement** `main()` mirroring `diagnose_selection.main()`: load stores, build `_market_cap_index`, for each plannable event read the cached minute bars (re-use the `population_ladders/bars/<TICKER>_<arrival>.parquet` read path — verify the exact helper in `population_ladder_monitor`/`diagnose_nofill` at execution), compute `arrival_open_ms`/`arrival_close_ms` (session open/close) + SPY `benchmark_window_return` from grouped-daily closes over the k=10 window, call `replay_entry_grid`, collect rows, apply `_common_support`, and print per-arm mean + `day_block_bootstrap_ci` (keyed on arrival session) for BOTH the cost-adjusted and a raw (pre-haircut) pass; report full-coverage N, common-support N, and the dropped-for-missing-bars count. CLI args mirror `diagnose_selection` + `--k` (default 10). **No stamped column.** **Step 4: GREEN** + a live smoke `--help`. **Step 5: Commit** (`feat(diagnostics): diagnose_entry_grid offline Faza-0 comparison script`).

---

## GO/NO-GO execution (VPS, after merge)

```bash
cd ~/AlphaLens && .venv/bin/python apps/alphalens-research/scripts/diagnose_entry_grid.py
```
Read the per-arm cost-adjusted market-excess (common-support headline, day-block CIs), pre- and post-haircut. **GO to Faza 1/2** if a static arm (esp. `market_at_arrival` / `vwap_arrival`) beats `baseline` after the haircut with a lower-CI-bound separation; **else** record which arm (if any) wins and whether the dip-buy baseline is simply not improvable at this N.

## Self-review

- **Spec coverage:** Tasks 1-7 cover every layer of the design memo §3-§6 Faza-0 scope (arms, fill primitives, own stops, reward incl. NO_FILL=cash + exit-fixed + equal-size, execution-cost haircut, day-block bootstrap, common-support, offline script). Policy/parquet-column/config-version correctly EXCLUDED (memo defers them).
- **Placeholder scan:** pure functions (Tasks 1, 6) carry full code; Tasks 2-5, 7 carry full test code + exact signatures + reuse targets + algorithm; the few "verify exact internal helper name at execution" notes (`_jitter_stop`, `_window_vwap` typical-price, the bars-read path) are flagged explicitly because they depend on internals best confirmed against live code during TDD — not silent gaps.
- **Type consistency:** `replay_entry_grid` returns `dict[str, float | None]` everywhere (contract resolved spec-1 vs spec-4); `apply_haircut_to_excess` takes the raw `market_cap` scalar + `first_rth_bar` (buckets internally); `day_block_bootstrap_ci` takes `values_by_day` and returns the same `(lo, mean, hi)` tuple shape as `bootstrap_ci`.
- **Scope check:** single coherent deliverable (one offline GO/NO-GO number); no decomposition needed.
</content>
