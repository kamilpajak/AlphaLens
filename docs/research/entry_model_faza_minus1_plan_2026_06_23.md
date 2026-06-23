# Entry-model Faza −1 (anchor-corrected HALT gate) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-run the selection diagnostic with the CAR stock anchor moved from the prior-session close to the arrival 30-min VWAP (the price an arrival entry actually pays), so we can decide whether the motivating "unfilled outperforms" edge survives subtracting the overnight gap — the HALT gate for the whole entry-model program.

**Architecture:** Add a tiny pure anchor-selection helper to the research diagnostics package (testable), then wire a `--anchor` option into `diagnose_selection.py` that uses the already-stamped `reference_close` (= arrival 30-min VWAP) as the stock anchor and SPY's arrival open as the market leg. Run both anchor modes and compare the filled/unfilled CAR.

**Tech Stack:** Python, pandas, unittest; reuses `alphalens_research.diagnostics.{fixed_horizon,edge_stores}` and the `reference_close` column already on the `population_ladders` parquet.

## Global Constraints

- TDD always — red→green→refactor, behavior change is tested in the package (scripts are coverage-excluded, so the new logic lives in the package).
- English-only in code (comments/docstrings/identifiers); math notation OK.
- No new data source, no network — reads only existing `~/.alphalens` parquet stores.
- Dependency direction: research may import `alphalens_pipeline`; do NOT add pipeline→research imports.
- Default behavior unchanged: `--anchor prior_close` reproduces the current output byte-for-byte.
- This is the existing diagnostic's k-windows (k=5/k=10/k=20); do NOT change K_WINDOWS here (the k=10 reward lock is a Faza-0 concern).

---

### Task 1: Pure anchor-selection helper (package, tested)

**Files:**
- Create: `apps/alphalens-research/alphalens_research/diagnostics/anchor.py`
- Test: `apps/alphalens-research/tests/test_diagnostics_anchor.py`

**Interfaces:**
- Produces:
  - `ANCHOR_PRIOR_CLOSE = "prior_close"`, `ANCHOR_ARRIVAL_VWAP = "arrival_vwap"`, `ANCHOR_MODES = (ANCHOR_PRIOR_CLOSE, ANCHOR_ARRIVAL_VWAP)`
  - `event_anchor(mode: str, *, prior_close_stock: float | None, prior_close_spy: float | None, arrival_vwap_stock: float | None, arrival_open_spy: float | None) -> tuple[float | None, float | None]` returning `(stock_anchor, spy_anchor)`.

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-research/tests/test_diagnostics_anchor.py
import unittest

from alphalens_research.diagnostics.anchor import (
    ANCHOR_ARRIVAL_VWAP,
    ANCHOR_PRIOR_CLOSE,
    event_anchor,
)


class TestEventAnchor(unittest.TestCase):
    def test_prior_close_mode_returns_prior_pair(self):
        stock, spy = event_anchor(
            ANCHOR_PRIOR_CLOSE,
            prior_close_stock=100.0,
            prior_close_spy=500.0,
            arrival_vwap_stock=110.0,
            arrival_open_spy=505.0,
        )
        self.assertEqual((stock, spy), (100.0, 500.0))

    def test_arrival_vwap_mode_uses_vwap_stock_and_spy_open(self):
        stock, spy = event_anchor(
            ANCHOR_ARRIVAL_VWAP,
            prior_close_stock=100.0,
            prior_close_spy=500.0,
            arrival_vwap_stock=110.0,
            arrival_open_spy=505.0,
        )
        self.assertEqual((stock, spy), (110.0, 505.0))

    def test_arrival_vwap_mode_propagates_missing_as_none(self):
        stock, spy = event_anchor(
            ANCHOR_ARRIVAL_VWAP,
            prior_close_stock=100.0,
            prior_close_spy=500.0,
            arrival_vwap_stock=None,
            arrival_open_spy=505.0,
        )
        self.assertEqual((stock, spy), (None, 505.0))

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            event_anchor(
                "nope",
                prior_close_stock=1.0,
                prior_close_spy=1.0,
                arrival_vwap_stock=1.0,
                arrival_open_spy=1.0,
            )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -p test_diagnostics_anchor.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'alphalens_research.diagnostics.anchor'`

- [ ] **Step 3: Write minimal implementation**

```python
# apps/alphalens-research/alphalens_research/diagnostics/anchor.py
"""CAR anchor selection for the selection diagnostic (Faza -1 HALT gate).

The legacy anchor is the prior-session CLOSE for both the stock and the SPY
leg. The arrival-VWAP anchor measures the return an ARRIVAL entry actually
earns: the stock leg starts at its arrival 30-min VWAP (the stamped
``reference_close``) and the SPY leg at its arrival OPEN (the same-window
market leg). This subtracts the stock's overnight gap, which the
prior-close anchor silently hands to "unfilled" names that gapped up.
"""

from __future__ import annotations

ANCHOR_PRIOR_CLOSE = "prior_close"
ANCHOR_ARRIVAL_VWAP = "arrival_vwap"
ANCHOR_MODES = (ANCHOR_PRIOR_CLOSE, ANCHOR_ARRIVAL_VWAP)


def event_anchor(
    mode: str,
    *,
    prior_close_stock: float | None,
    prior_close_spy: float | None,
    arrival_vwap_stock: float | None,
    arrival_open_spy: float | None,
) -> tuple[float | None, float | None]:
    """Return ``(stock_anchor, spy_anchor)`` for one event under ``mode``.

    ``None`` legs are propagated unchanged; the caller's CAR routine already
    treats a ``None``/non-positive anchor as an incomputable window.
    """
    if mode == ANCHOR_PRIOR_CLOSE:
        return prior_close_stock, prior_close_spy
    if mode == ANCHOR_ARRIVAL_VWAP:
        return arrival_vwap_stock, arrival_open_spy
    raise ValueError(f"unknown anchor mode: {mode!r} (expected one of {ANCHOR_MODES})")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research -p test_diagnostics_anchor.py`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/alphalens_research/diagnostics/anchor.py apps/alphalens-research/tests/test_diagnostics_anchor.py
git commit -m "feat(diagnostics): CAR anchor-selection helper (prior-close vs arrival-VWAP)"
```

---

### Task 2: Wire `--anchor` into diagnose_selection.py

**Files:**
- Modify: `apps/alphalens-research/scripts/diagnose_selection.py` (imports near line 20; `_close` helper region near line 31; arg parsing near line 76; the per-event loop lines 97-117; the print header line 149)

**Interfaces:**
- Consumes: `anchor.event_anchor`, `anchor.ANCHOR_MODES`, `anchor.ANCHOR_PRIOR_CLOSE` from Task 1; existing `fixed_horizon.car_for_event`; the `reference_close` column on each plannable row.
- Produces: a `--anchor {prior_close,arrival_vwap}` CLI option (default `prior_close`) that changes only the per-event CAR anchor.

- [ ] **Step 1: Add the import**

Add to the import block (next to `from alphalens_research.diagnostics import ...` near line 27):

```python
from alphalens_research.diagnostics import anchor as anchor_mod
```

- [ ] **Step 2: Add an `_open` helper**

Immediately after the existing `_low` function (near line 44), add:

```python
def _open(snapshot: dict | None, ticker: str) -> float | None:
    if not snapshot:
        return None
    bar = snapshot.get(ticker.upper())
    if not bar:
        return None
    try:
        o = float(bar["o"])
    except (KeyError, TypeError, ValueError):
        return None
    return o if o > 0.0 else None
```

- [ ] **Step 3: Add the `--anchor` argument**

After the `--seed` argument (near line 76) add:

```python
    ap.add_argument(
        "--anchor",
        choices=anchor_mod.ANCHOR_MODES,
        default=anchor_mod.ANCHOR_PRIOR_CLOSE,
        help="CAR anchor: prior_close (legacy) or arrival_vwap (price an arrival entry pays)",
    )
```

- [ ] **Step 4: Use the selected anchor in the per-event loop**

Replace the anchor computation + the `car_for_event` call inside the `for k in fixed_horizon.K_WINDOWS:` loop (lines 102-117) with:

```python
        anchor_session = previous_trading_day(arrival, args.exchange)
        prior_close_stock = _close(grouped.get(anchor_session), ticker)
        prior_close_spy = _close(grouped.get(anchor_session), _SPY)
        arrival_vwap_stock = row.get("reference_close")
        arrival_vwap_stock = (
            float(arrival_vwap_stock) if arrival_vwap_stock is not None else None
        )
        arrival_open_spy = _open(grouped.get(arrival), _SPY)
        a_stock, a_spy = anchor_mod.event_anchor(
            args.anchor,
            prior_close_stock=prior_close_stock,
            prior_close_spy=prior_close_spy,
            arrival_vwap_stock=arrival_vwap_stock,
            arrival_open_spy=arrival_open_spy,
        )

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
```

Note: delete the now-superseded lines `anchor = previous_trading_day(...)`, `a_stock = _close(...)`, `a_spy = _close(...)` and the original `rec: dict = {...}` line above the loop — they are replaced by the block above.

- [ ] **Step 5: Surface the anchor mode in the print header**

Change the selection header (line 149) from:

```python
    print("\nfixed-horizon CAR (market-adjusted BHAR vs SPY), bootstrap 90% CI:")
```

to:

```python
    print(
        f"\nfixed-horizon CAR (market-adjusted BHAR vs SPY, anchor={args.anchor}), "
        "bootstrap 90% CI:"
    )
```

- [ ] **Step 6: Smoke-run both modes locally (no assertion — script needs the VPS stores)**

Run: `uv run python apps/alphalens-research/scripts/diagnose_selection.py --help`
Expected: help text lists `--anchor {prior_close,arrival_vwap}`. (Full data run happens on the VPS in the HALT-gate section below; local stores are empty.)

- [ ] **Step 7: Run the full research suite to confirm no regression**

Run: `uv run python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research`
Expected: OK (all pass, including the new anchor tests)

- [ ] **Step 8: Commit**

```bash
git add apps/alphalens-research/scripts/diagnose_selection.py
git commit -m "feat(diagnostics): --anchor option (arrival-VWAP CAR) for the entry-model HALT gate"
```

---

## HALT-gate execution (on the VPS, after merge — produces the GO/NO-GO)

Not a code task — the decision step. Run on `vault.kamilpajak.pl` (host venv; grouped store must be fresh — top it up via `systemctl --user start alphalens-grouped-daily-topup.service` first):

```bash
cd ~/AlphaLens
.venv/bin/python apps/alphalens-research/scripts/diagnose_selection.py --anchor prior_close
.venv/bin/python apps/alphalens-research/scripts/diagnose_selection.py --anchor arrival_vwap
```

**Decision rule (from the design memo §2):** compare the `unfilled` CAR mean at k=10 (and k=5) between the two anchors.
- If under `arrival_vwap` the unfilled edge **collapses toward ~0** (CI now straddles 0, or mean drops to a small fraction of the prior-close value) → **NO_GO**: the motivating prize was mostly the overnight gap. Write a short REJECTED note, do NOT launch Faza 0.
- If the unfilled edge **survives** with a materially positive lower CI bound → **GO**: proceed to write the Faza 0 plan (entry-grid substrate + offline comparison script).

---

## Self-review

- **Spec coverage:** Implements design memo §2 (Faza −1 HALT gate) exactly: arrival-VWAP anchor via the stamped `reference_close`, SPY arrival-open market leg, both-mode comparison, GO/NO-GO rule. Faza 0+ intentionally out of scope (gated).
- **Placeholder scan:** none — every step has exact paths and full code.
- **Type consistency:** `event_anchor` signature identical in Task 1 (definition + test) and Task 2 (call site); `_open` mirrors the existing `_close`/`_low` shape; `car_for_event` keyword args unchanged.
- **Default-unchanged invariant:** `--anchor` defaults to `prior_close`, and the prior-close branch returns exactly the legacy `(a_stock, a_spy)` pair → existing output preserved.
</content>
