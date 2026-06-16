# Fixed-horizon CAR + survival-fill — Implementation Plan

> **For agentic workers:** implement task-by-task in the git worktree `.claude/worktrees/selection` (branch `feature/selection-car-survival`), created off fresh `origin/main`. Do NOT edit the main checkout. Do NOT `git commit/push` — leave changes in the working tree; the controller commits. Steps use `- [ ]`.

**Goal:** Two read-only, descriptive research diagnostics — fixed-horizon CAR (selection quality) and Kaplan-Meier survival-fill (entry quality) — plus a shared store-loader extraction.

**Architecture:** Pure, I/O-free modules (`fixed_horizon.py`, `fill_survival.py`) + extracted store loaders (`edge_stores.py`, also adopted by the existing `diagnose_nofill.py`) + a thin driver (`diagnose_selection.py`). Bootstrap CIs (seeded, deterministic), no t-tests; telemetry-only; daily closes/lows only; no production-path change.

**Tech Stack:** Python 3.13, pandas, `alphalens_pipeline.{data.rs_history, paper.brief_loader, paper.calendar, paper.constants}`, stdlib `random`, unittest.

**Spec:** `docs/superpowers/specs/2026-06-16-fixed-horizon-car-survival-fill-design.md`

---

## File structure

- Create: `apps/alphalens-research/alphalens_research/diagnostics/edge_stores.py` (shared loaders).
- Modify: `apps/alphalens-research/scripts/diagnose_nofill.py` (use the extracted loaders; no behaviour change).
- Create: `apps/alphalens-research/alphalens_research/diagnostics/fixed_horizon.py`.
- Create: `apps/alphalens-research/alphalens_research/diagnostics/fill_survival.py`.
- Create: `apps/alphalens-research/scripts/diagnose_selection.py` (driver).
- Create: `apps/alphalens-research/tests/test_fixed_horizon.py`, `tests/test_fill_survival.py`.
- Keep green: `apps/alphalens-research/tests/test_nofill_diagnostics.py`.

Test runner (from worktree): `cd apps/alphalens-research && uv run python -m unittest tests.<module> -v`. Lint/type: `cd <worktree-root> && uv run ruff check <files>` and `uv run pyright <files>`.

---

### Task 1: Extract shared store loaders (`edge_stores.py`) + adopt in `diagnose_nofill.py`

**Files:** Create `alphalens_research/diagnostics/edge_stores.py`; Modify `scripts/diagnose_nofill.py`.

- [ ] **Step 1: Create `edge_stores.py`**

```python
"""Shared read-only loaders for the EDGE parquet stores (research diagnostics).

All read from ~/.alphalens parquet stores; the research side may import
alphalens_pipeline. Pulled out of diagnose_nofill.py so the selection diagnostic
reuses the same loaders (DRY).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
from alphalens_pipeline.data import rs_history
from alphalens_pipeline.paper import brief_loader

HOME = Path.home() / ".alphalens"


def load_store(store_dir: Path) -> pd.DataFrame:
    """Concat every ``YYYY-MM-DD.parquet`` in ``store_dir``, stamping brief_date from the stem."""
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


def setup_index(briefs_dir: Path) -> dict[tuple[dt.date, str], dict]:
    """Map (brief_date, TICKER) -> decoded brief_trade_setup dict."""
    out: dict[tuple[dt.date, str], dict] = {}
    briefs = load_store(briefs_dir)
    if briefs.empty:
        return out
    for _, row in briefs.iterrows():
        ticker = str(row.get("ticker", "")).upper()
        if not ticker:
            continue
        setup = brief_loader._coerce_trade_setup(row.get("brief_trade_setup"))
        if setup is not None:
            out[(row["brief_date"], ticker)] = setup
    return out


class GroupedDailyCache:
    """Memoized ``rs_history.read_grouped_day`` so each session parquet is read once."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._cache: dict[dt.date, dict | None] = {}

    def get(self, session: dt.date) -> dict | None:
        if session not in self._cache:
            self._cache[session] = rs_history.read_grouped_day(self._root, session)
        return self._cache[session]


def newest_session(root: Path) -> dt.date | None:
    """The newest ISO-stem ``*.parquet`` session date in the grouped-daily store."""
    best: dt.date | None = None
    if not root.is_dir():
        return None
    for p in root.glob("*.parquet"):
        try:
            d = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if best is None or d > best:
            best = d
    return best
```

- [ ] **Step 2: Refactor `diagnose_nofill.py` to use `edge_stores`**

Replace its local `_load_store`, `_setup_index`, and the inline `grouped_cache`/`grouped()` closure with imports from `edge_stores`. Concretely:
- Add `from alphalens_research.diagnostics import edge_stores, nofill` (keep `nofill`).
- Delete the local `_load_store` and `_setup_index` function definitions.
- Replace calls: `_load_store(args.ladders_dir)` → `edge_stores.load_store(args.ladders_dir)`; `_setup_index(args.briefs_dir)` → `edge_stores.setup_index(args.briefs_dir)`.
- Replace the `grouped_cache = {}` + `def grouped(session)` block with `grouped = edge_stores.GroupedDailyCache(args.grouped_root)` and change call sites `grouped(s)` → `grouped.get(s)`.
- Keep `_HOME` or switch to `edge_stores.HOME` for the argparse defaults (either is fine; prefer `edge_stores.HOME` and drop the local `_HOME`).

- [ ] **Step 3: Verify no behaviour change**

Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_nofill_diagnostics -v` → 12 pass.
Run: `cd apps/alphalens-research && uv run python scripts/diagnose_nofill.py --help` → argparse help, no ImportError.
Run: `cd <worktree-root> && uv run ruff check apps/alphalens-research/alphalens_research/diagnostics/edge_stores.py apps/alphalens-research/scripts/diagnose_nofill.py` → clean.
Run: `cd <worktree-root> && uv run pyright apps/alphalens-research/alphalens_research/diagnostics/edge_stores.py apps/alphalens-research/scripts/diagnose_nofill.py` → 0 errors.

---

### Task 2: `fixed_horizon.py` (pure CAR + bootstrap)

**Files:** Create `alphalens_research/diagnostics/fixed_horizon.py`; Test `tests/test_fixed_horizon.py`.

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for fixed-horizon CAR + bootstrap (pure, no I/O)."""

from __future__ import annotations

import unittest

from alphalens_research.diagnostics import fixed_horizon as fh


class TestCarForEvent(unittest.TestCase):
    def test_market_adjusted_bhar(self):
        # stock +10%, SPY +4% -> CAR +6%.
        car = fh.car_for_event(stock_anchor=100.0, stock_horizon=110.0,
                               spy_anchor=100.0, spy_horizon=104.0)
        self.assertAlmostEqual(car, 0.06)

    def test_none_on_missing_or_nonpositive(self):
        self.assertIsNone(fh.car_for_event(stock_anchor=None, stock_horizon=110.0,
                                           spy_anchor=100.0, spy_horizon=104.0))
        self.assertIsNone(fh.car_for_event(stock_anchor=0.0, stock_horizon=110.0,
                                           spy_anchor=100.0, spy_horizon=104.0))


class TestBootstrapCi(unittest.TestCase):
    def test_deterministic_and_brackets_mean(self):
        vals = [0.01, -0.02, 0.05, 0.03, -0.01, 0.04]
        lo, mean, hi = fh.bootstrap_ci(vals, n_resamples=2000, ci=0.90, seed=42)
        self.assertAlmostEqual(mean, sum(vals) / len(vals))
        self.assertLessEqual(lo, mean)
        self.assertLessEqual(mean, hi)
        # reproducible
        lo2, _, hi2 = fh.bootstrap_ci(vals, n_resamples=2000, ci=0.90, seed=42)
        self.assertEqual((lo, hi), (lo2, hi2))

    def test_empty_and_singleton(self):
        self.assertEqual(fh.bootstrap_ci([], n_resamples=100, seed=1), (None, None, None))
        self.assertEqual(fh.bootstrap_ci([0.07], n_resamples=100, seed=1), (0.07, 0.07, 0.07))

    def test_filters_none(self):
        lo, mean, hi = fh.bootstrap_ci([0.02, None, 0.04], n_resamples=500, seed=3)
        self.assertAlmostEqual(mean, 0.03)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → fail** (`ModuleNotFoundError: ...fixed_horizon`).
Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_fixed_horizon -v`

- [ ] **Step 3: Implement `fixed_horizon.py`**

```python
"""Fixed-horizon market-adjusted CAR + percentile bootstrap (pure, no I/O).

Selection-quality metric: per-event buy-and-hold abnormal return over a fixed
k-session window from the event, market-adjusted (beta=1) against SPY. See
docs/superpowers/specs/2026-06-16-fixed-horizon-car-survival-fill-design.md.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

K_WINDOWS: tuple[int, ...] = (5, 10, 20)
LOW_N_WARN = 30  # below this, the CI is wide / estimate anecdotal (warning only, not a gate)


def car_for_event(
    *,
    stock_anchor: float | None,
    stock_horizon: float | None,
    spy_anchor: float | None,
    spy_horizon: float | None,
) -> float | None:
    """Market-adjusted BHAR = (stock buy-hold) - (SPY buy-hold) over the window.

    ``None`` when any of the four closes is missing or non-positive.
    """
    for v in (stock_anchor, stock_horizon, spy_anchor, spy_horizon):
        if v is None or v <= 0.0:
            return None
    stock_bhar = stock_horizon / stock_anchor - 1.0
    spy_bhar = spy_horizon / spy_anchor - 1.0
    return stock_bhar - spy_bhar


def bootstrap_ci(
    values: Sequence[float | None],
    *,
    n_resamples: int = 10_000,
    ci: float = 0.90,
    seed: int = 0,
) -> tuple[float | None, float | None, float | None]:
    """Percentile bootstrap ``(lo, mean, hi)`` of the mean. Deterministic given ``seed``.

    ``None`` values are dropped. Returns ``(None, None, None)`` for an empty input and
    ``(x, x, x)`` for a singleton.
    """
    vals = [float(v) for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return (None, None, None)
    mean = sum(vals) / n
    if n == 1:
        return (vals[0], vals[0], vals[0])
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_resamples):
        means.append(sum(vals[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo_i = int((1.0 - ci) / 2.0 * n_resamples)
    hi_i = int((1.0 + ci) / 2.0 * n_resamples) - 1
    return (means[lo_i], mean, means[hi_i])
```

- [ ] **Step 4: Run → pass.**
Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_fixed_horizon -v`

---

### Task 3: `fill_survival.py` (pure Kaplan-Meier + fill-rate)

**Files:** Create `alphalens_research/diagnostics/fill_survival.py`; Test `tests/test_fill_survival.py`.

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the Kaplan-Meier time-to-fill + fill-rate (pure, no I/O)."""

from __future__ import annotations

import unittest

from alphalens_research.diagnostics import fill_survival as fs


class TestKaplanMeier(unittest.TestCase):
    def test_product_limit_with_censoring(self):
        # durations 2,3,3 filled; 7,7 censored. S drops at 2 (->0.8) and 3 (->0.4),
        # flat through the censored 7s (->0.4).
        curve = fs.kaplan_meier([2, 3, 3, 7, 7], [1, 1, 1, 0, 0])
        self.assertEqual([t for t, _ in curve], [2, 3, 7])
        self.assertAlmostEqual(dict(curve)[2], 0.8)
        self.assertAlmostEqual(dict(curve)[3], 0.4)
        self.assertAlmostEqual(dict(curve)[7], 0.4)

    def test_empty(self):
        self.assertEqual(fs.kaplan_meier([], []), [])


class TestFillRateCi(unittest.TestCase):
    def test_rate_and_deterministic_ci(self):
        lo, rate, hi = fs.fill_rate_ci(3, 5, n_resamples=2000, ci=0.90, seed=7)
        self.assertAlmostEqual(rate, 0.6)
        self.assertLessEqual(lo, rate)
        self.assertLessEqual(rate, hi)
        lo2, _, hi2 = fs.fill_rate_ci(3, 5, n_resamples=2000, ci=0.90, seed=7)
        self.assertEqual((lo, hi), (lo2, hi2))

    def test_zero_total(self):
        self.assertEqual(fs.fill_rate_ci(0, 0, n_resamples=10, seed=1), (None, None, None))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → fail.**
Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_fill_survival -v`

- [ ] **Step 3: Implement `fill_survival.py`**

```python
"""Kaplan-Meier time-to-fill + fill-rate (pure, no I/O).

Entry-quality metric: model time (in sessions) until the dip-buy ladder's E1 is
first touched within the entry-TTL window; orders never touched within the window
are right-censored at TTL. See the design spec.
"""

from __future__ import annotations

import random
from collections.abc import Sequence


def kaplan_meier(
    durations: Sequence[int], events: Sequence[int]
) -> list[tuple[int, float]]:
    """Product-limit survival estimate S(t) = P(not yet filled by session t).

    ``durations[i]`` = session index of fill (event=1) or the censoring time (event=0).
    Returns ``[(t, S_t), ...]`` over the distinct event/censor times, ascending.
    """
    pairs = sorted(zip(durations, events, strict=True))
    n = len(pairs)
    if n == 0:
        return []
    at_risk = n
    surv = 1.0
    out: list[tuple[int, float]] = []
    for t in sorted({d for d, _ in pairs}):
        fills = sum(1 for d, e in pairs if d == t and e == 1)
        leaving = sum(1 for d, _ in pairs if d == t)
        if at_risk > 0 and fills > 0:
            surv *= 1.0 - fills / at_risk
        out.append((t, surv))
        at_risk -= leaving
    return out


def fill_rate_ci(
    n_touched: int,
    n_total: int,
    *,
    n_resamples: int = 10_000,
    ci: float = 0.90,
    seed: int = 0,
) -> tuple[float | None, float | None, float | None]:
    """Fraction filled within the window + percentile-bootstrap CI. Deterministic given ``seed``."""
    if n_total <= 0:
        return (None, None, None)
    rate = n_touched / n_total
    data = [1] * n_touched + [0] * (n_total - n_touched)
    rng = random.Random(seed)
    rates: list[float] = []
    for _ in range(n_resamples):
        rates.append(sum(data[rng.randrange(n_total)] for _ in range(n_total)) / n_total)
    rates.sort()
    lo_i = int((1.0 - ci) / 2.0 * n_resamples)
    hi_i = int((1.0 + ci) / 2.0 * n_resamples) - 1
    return (rates[lo_i], rate, rates[hi_i])
```

- [ ] **Step 4: Run → pass.**
Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_fill_survival -v`

---

### Task 4: Driver `diagnose_selection.py`

**Files:** Create `apps/alphalens-research/scripts/diagnose_selection.py`. (No new unit test — logic is the tested pure modules; verify via `--help`, ruff, pyright, and the full suite.)

- [ ] **Step 1: Write the driver**

```python
#!/usr/bin/env python
"""Fixed-horizon CAR (selection) + Kaplan-Meier survival-fill (entry) diagnostic.

Read-only, research-side. Reads the same three ~/.alphalens parquet stores as
diagnose_nofill.py. Selection = daily market-adjusted BHAR over fixed k-session
windows from the event (complete-window-only) with bootstrap CIs; entry =
time-to-touch-E1 survival with right-censoring at the entry TTL. Telemetry-only.

    .venv/bin/python apps/alphalens-research/scripts/diagnose_selection.py
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    previous_trading_day,
    session_on_or_after,
)
from alphalens_pipeline.paper.constants import DEFAULT_ORDER_TTL_DAYS

from alphalens_research.diagnostics import edge_stores, fill_survival, fixed_horizon

_SPY = "SPY"
_TOUCH_EPS = 0.0025
_FILLED = {"OPEN", "PARTIAL_TP_OPEN", "TP_FULL", "SL_HIT"}


def _close(snapshot: dict | None, ticker: str) -> float | None:
    if not snapshot:
        return None
    bar = snapshot.get(ticker.upper())
    if not bar:
        return None
    try:
        c = float(bar["c"])
    except (KeyError, TypeError, ValueError):
        return None
    return c if c > 0.0 else None


def _low(snapshot: dict | None, ticker: str) -> float | None:
    if not snapshot:
        return None
    bar = snapshot.get(ticker.upper())
    if not bar:
        return None
    try:
        return float(bar["l"])
    except (KeyError, TypeError, ValueError):
        return None


def _e1(setup: dict | None) -> float | None:
    if not setup or setup.get("status") != "OK":
        return None
    tiers = setup.get("entry_tiers") or []
    if not tiers:
        return None
    try:
        return float(tiers[0]["limit"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ladders-dir", type=Path, default=edge_stores.HOME / "population_ladders")
    ap.add_argument("--briefs-dir", type=Path, default=edge_stores.HOME / "thematic_briefs")
    from alphalens_pipeline.data import rs_history

    ap.add_argument("--grouped-root", type=Path, default=rs_history.DEFAULT_RS_HISTORY_ROOT)
    ap.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    ap.add_argument("--ttl", type=int, default=DEFAULT_ORDER_TTL_DAYS)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=edge_stores.HOME / "diagnostics" / "selection.parquet")
    args = ap.parse_args()

    outcomes = edge_stores.load_store(args.ladders_dir)
    if outcomes.empty or "plannable" not in outcomes.columns:
        print("no plannable population-ladder outcomes at", args.ladders_dir)
        return
    setups = edge_stores.setup_index(args.briefs_dir)
    grouped = edge_stores.GroupedDailyCache(args.grouped_root)
    newest = edge_stores.newest_session(args.grouped_root)
    if newest is None:
        print("empty grouped-daily store at", args.grouped_root)
        return

    plannable = outcomes[outcomes["plannable"] == True].copy()  # noqa: E712

    # Per-event CAR at each k (complete-window-only) + fill duration/censoring.
    records: list[dict] = []
    for _, row in plannable.iterrows():
        brief_date = row["brief_date"]
        ticker = str(row["ticker"]).upper()
        classification = str(row.get("ladder_classification") or "")
        arrival = session_on_or_after(brief_date, args.exchange)
        anchor = previous_trading_day(arrival, args.exchange)
        a_stock = _close(grouped.get(anchor), ticker)
        a_spy = _close(grouped.get(anchor), _SPY)

        rec: dict = {"brief_date": brief_date, "ticker": ticker, "classification": classification}
        for k in fixed_horizon.K_WINDOWS:
            horizon = advance_trading_sessions(arrival, k - 1, args.exchange)
            if horizon > newest:
                rec[f"car_{k}"] = None  # window not elapsed
                continue
            rec[f"car_{k}"] = fixed_horizon.car_for_event(
                stock_anchor=a_stock,
                stock_horizon=_close(grouped.get(horizon), ticker),
                spy_anchor=a_spy,
                spy_horizon=_close(grouped.get(horizon), _SPY),
            )

        # Survival: first session in [arrival, arrival+ttl) whose low touches E1.
        e1 = _e1(setups.get((brief_date, ticker)))
        duration: int | None = None
        event = 0
        if e1 is not None:
            incomplete = False
            for i in range(args.ttl):
                s = advance_trading_sessions(arrival, i, args.exchange)
                if s > newest:
                    incomplete = True
                    break
                low = _low(grouped.get(s), ticker)
                if low is None:
                    incomplete = True
                    break
                if low <= e1 * (1.0 + _TOUCH_EPS):
                    duration, event = i + 1, 1
                    break
            if duration is None and not incomplete:
                duration, event = args.ttl, 0  # right-censored at TTL
        rec["fill_duration"] = duration
        rec["fill_event"] = event
        records.append(rec)

    import pandas as pd

    table = pd.DataFrame.from_records(records)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(args.out, index=False)
    print(f"plannable: {len(plannable)}; wrote {args.out} rows: {len(table)}")

    # ---- Selection: per-k CAR with bootstrap CI (all / filled / unfilled) ----
    print("\nfixed-horizon CAR (market-adjusted BHAR vs SPY), bootstrap 90% CI:")
    for k in fixed_horizon.K_WINDOWS:
        col = table[f"car_{k}"] if f"car_{k}" in table else None
        if col is None:
            continue
        complete = table[col.notna()]
        groups = {
            "all": complete,
            "filled": complete[complete["classification"].isin(_FILLED)],
            "unfilled": complete[complete["classification"] == "NO_FILL"],
        }
        print(f"  k={k}:")
        for name, sub in groups.items():
            lo, mean, hi = fixed_horizon.bootstrap_ci(
                sub[f"car_{k}"].tolist(), seed=args.seed
            )
            warn = "  [low-N]" if len(sub) < fixed_horizon.LOW_N_WARN else ""
            ms = f"{mean:+.4f}" if mean is not None else "n/a"
            cis = f"[{lo:+.4f}, {hi:+.4f}]" if lo is not None else ""
            print(f"    {name:9} n={len(sub):3} mean={ms} {cis}{warn}")

    # ---- Entry: fill-rate + Kaplan-Meier survival ----
    fillable = table[table["fill_duration"].notna()]
    n_total = len(fillable)
    n_touched = int((fillable["fill_event"] == 1).sum())
    lo, rate, hi = fill_survival.fill_rate_ci(n_touched, n_total, seed=args.seed)
    if rate is not None:
        warn = "  [low-N]" if n_total < fixed_horizon.LOW_N_WARN else ""
        print(
            f"\nfill-rate (touch E1 within TTL={args.ttl}): {n_touched}/{n_total} "
            f"= {rate:.3f}  90% CI [{lo:.3f}, {hi:.3f}]{warn}"
        )
        durations = [int(d) for d in fillable["fill_duration"].tolist()]
        events = [int(e) for e in fillable["fill_event"].tolist()]
        print("Kaplan-Meier S(t) = P(not yet filled by session t):")
        for t, s in fill_survival.kaplan_meier(durations, events):
            print(f"  t={t:2}  S={s:.3f}")
    else:
        print("\nno fillable rows with a complete entry window yet")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify imports + help.**
Run: `cd apps/alphalens-research && uv run python scripts/diagnose_selection.py --help` → argparse help, no ImportError.

- [ ] **Step 3: Lint + type.**
Run: `cd <worktree-root> && uv run ruff check apps/alphalens-research/scripts/diagnose_selection.py apps/alphalens-research/alphalens_research/diagnostics/fixed_horizon.py apps/alphalens-research/alphalens_research/diagnostics/fill_survival.py` → fix any findings (e.g. move the two mid-function imports to the top if ruff prefers; `pd`/`rs_history` may be flagged — hoist them).
Run: `cd <worktree-root> && uv run pyright <same files>` → 0 errors.

- [ ] **Step 4: Full diagnostics suite green.**
Run: `cd apps/alphalens-research && uv run python -m unittest tests.test_fixed_horizon tests.test_fill_survival tests.test_nofill_diagnostics -v` → all pass.

---

### Task 5: Branch-wide gates

- [ ] **Step 1: Full research test discover** (catches dependency/no-polish/layer-status rules):
Run: `cd <worktree-root> && uv run python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research 2>&1 | tail -15` → OK.
- [ ] **Step 2: ruff + pyright on all new/changed files** → clean / 0 errors.

(The controller commits, pushes, opens the PR, runs the zen pre-merge review, waits for CI + Sonar, and merges — not part of the agent tasks.)

---

## Self-review (completed during planning)

- **Spec coverage:** §3 stores → Task 1 `edge_stores`; §4 CAR → Task 2 `car_for_event` + Task 4 windowing/complete-gate/anchor (`previous_trading_day`)/all-filled-unfilled split; §5 survival → Task 3 `kaplan_meier`/`fill_rate_ci` + Task 4 touch-walk/censoring; §6 inference → seeded `bootstrap_ci`, CIs-not-t-tests, all-k, `LOW_N_WARN` warning; §7 deliverables → Tasks 1-4 files; §8 tests → Tasks 2-3 (BHAR, deterministic bootstrap, KM-with-censoring, fill-rate, empties).
- **Placeholders:** none — all code complete; commands have expected output.
- **Type/name consistency:** `car_for_event`/`bootstrap_ci`/`kaplan_meier`/`fill_rate_ci` signatures + `K_WINDOWS`/`LOW_N_WARN` match between modules, driver, and tests; `edge_stores.{load_store,setup_index,GroupedDailyCache,newest_session,HOME}` used consistently; `_FILLED` set matches spec §4.

## Notes

- All agents: work in the worktree, do NOT `git commit/push` (controller handles git).
- `entry_tiers[0]["limit"]` is E1 (verified against `ladder_replay.parse_ladder`).
- `read_grouped_day` keys are Polygon grouped-daily `o/h/l/c/v`, symbol upper-cased; `SPY` is assumed present (per-event drop if absent).
- Harness LSP may false-positive `"... is unknown import symbol"` for files in a worktree; the authoritative check is `uv run pyright` from the worktree root (0 errors).
