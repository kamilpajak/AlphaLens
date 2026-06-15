# NO_FILL root-cause + metric-rethink — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Worktree:** This repo runs multiple concurrent sessions. Execute this plan in a dedicated `git worktree` off fresh `origin/main` (`git worktree add -b feature/nofill-diagnostics .claude/worktrees/nofill origin/main`), NOT in the main checkout. All commits below happen in that worktree.

**Goal:** A read-only, research-side diagnostic that explains *why* EDGE outcomes end as `NO_FILL` and turns the evidence into a metric decision (selection via `market_excess_return` vs ladder `realized_r`).

**Architecture:** A pure classification module (`alphalens_research/diagnostics/nofill.py`) with no I/O, driven by a thin script (`scripts/diagnose_nofill.py`) that reads three parquet stores on the VPS (`population_ladders`, `thematic_briefs`, `grouped_daily_history`) using existing `alphalens_pipeline` readers, reconstructs each NO_FILL outcome's entry-window price path, classifies the cause, and emits a table + aggregates. The numeric output feeds a hand-written memo. No Django/Postgres, no Polygon, no production-path change.

**Tech Stack:** Python 3.13, pandas, `alphalens_pipeline.{paper.calendar, paper.brief_loader, data.rs_history}`, unittest.

**Spec:** `docs/superpowers/specs/2026-06-15-nofill-rootcause-metric-rethink-design.md`

---

## File structure

- Create: `apps/alphalens-research/alphalens_research/diagnostics/nofill.py` — pure reconstruction + classification.
- Create: `apps/alphalens-research/scripts/diagnose_nofill.py` — thin I/O driver.
- Create: `apps/alphalens-research/tests/test_nofill_diagnostics.py` — unit tests.
- Read-only references: `apps/alphalens-pipeline/alphalens_pipeline/paper/calendar.py`, `.../paper/brief_loader.py`, `.../data/rs_history.py`, `.../feedback/ladder_replay.py` (parse_ladder keys).
- Output (runtime, not committed): `~/.alphalens/diagnostics/nofill_<date>.parquet` + the memo `docs/research/nofill_rootcause_metric_rethink_2026_06_15.md`.

## Module API (locked — used across tasks)

```python
# alphalens_research/diagnostics/nofill.py
from dataclasses import dataclass
from collections.abc import Mapping, Sequence

TOUCH_EPS = 0.0025          # mirrors population_ladder_monitor._TOUCH_EPS
GAP_UP_MARGIN = 0.03        # opening gap vs arrival anchor that counts as GAP_UP_ARRIVAL

CAUSE_DATA_GAP = "DATA_GAP"
CAUSE_AMBIGUOUS = "AMBIGUOUS"
CAUSE_TOUCHED_AFTER_TTL = "TOUCHED_AFTER_TTL"
CAUSE_GAP_UP_ARRIVAL = "GAP_UP_ARRIVAL"
CAUSE_MOMENTUM_RAN = "MOMENTUM_RAN"

@dataclass(frozen=True)
class NoFillReconstruction:
    e1: float | None
    e2: float | None
    e3: float | None
    stop: float | None
    min_low_in_window: float | None
    touched_e1: bool
    touched_e2: bool
    touched_e3: bool
    gap_to_e1: float | None
    days_to_first_touch: int | None
    arrival_drift: float | None
    window_complete: bool
    cause: str

def reconstruct(*, tiers, stop, reference_close, window_lows_highs,
                first_session_open, tail_min_low,
                touch_eps=TOUCH_EPS, gap_up_margin=GAP_UP_MARGIN) -> NoFillReconstruction: ...

def analyze_outcome_row(*, ticker, tiers, stop, reference_close,
                        window_sessions, tail_sessions, grouped_by_session,
                        touch_eps=TOUCH_EPS, gap_up_margin=GAP_UP_MARGIN) -> NoFillReconstruction: ...
```

- `tiers`: descending entry-tier prices `[E1, E2, E3]` (E1 = shallowest = highest), 0–3 long. `tiers[0]` = E1.
- `window_lows_highs`: per window session, `(low, high)` or `None` (missing snapshot / ticker absent). Order = session order.
- `grouped_by_session`: `{session_date: {TICKER_UPPER: {"o","h","l","c","v"}} | None}` (exactly `rs_history.read_grouped_day` output per session; `None` = snapshot not on disk).

**Cause precedence (first match wins):**
1. `DATA_GAP` — no E1, or `not window_complete` (any window session missing).
2. `AMBIGUOUS` — `min_low_in_window <= e1*(1+eps)` (daily path says fillable but the row is NO_FILL → daily-vs-minute / eps edge; escalate manually).
3. `TOUCHED_AFTER_TTL` — window never touched E1, but `tail_min_low <= e1*(1+eps)` (came back, too late).
4. `GAP_UP_ARRIVAL` — window never touched E1 and `arrival_drift > gap_up_margin` (ran away via an opening gap vs the arrival anchor).
5. `MOMENTUM_RAN` — window never touched E1, no opening gap (drifted away).

---

### Task 1: Pure module + MOMENTUM_RAN

**Files:**
- Create: `apps/alphalens-research/alphalens_research/diagnostics/nofill.py`
- Test: `apps/alphalens-research/tests/test_nofill_diagnostics.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-research/tests/test_nofill_diagnostics.py
"""Unit tests for the NO_FILL root-cause reconstruction (pure, no I/O)."""

from __future__ import annotations

import unittest

from alphalens_research.diagnostics import nofill


class TestReconstruct(unittest.TestCase):
    def test_momentum_ran_when_low_never_reaches_e1(self):
        # E1=99, E2=97, E3=95; every session low stays above E1; no gap, no tail touch.
        r = nofill.reconstruct(
            tiers=[99.0, 97.0, 95.0],
            stop=90.0,
            reference_close=100.0,
            window_lows_highs=[(100.5, 105.0), (101.0, 106.0), (102.0, 107.0)],
            first_session_open=100.4,   # drift = +0.4% < 3% -> not a gap-up
            tail_min_low=103.0,         # tail also never dips to E1
        )
        self.assertEqual(r.cause, nofill.CAUSE_MOMENTUM_RAN)
        self.assertFalse(r.touched_e1)
        self.assertIsNone(r.days_to_first_touch)
        self.assertTrue(r.window_complete)
        self.assertEqual(r.e1, 99.0)
        self.assertAlmostEqual(r.min_low_in_window, 100.5)
        self.assertAlmostEqual(r.gap_to_e1, (100.5 - 99.0) / 99.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_nofill_diagnostics -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'alphalens_research.diagnostics.nofill'`.

- [ ] **Step 3: Write minimal implementation**

```python
# apps/alphalens-research/alphalens_research/diagnostics/nofill.py
"""Pure root-cause reconstruction for NO_FILL EDGE outcomes (no I/O).

Given a candidate's entry tiers and the daily [low, high] path over its
entry-TTL window (+ a short post-window tail), classify WHY the dip-buy entry
never filled. See docs/superpowers/specs/2026-06-15-nofill-rootcause-metric-rethink-design.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

TOUCH_EPS = 0.0025  # mirrors alphalens_pipeline...population_ladder_monitor._TOUCH_EPS
GAP_UP_MARGIN = 0.03  # opening gap vs arrival anchor that counts as GAP_UP_ARRIVAL

CAUSE_DATA_GAP = "DATA_GAP"
CAUSE_AMBIGUOUS = "AMBIGUOUS"
CAUSE_TOUCHED_AFTER_TTL = "TOUCHED_AFTER_TTL"
CAUSE_GAP_UP_ARRIVAL = "GAP_UP_ARRIVAL"
CAUSE_MOMENTUM_RAN = "MOMENTUM_RAN"


@dataclass(frozen=True)
class NoFillReconstruction:
    e1: float | None
    e2: float | None
    e3: float | None
    stop: float | None
    min_low_in_window: float | None
    touched_e1: bool
    touched_e2: bool
    touched_e3: bool
    gap_to_e1: float | None
    days_to_first_touch: int | None
    arrival_drift: float | None
    window_complete: bool
    cause: str


def _tier(tiers: Sequence[float], i: int) -> float | None:
    return float(tiers[i]) if tiers is not None and len(tiers) > i else None


def reconstruct(
    *,
    tiers: Sequence[float],
    stop: float | None,
    reference_close: float | None,
    window_lows_highs: Sequence[tuple[float, float] | None],
    first_session_open: float | None,
    tail_min_low: float | None,
    touch_eps: float = TOUCH_EPS,
    gap_up_margin: float = GAP_UP_MARGIN,
) -> NoFillReconstruction:
    e1 = _tier(tiers, 0)
    e2 = _tier(tiers, 1)
    e3 = _tier(tiers, 2)
    stop_f = float(stop) if stop is not None else None

    present = [lh for lh in window_lows_highs if lh is not None]
    window_complete = bool(window_lows_highs) and len(present) == len(window_lows_highs)
    min_low = min((lh[0] for lh in present), default=None)

    def _touched(level: float | None) -> bool:
        return level is not None and min_low is not None and min_low <= level * (1.0 + touch_eps)

    touched_e1 = _touched(e1)
    touched_e2 = _touched(e2)
    touched_e3 = _touched(e3)

    days_to_first_touch: int | None = None
    if e1 is not None:
        for i, lh in enumerate(window_lows_highs):
            if lh is not None and lh[0] <= e1 * (1.0 + touch_eps):
                days_to_first_touch = i + 1
                break

    gap_to_e1 = (min_low - e1) / e1 if (min_low is not None and e1) else None
    arrival_drift = (
        (first_session_open - reference_close) / reference_close
        if (first_session_open is not None and reference_close)
        else None
    )

    cause = _classify(
        e1=e1,
        window_complete=window_complete,
        min_low=min_low,
        tail_min_low=tail_min_low,
        arrival_drift=arrival_drift,
        touch_eps=touch_eps,
        gap_up_margin=gap_up_margin,
    )

    return NoFillReconstruction(
        e1=e1, e2=e2, e3=e3, stop=stop_f,
        min_low_in_window=min_low,
        touched_e1=touched_e1, touched_e2=touched_e2, touched_e3=touched_e3,
        gap_to_e1=gap_to_e1, days_to_first_touch=days_to_first_touch,
        arrival_drift=arrival_drift, window_complete=window_complete, cause=cause,
    )


def _classify(*, e1, window_complete, min_low, tail_min_low, arrival_drift,
              touch_eps, gap_up_margin) -> str:
    if e1 is None or not window_complete or min_low is None:
        return CAUSE_DATA_GAP
    if min_low <= e1 * (1.0 + touch_eps):
        return CAUSE_AMBIGUOUS
    if tail_min_low is not None and tail_min_low <= e1 * (1.0 + touch_eps):
        return CAUSE_TOUCHED_AFTER_TTL
    if arrival_drift is not None and arrival_drift > gap_up_margin:
        return CAUSE_GAP_UP_ARRIVAL
    return CAUSE_MOMENTUM_RAN
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_nofill_diagnostics -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/alphalens_research/diagnostics/nofill.py apps/alphalens-research/tests/test_nofill_diagnostics.py
git commit -m "feat(diagnostics): NO_FILL reconstruction core + MOMENTUM_RAN classification"
```

---

### Task 2: AMBIGUOUS + touch flags + days_to_first_touch

**Files:**
- Test: `apps/alphalens-research/tests/test_nofill_diagnostics.py`

- [ ] **Step 1: Write the failing test** (add method to `TestReconstruct`)

```python
    def test_ambiguous_when_daily_low_reaches_e1(self):
        # Session 2 low dips to 98.5 < E1=99 -> daily path says fillable, yet the
        # row is NO_FILL -> AMBIGUOUS (daily-vs-minute discrepancy, escalate).
        r = nofill.reconstruct(
            tiers=[99.0, 97.0, 95.0],
            stop=90.0,
            reference_close=100.0,
            window_lows_highs=[(100.0, 105.0), (98.5, 101.0), (99.5, 103.0)],
            first_session_open=100.1,
            tail_min_low=None,
        )
        self.assertEqual(r.cause, nofill.CAUSE_AMBIGUOUS)
        self.assertTrue(r.touched_e1)
        self.assertFalse(r.touched_e3)            # 98.5 not <= 95*(1.0025)
        self.assertEqual(r.days_to_first_touch, 2)
        self.assertAlmostEqual(r.min_low_in_window, 98.5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_nofill_diagnostics.TestReconstruct.test_ambiguous_when_daily_low_reaches_e1 -v`
Expected: PASS already (logic implemented in Task 1) — this test pins the AMBIGUOUS branch + touch flags. If it FAILS, fix the implementation to match the precedence in the Module API.

- [ ] **Step 3: No new implementation needed** (Task 1 covers it). If the test failed, correct `_classify` / touch logic now.

- [ ] **Step 4: Run the full test file**

Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_nofill_diagnostics -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/tests/test_nofill_diagnostics.py
git commit -m "test(diagnostics): pin AMBIGUOUS branch + touch flags + days_to_first_touch"
```

---

### Task 3: TOUCHED_AFTER_TTL

**Files:**
- Test: `apps/alphalens-research/tests/test_nofill_diagnostics.py`

- [ ] **Step 1: Write the failing test**

```python
    def test_touched_after_ttl_when_only_tail_dips_to_e1(self):
        # Window never reaches E1=99 (min 100.0), but the post-window tail dips to
        # 98.0 -> the dip-buy would have filled just after the 7-session TTL.
        r = nofill.reconstruct(
            tiers=[99.0, 97.0, 95.0],
            stop=90.0,
            reference_close=100.0,
            window_lows_highs=[(100.0, 104.0), (100.5, 105.0), (101.0, 106.0)],
            first_session_open=100.2,
            tail_min_low=98.0,
        )
        self.assertEqual(r.cause, nofill.CAUSE_TOUCHED_AFTER_TTL)
        self.assertFalse(r.touched_e1)
        self.assertIsNone(r.days_to_first_touch)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_nofill_diagnostics.TestReconstruct.test_touched_after_ttl_when_only_tail_dips_to_e1 -v`
Expected: PASS if Task 1 precedence is correct. (This pins branch 3 ahead of GAP_UP/MOMENTUM.)

- [ ] **Step 3: No new implementation** unless it failed; then fix precedence.

- [ ] **Step 4: Run the full test file**

Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_nofill_diagnostics -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/tests/test_nofill_diagnostics.py
git commit -m "test(diagnostics): pin TOUCHED_AFTER_TTL precedence over gap/momentum"
```

---

### Task 4: GAP_UP_ARRIVAL + arrival_drift + DATA_GAP

**Files:**
- Test: `apps/alphalens-research/tests/test_nofill_diagnostics.py`

- [ ] **Step 1: Write the failing tests**

```python
    def test_gap_up_arrival_when_open_jumps_above_anchor(self):
        # Window never reaches E1, no tail touch, but arrival opened +5% vs anchor.
        r = nofill.reconstruct(
            tiers=[99.0, 97.0, 95.0],
            stop=90.0,
            reference_close=100.0,
            window_lows_highs=[(104.0, 108.0), (105.0, 109.0), (106.0, 110.0)],
            first_session_open=105.0,     # drift = +5% > 3%
            tail_min_low=104.0,
        )
        self.assertEqual(r.cause, nofill.CAUSE_GAP_UP_ARRIVAL)
        self.assertAlmostEqual(r.arrival_drift, 0.05)

    def test_data_gap_when_a_window_session_is_missing(self):
        r = nofill.reconstruct(
            tiers=[99.0, 97.0, 95.0],
            stop=90.0,
            reference_close=100.0,
            window_lows_highs=[(101.0, 105.0), None, (102.0, 106.0)],  # one snapshot absent
            first_session_open=100.5,
            tail_min_low=None,
        )
        self.assertEqual(r.cause, nofill.CAUSE_DATA_GAP)
        self.assertFalse(r.window_complete)

    def test_data_gap_when_no_entry_tier(self):
        r = nofill.reconstruct(
            tiers=[], stop=None, reference_close=100.0,
            window_lows_highs=[(101.0, 105.0)],
            first_session_open=100.5, tail_min_low=None,
        )
        self.assertEqual(r.cause, nofill.CAUSE_DATA_GAP)
        self.assertIsNone(r.e1)
```

- [ ] **Step 2: Run tests to verify**

Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_nofill_diagnostics -v`
Expected: PASS (6 tests). If GAP_UP or DATA_GAP fail, correct `_classify`.

- [ ] **Step 3: No new implementation** unless a test failed.

- [ ] **Step 4: Re-run** (same command) — Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/tests/test_nofill_diagnostics.py
git commit -m "test(diagnostics): pin GAP_UP_ARRIVAL + DATA_GAP branches"
```

---

### Task 5: `analyze_outcome_row` — extract path from grouped-daily snapshots

**Files:**
- Modify: `apps/alphalens-research/alphalens_research/diagnostics/nofill.py`
- Test: `apps/alphalens-research/tests/test_nofill_diagnostics.py`

- [ ] **Step 1: Write the failing test** (new class)

```python
import datetime as dt


class TestAnalyzeOutcomeRow(unittest.TestCase):
    def _grouped(self, low, high, open_=None):
        bar = {"o": open_ if open_ is not None else high, "h": high, "l": low, "c": high, "v": 1.0}
        return {"AAA": bar}

    def test_extracts_window_path_and_classifies_momentum(self):
        w = [dt.date(2026, 5, 4), dt.date(2026, 5, 5), dt.date(2026, 5, 6)]
        tail = [dt.date(2026, 5, 7)]
        grouped = {
            w[0]: self._grouped(100.5, 105.0, open_=100.4),
            w[1]: self._grouped(101.0, 106.0),
            w[2]: self._grouped(102.0, 107.0),
            tail[0]: self._grouped(103.0, 108.0),
        }
        r = nofill.analyze_outcome_row(
            ticker="aaa",
            tiers=[99.0, 97.0, 95.0],
            stop=90.0,
            reference_close=100.0,
            window_sessions=w,
            tail_sessions=tail,
            grouped_by_session=grouped,
        )
        self.assertEqual(r.cause, nofill.CAUSE_MOMENTUM_RAN)
        self.assertAlmostEqual(r.min_low_in_window, 100.5)

    def test_missing_snapshot_is_data_gap(self):
        w = [dt.date(2026, 5, 4), dt.date(2026, 5, 5)]
        grouped = {w[0]: self._grouped(101.0, 105.0), w[1]: None}  # second snapshot not on disk
        r = nofill.analyze_outcome_row(
            ticker="AAA", tiers=[99.0], stop=90.0, reference_close=100.0,
            window_sessions=w, tail_sessions=[], grouped_by_session=grouped,
        )
        self.assertEqual(r.cause, nofill.CAUSE_DATA_GAP)

    def test_ticker_absent_from_present_snapshot_is_missing(self):
        w = [dt.date(2026, 5, 4)]
        grouped = {w[0]: {"BBB": {"o": 10, "h": 11, "l": 9, "c": 10, "v": 1}}}  # AAA not traded
        r = nofill.analyze_outcome_row(
            ticker="AAA", tiers=[99.0], stop=90.0, reference_close=100.0,
            window_sessions=w, tail_sessions=[], grouped_by_session=grouped,
        )
        self.assertEqual(r.cause, nofill.CAUSE_DATA_GAP)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_nofill_diagnostics.TestAnalyzeOutcomeRow -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'analyze_outcome_row'`.

- [ ] **Step 3: Add the implementation** (append to `nofill.py`)

```python
def _bar_low_high_open(
    snapshot: Mapping[str, Mapping[str, object]] | None, ticker: str
) -> tuple[float, float, float] | None:
    """Pull (low, high, open) for ``ticker`` from one grouped-daily snapshot.

    ``snapshot is None`` means the session is not on disk; a present snapshot
    missing the ticker means it did not trade that session. Either way -> None.
    """
    if snapshot is None:
        return None
    bar = snapshot.get(ticker.upper())
    if not bar:
        return None
    try:
        return float(bar["l"]), float(bar["h"]), float(bar["o"])
    except (KeyError, TypeError, ValueError):
        return None


def analyze_outcome_row(
    *,
    ticker: str,
    tiers: Sequence[float],
    stop: float | None,
    reference_close: float | None,
    window_sessions: Sequence["object"],
    tail_sessions: Sequence["object"],
    grouped_by_session: Mapping[object, Mapping[str, Mapping[str, object]] | None],
    touch_eps: float = TOUCH_EPS,
    gap_up_margin: float = GAP_UP_MARGIN,
) -> NoFillReconstruction:
    """Build the window/tail price path for ``ticker`` from grouped-daily snapshots
    and classify the NO_FILL cause. Pure: ``grouped_by_session`` is already loaded."""
    window_lows_highs: list[tuple[float, float] | None] = []
    first_session_open: float | None = None
    for i, session in enumerate(window_sessions):
        lho = _bar_low_high_open(grouped_by_session.get(session), ticker)
        if lho is None:
            window_lows_highs.append(None)
            continue
        low, high, open_ = lho
        window_lows_highs.append((low, high))
        if i == 0:
            first_session_open = open_

    tail_lows: list[float] = []
    for session in tail_sessions:
        lho = _bar_low_high_open(grouped_by_session.get(session), ticker)
        if lho is not None:
            tail_lows.append(lho[0])
    tail_min_low = min(tail_lows) if tail_lows else None

    return reconstruct(
        tiers=tiers, stop=stop, reference_close=reference_close,
        window_lows_highs=window_lows_highs, first_session_open=first_session_open,
        tail_min_low=tail_min_low, touch_eps=touch_eps, gap_up_margin=gap_up_margin,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_nofill_diagnostics -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/alphalens_research/diagnostics/nofill.py apps/alphalens-research/tests/test_nofill_diagnostics.py
git commit -m "feat(diagnostics): analyze_outcome_row extracts path from grouped-daily snapshots"
```

---

### Task 6: Driver script — read stores, build windows, emit table + aggregates

**Files:**
- Create: `apps/alphalens-research/scripts/diagnose_nofill.py`

This is an I/O driver (no automated test — its per-row logic is the unit-tested `analyze_outcome_row`). Keep it thin.

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python
"""Diagnose WHY EDGE outcomes end as NO_FILL (read-only, research-side).

Reads three parquet stores under ~/.alphalens (population_ladders, thematic_briefs,
grouped_daily_history), reconstructs each NO_FILL outcome's entry-window price path,
classifies the cause, and prints population aggregates + writes a tidy table.

Run on the VPS (where the stores live) or against rsync'd copies:
    .venv/bin/python apps/alphalens-research/scripts/diagnose_nofill.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter
from pathlib import Path

import pandas as pd

from alphalens_pipeline.data import rs_history
from alphalens_pipeline.paper import brief_loader
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    session_on_or_after,
)
from alphalens_pipeline.paper.constants import DEFAULT_ORDER_TTL_DAYS

from alphalens_research.diagnostics import nofill

_HOME = Path.home() / ".alphalens"
_TAIL_SESSIONS = 10  # post-TTL window for TOUCHED_AFTER_TTL detection


def _tiers_and_stop(setup: dict | None) -> tuple[list[float], float | None]:
    """E1..E3 (descending) + disaster_stop from a decoded brief_trade_setup."""
    if not setup or setup.get("status") != "OK":
        return [], None
    raw = setup.get("entry_tiers") or []
    tiers: list[float] = []
    for t in raw:
        try:
            tiers.append(float(t["limit"]))
        except (KeyError, TypeError, ValueError):
            continue
    stop = setup.get("disaster_stop")
    try:
        stop_f = float(stop) if stop is not None else None
    except (TypeError, ValueError):
        stop_f = None
    return tiers, stop_f


def _load_store(store_dir: Path) -> pd.DataFrame:
    frames = []
    for path in sorted(store_dir.glob("*.parquet")):
        try:
            d = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        df = pd.read_parquet(path)
        df["brief_date"] = d
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _setup_index(briefs_dir: Path) -> dict[tuple[dt.date, str], dict]:
    out: dict[tuple[dt.date, str], dict] = {}
    briefs = _load_store(briefs_dir)
    if briefs.empty:
        return out
    for _, row in briefs.iterrows():
        ticker = str(row.get("ticker", "")).upper()
        if not ticker:
            continue
        raw = row.get("brief_trade_setup")
        setup = brief_loader._coerce_trade_setup(raw)
        if setup is not None:
            out[(row["brief_date"], ticker)] = setup
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ladders-dir", type=Path, default=_HOME / "population_ladders")
    ap.add_argument("--briefs-dir", type=Path, default=_HOME / "thematic_briefs")
    ap.add_argument("--grouped-root", type=Path, default=rs_history.DEFAULT_RS_HISTORY_ROOT)
    ap.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    ap.add_argument("--ttl", type=int, default=DEFAULT_ORDER_TTL_DAYS)
    ap.add_argument("--out", type=Path, default=_HOME / "diagnostics" / "nofill.parquet")
    args = ap.parse_args()

    outcomes = _load_store(args.ladders_dir)
    if outcomes.empty:
        print("no population-ladder outcomes found at", args.ladders_dir)
        return
    setups = _setup_index(args.briefs_dir)

    # Population mix over ALL rows (NO_FILL classification needs no maturity).
    mix = Counter(str(c or "") for c in outcomes.get("ladder_classification", []))
    print("classification mix (all rows):", dict(mix))

    nofill_rows = outcomes[outcomes["ladder_classification"] == "NO_FILL"].copy()
    print(f"NO_FILL rows: {len(nofill_rows)} / {len(outcomes)} total")

    grouped_cache: dict[dt.date, dict | None] = {}

    def grouped(session: dt.date):
        if session not in grouped_cache:
            grouped_cache[session] = rs_history.read_grouped_day(args.grouped_root, session)
        return grouped_cache[session]

    records: list[dict] = []
    for _, row in nofill_rows.iterrows():
        brief_date = row["brief_date"]
        ticker = str(row["ticker"]).upper()
        tiers, stop = _tiers_and_stop(setups.get((brief_date, ticker)))

        arrival = session_on_or_after(brief_date, args.exchange)
        window_sessions = [advance_trading_sessions(arrival, i, args.exchange) for i in range(args.ttl)]
        tail_sessions = [
            advance_trading_sessions(arrival, args.ttl + j, args.exchange) for j in range(_TAIL_SESSIONS)
        ]
        grouped_by_session = {
            s: grouped(s) for s in (*window_sessions, *tail_sessions)
        }

        r = nofill.analyze_outcome_row(
            ticker=ticker, tiers=tiers, stop=stop,
            reference_close=_as_float(row.get("reference_close")),
            window_sessions=window_sessions, tail_sessions=tail_sessions,
            grouped_by_session=grouped_by_session,
        )
        records.append({
            "brief_date": brief_date, "ticker": ticker,
            "cause": r.cause, "e1": r.e1, "stop": r.stop,
            "min_low_in_window": r.min_low_in_window, "gap_to_e1": r.gap_to_e1,
            "days_to_first_touch": r.days_to_first_touch, "arrival_drift": r.arrival_drift,
            "window_complete": r.window_complete,
            "market_excess_return": _as_float(row.get("market_excess_return")),
            "terminal": bool(row.get("terminal", False)),
            "ladder_config_version": str(row.get("ladder_config_version", "")),
        })

    table = pd.DataFrame.from_records(records)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(args.out, index=False)
    print("wrote", args.out, "rows:", len(table))

    print("\ncause distribution (NO_FILL):", dict(Counter(table["cause"])))

    # The lynchpin: NO_FILL cause x sign(market_excess) over MATURED rows only.
    matured = table[table["terminal"] & table["market_excess_return"].notna()]
    if not matured.empty:
        matured = matured.assign(excess_sign=matured["market_excess_return"].apply(_sign))
        print("\nmatured NO_FILL  cause x sign(market_excess):")
        print(pd.crosstab(matured["cause"], matured["excess_sign"]))
    else:
        print("\nno matured NO_FILL rows with market_excess yet")


def _as_float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def _sign(x: float) -> str:
    return "pos" if x > 0 else ("neg" if x < 0 else "zero")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it imports and shows help (no data needed)**

Run: `cd apps/alphalens-research && uv run python scripts/diagnose_nofill.py --help`
Expected: argparse help prints; no ImportError.

- [ ] **Step 3: Lint**

Run: `cd /Users/jacoren/Developer/Personal/AlphaLens && uv run ruff check apps/alphalens-research/scripts/diagnose_nofill.py apps/alphalens-research/alphalens_research/diagnostics/nofill.py`
Expected: no errors. Fix any (e.g. unused `json` import — remove it if ruff flags F401).

- [ ] **Step 4: Run the full diagnostics test suite once more**

Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_nofill_diagnostics -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/scripts/diagnose_nofill.py
git commit -m "feat(diagnostics): diagnose_nofill driver — stores -> cause table + aggregates"
```

---

### Task 7: Run against real data + write the memo

**Files:**
- Create: `docs/research/nofill_rootcause_metric_rethink_2026_06_15.md`

This task produces the answer. It runs on the VPS (stores live there) or against rsync'd stores.

- [ ] **Step 1: Make the stores reachable**

Either run on the VPS (`vault.kamilpajak.pl`, repo at `/home/jacoren/AlphaLens`, host venv), or rsync to the Mac:

```bash
rsync -av jacoren@vault.kamilpajak.pl:.alphalens/population_ladders/ ~/.alphalens/population_ladders/
rsync -av jacoren@vault.kamilpajak.pl:.alphalens/thematic_briefs/ ~/.alphalens/thematic_briefs/
rsync -av jacoren@vault.kamilpajak.pl:.alphalens/grouped_daily_history/ ~/.alphalens/grouped_daily_history/
```

- [ ] **Step 2: Run the diagnostic**

Run: `.venv/bin/python apps/alphalens-research/scripts/diagnose_nofill.py`
Expected: prints the classification mix, NO_FILL count, cause distribution, and the matured `cause x sign(market_excess)` crosstab; writes `~/.alphalens/diagnostics/nofill.parquet`.

- [ ] **Step 3: Sanity-check store coverage**

Confirm the grouped-daily store spans the oldest NO_FILL window (else early rows read as `DATA_GAP` spuriously). If `DATA_GAP` dominates, check the oldest `brief_date` vs the oldest `grouped_daily_history/*.parquet` stem and note the gap in the memo rather than over-claiming.

- [ ] **Step 4: Write the memo**

Create `docs/research/nofill_rootcause_metric_rethink_2026_06_15.md` with:
- the classification mix + NO_FILL count;
- the cause distribution table (MOMENTUM_RAN / GAP_UP_ARRIVAL / TOUCHED_AFTER_TTL / AMBIGUOUS / DATA_GAP);
- the matured `cause x sign(market_excess)` crosstab and the read: do NO_FILL names skew positive market_excess (the ladder discards winners)?;
- the **metric-rethink decision** (spec §7): recommend (i) `market_excess_return` as primary selection feedback with `realized_r` demoted to a separate entry-model question, or (ii) flag entry-model mis-specification for a future spec — whichever the numbers support;
- explicit caveats: small N for the crosstab; split-adjustment / DATA_GAP handling; daily (not minute) resolution; AMBIGUOUS rows need minute escalation.
- Header: `**Status:** COMPLETE` and link back to the spec + this plan.

- [ ] **Step 5: Commit**

```bash
git add docs/research/nofill_rootcause_metric_rethink_2026_06_15.md
git commit -m "docs(research): NO_FILL root-cause + metric-rethink findings"
```

---

## Self-review (completed during planning)

- **Spec coverage:** §3 data source → Tasks 5–6 (readers) + §3 readers used verbatim; §4 reconstruction → Tasks 1–5 (all derived columns present in `NoFillReconstruction`); §5 taxonomy → Tasks 1–4 (all 5 causes, precedence locked in Module API + `_classify`); §6 aggregation → Task 6 (mix, cause distribution, crosstab); §7 metric-rethink → Task 7 memo; §8 deliverables → all four files created; §9 testing → Tasks 1–5 (one test per cause + control); §10 risks → handled (DATA_GAP for missing/no-setup/absent-ticker; split caveat noted in Task 7; store-freshness check Task 7 Step 3).
- **Placeholder scan:** none — every code step has complete code; commands have expected output.
- **Type consistency:** `reconstruct` / `analyze_outcome_row` signatures and `NoFillReconstruction` fields match across Module API, Tasks 1–5, and the driver in Task 6; cause constants referenced consistently; `_coerce_trade_setup`, `read_grouped_day`, `session_on_or_after`, `advance_trading_sessions`, `DEFAULT_ORDER_TTL_DAYS` match the verified pipeline APIs.

## Notes

- `entry_tiers` keys are `limit` (not `price`) — verified against `ladder_replay.parse_ladder`.
- `read_grouped_day` keys are Polygon grouped-daily: `o/h/l/c/v`, symbol key upper-cased.
- The TTL parameter defaults to `DEFAULT_ORDER_TTL_DAYS` (7); per-row TTL is recorded in `ladder_config_version` — if a row's stamped TTL differs, the table's `ladder_config_version` column surfaces it (a v2 refinement could read TTL per row; out of scope here).
- Non-US tickers (XTKS/XHKG/…) are not in the Polygon US grouped-daily store → they classify as `DATA_GAP`; note their share in the memo.
