# Options Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stamp 16 display-only `options_*` telemetry columns (IV30, term slope, VRP ratio, XZZ skew, raw put/call volume+OI, spread, chain quality, audit columns, config version) onto the thematic candidate parquet at the `score` stage, from a yfinance option-chain snapshot taken only inside the post-close window for the asof session.

**Architecture:** A new `alphalens_pipeline/thematic/options_telemetry/` package with pure feature functions (`features.py`) and a frame enricher (`enrichment.py`) that mirrors the Buffett/O'Neil `enrich(frame, *, asof)` pattern. Two small additions to existing infrastructure: `option_expiries()`/`option_chain()` methods on the canonical `YFinanceClient`, and `session_close_utc()` in the calendar helper. The CLI `thematic score` command wires the enricher last, passing the previous same-date output parquet for carry-forward (first successful stamp freezes). No Django changes: the parquet is the telemetry source of truth; Django's ingest drops unknown columns and no model field is added.

**Tech Stack:** Python 3.12, pandas, yfinance (via canonical client only), exchange_calendars (via `paper/calendar.py`), unittest.

**Spec:** `docs/research/options_telemetry_design_2026_07_07.md` (LOCKED, Perplexity-amended). Read it before starting.

## Global Constraints

- Work in worktree `/Users/jacoren/Developer/Personal/AlphaLens/.claude/worktrees/docs+options-telemetry-design`, branch `docs/options-telemetry-design`. Run `uv sync` there FIRST — the shared editable install otherwise imports the main checkout's copy of `alphalens_pipeline` (known gotcha).
- TDD always: red → green → refactor, even for 2-line changes.
- English-only in code (comments, docstrings, identifiers). Enforced by `test_no_polish_chars.py`.
- All yfinance access through `alphalens_pipeline/data/alt_data/yfinance_client.py` — `import yfinance` anywhere else trips `apps/alphalens-research/tests/test_no_raw_yfinance_http.py`.
- Conventional Commits (`type(scope): description`); never mention AI in commit messages.
- CLI command bodies use lazy imports (startup-time budget for the 15-min edgar cron).
- Display-only: `options_*` columns must NOT enter the brief sort (`_BRIEF_SORT_KEYS` untouched).
- Fail-soft: no options failure may abort the score stage.
- Constants from the spec, fixed here: `OPTIONS_CONFIG_VERSION = "options-telemetry-v1-yf-snapshot"`, IV sanity band [0.01, 5.0], ATM per-leg OI floor 50, ATM relative-spread cap 0.10, near-leg min DTE 7, term leg DTE target 180 within [120, 270], XZZ moneyness windows: OTM put K/S ∈ [0.80, 0.95] (closest to 0.95), ATM call K/S ∈ [0.95, 1.05] (closest to 1.00).
- Test commands run from `apps/alphalens-research`: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest <module> -v`.
- Full suite before the PR: `uv run python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research`.

## The 16 columns (single source of truth for names/dtypes)

| Column | dtype | Null when |
|---|---|---|
| `options_ivx30` | float64 | chain NONE, or no usable near leg |
| `options_term_slope` | float64 | no term leg in [120, 270] DTE |
| `options_vrp_ratio` | float64 | ivx30 null or realized vol null/zero |
| `options_skew_xzz` | float64 | either XZZ leg missing/insane |
| `options_put_vol` | float64 | chain NONE |
| `options_call_vol` | float64 | chain NONE |
| `options_put_oi` | float64 | chain NONE |
| `options_call_oi` | float64 | chain NONE |
| `options_spread_pct_atm` | float64 | ATM quotes unusable (bid<=0 or ask<bid) |
| `options_atm_strike` | float64 | no ATM strike present in both legs |
| `options_atm_mid` | float64 | ATM quotes unusable |
| `options_spot` | float64 | spot fetch failed |
| `options_chain_quality` | object (str) | null ONLY when no in-window stamp ever happened; a stamped row is always `"NONE"`/`"THIN"`/`"OK"` |
| `options_asof_expiry_near` | object (str, ISO date) | no near expiry |
| `options_snapshot_utc` | object (str, ISO datetime) | null when no in-window stamp ever happened |
| `options_config_version` | object (str) | never (constant) |

---

### Task 1: `session_close_utc()` in the calendar helper

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/paper/calendar.py` (add function right after `session_open_utc`, ~line 303)
- Test: `apps/alphalens-research/tests/paper/test_calendar.py` (append a class)

**Interfaces:**
- Produces: `session_close_utc(d: DateLike, exchange: str = DEFAULT_EXCHANGE) -> dt.datetime` — UTC close of the session on exact session date `d`; raises `ValueError` on non-sessions (mirrors `session_open_utc`). Used by Task 5's window rule.

- [ ] **Step 1: Write the failing test** — append to `apps/alphalens-research/tests/paper/test_calendar.py`:

```python
class TestSessionCloseUtc(unittest.TestCase):
    def test_xnys_summer_close_is_2000_utc(self):
        # 2026-07-06 is a regular Monday session; EDT close 16:00 ET = 20:00 UTC.
        close = calendar.session_close_utc(dt.date(2026, 7, 6))
        self.assertEqual(close, dt.datetime(2026, 7, 6, 20, 0, tzinfo=dt.UTC))

    def test_xnys_winter_close_is_2100_utc(self):
        # 2026-01-05 is a regular Monday session; EST close 16:00 ET = 21:00 UTC.
        close = calendar.session_close_utc(dt.date(2026, 1, 5))
        self.assertEqual(close, dt.datetime(2026, 1, 5, 21, 0, tzinfo=dt.UTC))

    def test_non_session_raises_value_error(self):
        with self.assertRaises(ValueError):
            calendar.session_close_utc(dt.date(2026, 7, 4))  # Saturday

    def test_half_day_close_is_early(self):
        # 2025-11-28 (Friday after Thanksgiving) closes 13:00 ET = 18:00 UTC (EST).
        close = calendar.session_close_utc(dt.date(2025, 11, 28))
        self.assertEqual(close, dt.datetime(2025, 11, 28, 18, 0, tzinfo=dt.UTC))
```

Match the existing file's import style (it already imports the calendar module and `datetime` — reuse its aliases; if it imports as `from alphalens_pipeline.paper import calendar`, keep that).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.paper.test_calendar.TestSessionCloseUtc -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'session_close_utc'`

- [ ] **Step 3: Write minimal implementation** — add to `calendar.py` directly below `session_open_utc`:

```python
def session_close_utc(
    d: DateLike,
    exchange: str = DEFAULT_EXCHANGE,
) -> dt.datetime:
    """UTC datetime of ``exchange``'s closing auction on session date ``d``.

    Mirror of :func:`session_open_utc` — requires ``d`` to be an EXACT
    session (raises ``ValueError`` otherwise). Half-days resolve to the
    early close automatically because ``exchange_calendars`` stores the
    actual per-session close. For XNYS in summer (EDT) the close is
    20:00 UTC (16:00 ET); in winter (EST) 21:00 UTC.
    """
    ts = _to_session_timestamp(d)
    cal = _calendar(exchange)
    if not cal.is_session(ts):
        raise ValueError(f"{d!r} is not a session on {exchange}; resolve it first")
    return cal.session_close(ts).to_pydatetime().astimezone(dt.UTC)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.paper.test_calendar -v`
Expected: PASS (all classes in the file, not just the new one)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/paper/calendar.py apps/alphalens-research/tests/paper/test_calendar.py
git commit -m "feat(calendar): add session_close_utc mirroring session_open_utc"
```

---

### Task 2: option-chain methods on the canonical yfinance client

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/yfinance_client.py` (add two methods after `next_earnings`, ~line 219)
- Test: `apps/alphalens-research/tests/thematic/test_yfinance_client.py` (append classes)

**Interfaces:**
- Consumes: existing `self._call_with_retry(fetch_fn, what=..., default=...)` internal (same as `splits()`).
- Produces:
  - `option_expiries(self, ticker: str) -> list[dt.date] | None` — sorted listed expiries; `None` on permanent failure; `[]` when the ticker has no listed options.
  - `option_chain(self, ticker: str, expiry: dt.date) -> tuple[pd.DataFrame, pd.DataFrame] | None` — `(calls, puts)` DataFrames with at least columns `[strike, bid, ask, impliedVolatility, openInterest, volume]`; `None` on failure.

- [ ] **Step 1: Write the failing test** — append to `tests/thematic/test_yfinance_client.py` (follow the file's existing `patch("yfinance.Ticker", return_value=fake)` idiom and its client-construction helper; use `min_interval_s=0.0` like neighboring tests to avoid throttling sleeps):

```python
def _fake_chain_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    calls = pd.DataFrame(
        {
            "strike": [95.0, 100.0, 105.0],
            "bid": [6.0, 3.0, 1.2],
            "ask": [6.4, 3.2, 1.4],
            "impliedVolatility": [0.52, 0.50, 0.49],
            "openInterest": [120, 300, 80],
            "volume": [10, 40, 5],
        }
    )
    puts = calls.copy()
    return calls, puts


class TestOptionExpiries(unittest.TestCase):
    def test_returns_sorted_dates(self):
        fake = MagicMock()
        fake.options = ("2026-08-07", "2026-07-17")
        client = yc.YFinanceClient(min_interval_s=0.0)
        with patch("yfinance.Ticker", return_value=fake) as patched:
            out = client.option_expiries("qubt")
        patched.assert_called_once_with("QUBT")
        self.assertEqual(out, [dt.date(2026, 7, 17), dt.date(2026, 8, 7)])

    def test_no_listed_options_returns_empty_list(self):
        fake = MagicMock()
        fake.options = ()
        client = yc.YFinanceClient(min_interval_s=0.0)
        with patch("yfinance.Ticker", return_value=fake):
            self.assertEqual(client.option_expiries("QUBT"), [])

    def test_permanent_failure_returns_none(self):
        class _Raises:
            @property
            def options(self):
                raise RuntimeError("404 Not Found")

        client = yc.YFinanceClient(min_interval_s=0.0)
        with patch("yfinance.Ticker", return_value=_Raises()):
            self.assertIsNone(client.option_expiries("QUBT"))


class TestOptionChain(unittest.TestCase):
    def test_returns_calls_and_puts_frames(self):
        calls, puts = _fake_chain_frames()
        chain = MagicMock()
        chain.calls = calls
        chain.puts = puts
        fake = MagicMock()
        fake.option_chain.return_value = chain
        client = yc.YFinanceClient(min_interval_s=0.0)
        with patch("yfinance.Ticker", return_value=fake):
            out = client.option_chain("QUBT", dt.date(2026, 7, 17))
        fake.option_chain.assert_called_once_with("2026-07-17")
        self.assertIsNotNone(out)
        pd.testing.assert_frame_equal(out[0], calls)
        pd.testing.assert_frame_equal(out[1], puts)

    def test_failure_returns_none(self):
        fake = MagicMock()
        fake.option_chain.side_effect = RuntimeError("boom")
        client = yc.YFinanceClient(min_interval_s=0.0)
        with patch("yfinance.Ticker", return_value=fake):
            self.assertIsNone(client.option_chain("QUBT", dt.date(2026, 7, 17)))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.test_yfinance_client.TestOptionExpiries tests.thematic.test_yfinance_client.TestOptionChain -v`
Expected: FAIL with `AttributeError: 'YFinanceClient' object has no attribute 'option_expiries'`

- [ ] **Step 3: Write minimal implementation** — add to `YFinanceClient` after `next_earnings`:

```python
    def option_expiries(self, ticker: str) -> list[dt.date] | None:
        """Sorted listed option expiries for ``ticker``.

        Wraps ``yfinance.Ticker(T).options`` (a tuple of ISO date strings).
        Returns an EMPTY list when the ticker genuinely has no listed
        options, and ``None`` only on a permanent failure / exhausted
        retries — same tri-state contract as :meth:`splits`.
        """
        upper = ticker.upper()

        def _fetch() -> tuple | None:
            import yfinance as yf

            return yf.Ticker(upper).options

        raw = self._call_with_retry(_fetch, what=f"option_expiries({upper})", default=None)
        if raw is None:
            return None
        return sorted(dt.date.fromisoformat(str(e)) for e in raw)

    def option_chain(
        self, ticker: str, expiry: dt.date
    ) -> tuple[pd.DataFrame, pd.DataFrame] | None:
        """``(calls, puts)`` chain frames for one expiry, or ``None`` on failure.

        Wraps ``yfinance.Ticker(T).option_chain("YYYY-MM-DD")``. Frames are
        passed through verbatim (per-contract ``strike``, ``bid``, ``ask``,
        ``impliedVolatility``, ``openInterest``, ``volume``, ...) — the
        options-telemetry feature layer owns all filtering, because the
        vendor IV field has documented bugs the caller must sanity-screen.
        """
        upper = ticker.upper()

        def _fetch():
            import yfinance as yf

            return yf.Ticker(upper).option_chain(expiry.isoformat())

        chain = self._call_with_retry(_fetch, what=f"option_chain({upper})", default=None)
        if chain is None:
            return None
        return chain.calls, chain.puts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.test_yfinance_client -v`
Expected: PASS (whole module)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/data/alt_data/yfinance_client.py apps/alphalens-research/tests/thematic/test_yfinance_client.py
git commit -m "feat(data): option_expiries + option_chain on canonical yfinance client"
```

---

### Task 3: pure chain features — expiry selection, ATM, IV30, skew, spread, totals, quality

**Files:**
- Create: `apps/alphalens-pipeline/alphalens_pipeline/thematic/options_telemetry/__init__.py`
- Create: `apps/alphalens-pipeline/alphalens_pipeline/thematic/options_telemetry/features.py`
- Create: `apps/alphalens-research/tests/thematic/options_telemetry/__init__.py` (empty)
- Test: `apps/alphalens-research/tests/thematic/options_telemetry/test_features.py`

**Interfaces:**
- Produces (all pure, consumed by Task 5's enricher):
  - `OPTIONS_CONFIG_VERSION: str`, `CHAIN_QUALITY_{NONE,THIN,OK}: str` constants and the numeric thresholds from Global Constraints.
  - `select_bracketing_expiries(expiries: list[dt.date], asof: dt.date) -> tuple[dt.date | None, dt.date | None]` — `(below_or_at_30d_with_dte_ge_7, first_above_30d)`.
  - `select_term_expiry(expiries: list[dt.date], asof: dt.date) -> dt.date | None` — DTE closest to 180 within [120, 270].
  - `sane_iv(value: float | None) -> bool`
  - `atm_strike(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> float | None` — nearest-to-spot strike present in BOTH legs.
  - `expiry_atm_iv(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> float | None` — midpoint of sane call/put IV at the ATM strike; single sane leg used alone; both insane → None.
  - `interpolate_iv30(iv_near: float | None, dte_near: int | None, iv_far: float | None, dte_far: int | None) -> float | None` — linear in DTE at 30; one leg → flat value.
  - `skew_xzz(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> float | None`
  - `atm_quote(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> tuple[float, float, float] | None` — `(strike, mid, spread_pct)` from the ATM call, falling back to the ATM put when the call quote is unusable (`bid <= 0` or `ask < bid`).
  - `chain_totals(legs: list[tuple[pd.DataFrame, pd.DataFrame]]) -> dict[str, float]` — keys `put_vol, call_vol, put_oi, call_oi` summed over the bracketing expiries.
  - `classify_chain_quality(*, has_chain: bool, near: dt.date | None, far: dt.date | None, atm: float | None, atm_call_oi: float | None, atm_put_oi: float | None, atm_vol_total: float | None, spread_pct: float | None) -> str`

- [ ] **Step 1: Write the failing test** — `tests/thematic/options_telemetry/test_features.py`:

```python
"""Pure feature-function tests for the options telemetry (spec 2026-07-07 §4)."""

from __future__ import annotations

import datetime as dt
import math
import unittest

import pandas as pd

from alphalens_pipeline.thematic.options_telemetry import features as f

ASOF = dt.date(2026, 7, 6)


def _chain(strikes, ivs, bids=None, asks=None, oi=None, vol=None) -> pd.DataFrame:
    n = len(strikes)
    return pd.DataFrame(
        {
            "strike": strikes,
            "impliedVolatility": ivs,
            "bid": bids or [1.0] * n,
            "ask": asks or [1.1] * n,
            "openInterest": oi or [100] * n,
            "volume": vol or [10] * n,
        }
    )


class TestExpirySelection(unittest.TestCase):
    def test_brackets_30d(self):
        expiries = [ASOF + dt.timedelta(days=d) for d in (3, 17, 45, 170)]
        near, far = f.select_bracketing_expiries(expiries, ASOF)
        self.assertEqual(near, ASOF + dt.timedelta(days=17))
        self.assertEqual(far, ASOF + dt.timedelta(days=45))

    def test_near_leg_must_have_dte_ge_7(self):
        # Only a 3-DTE and a 45-DTE listed: gamma-week 3d leg is skipped.
        expiries = [ASOF + dt.timedelta(days=d) for d in (3, 45)]
        near, far = f.select_bracketing_expiries(expiries, ASOF)
        self.assertIsNone(near)
        self.assertEqual(far, ASOF + dt.timedelta(days=45))

    def test_no_chain_returns_none_pair(self):
        self.assertEqual(f.select_bracketing_expiries([], ASOF), (None, None))

    def test_term_leg_closest_to_180_within_band(self):
        expiries = [ASOF + dt.timedelta(days=d) for d in (17, 45, 130, 200, 400)]
        self.assertEqual(f.select_term_expiry(expiries, ASOF), ASOF + dt.timedelta(days=200))

    def test_term_leg_none_outside_band(self):
        expiries = [ASOF + dt.timedelta(days=d) for d in (17, 45, 400)]
        self.assertIsNone(f.select_term_expiry(expiries, ASOF))


class TestIvSanityAndAtm(unittest.TestCase):
    def test_sane_iv_band(self):
        self.assertTrue(f.sane_iv(0.5))
        self.assertFalse(f.sane_iv(0.001))   # stale/broken near-zero quote
        self.assertFalse(f.sane_iv(7.0))     # zero-bid inversion blow-up
        self.assertFalse(f.sane_iv(None))
        self.assertFalse(f.sane_iv(float("nan")))

    def test_atm_strike_needs_both_legs(self):
        calls = _chain([95.0, 100.0, 105.0], [0.5, 0.5, 0.5])
        puts = _chain([95.0, 105.0], [0.5, 0.5])  # 100 missing on the put side
        self.assertEqual(f.atm_strike(calls, puts, spot=101.0), 105.0)

    def test_expiry_atm_iv_midpoint(self):
        calls = _chain([100.0], [0.50])
        puts = _chain([100.0], [0.54])
        self.assertAlmostEqual(f.expiry_atm_iv(calls, puts, spot=100.0), 0.52)

    def test_expiry_atm_iv_single_sane_leg(self):
        calls = _chain([100.0], [0.0001])  # insane vendor IV
        puts = _chain([100.0], [0.54])
        self.assertAlmostEqual(f.expiry_atm_iv(calls, puts, spot=100.0), 0.54)

    def test_expiry_atm_iv_both_insane_is_none(self):
        calls = _chain([100.0], [0.0001])
        puts = _chain([100.0], [9.9])
        self.assertIsNone(f.expiry_atm_iv(calls, puts, spot=100.0))


class TestInterpolationAndSkew(unittest.TestCase):
    def test_linear_interpolation_at_30(self):
        # 20 DTE @ 0.60, 40 DTE @ 0.40 -> 30 DTE @ 0.50
        self.assertAlmostEqual(f.interpolate_iv30(0.60, 20, 0.40, 40), 0.50)

    def test_single_leg_is_flat(self):
        self.assertAlmostEqual(f.interpolate_iv30(None, None, 0.40, 45), 0.40)
        self.assertAlmostEqual(f.interpolate_iv30(0.60, 20, None, None), 0.60)

    def test_no_legs_is_none(self):
        self.assertIsNone(f.interpolate_iv30(None, None, None, None))

    def test_skew_xzz(self):
        # spot 100: OTM put window [80, 95] closest to 95 -> strike 94 @ 0.62;
        # ATM call window [95, 105] closest to 100 -> strike 101 @ 0.50.
        puts = _chain([75.0, 90.0, 94.0], [0.70, 0.65, 0.62])
        calls = _chain([96.0, 101.0, 110.0], [0.52, 0.50, 0.48])
        self.assertAlmostEqual(f.skew_xzz(calls, puts, spot=100.0), 0.12)

    def test_skew_none_when_no_otm_put_in_window(self):
        puts = _chain([50.0], [0.70])  # moneyness 0.5, outside [0.80, 0.95]
        calls = _chain([100.0], [0.50])
        self.assertIsNone(f.skew_xzz(calls, puts, spot=100.0))


class TestQuoteAndTotals(unittest.TestCase):
    def test_atm_quote_spread_pct(self):
        calls = _chain([100.0], [0.5], bids=[3.0], asks=[3.2])
        puts = _chain([100.0], [0.5])
        strike, mid, spread_pct = f.atm_quote(calls, puts, spot=100.0)
        self.assertEqual(strike, 100.0)
        self.assertAlmostEqual(mid, 3.1)
        self.assertAlmostEqual(spread_pct, 0.2 / 3.1)

    def test_atm_quote_falls_back_to_put_on_zero_bid_call(self):
        calls = _chain([100.0], [0.5], bids=[0.0], asks=[0.4])
        puts = _chain([100.0], [0.5], bids=[2.0], asks=[2.2])
        strike, mid, spread_pct = f.atm_quote(calls, puts, spot=100.0)
        self.assertAlmostEqual(mid, 2.1)

    def test_atm_quote_none_when_both_unusable(self):
        calls = _chain([100.0], [0.5], bids=[0.0], asks=[0.4])
        puts = _chain([100.0], [0.5], bids=[3.0], asks=[2.0])  # ask < bid
        self.assertIsNone(f.atm_quote(calls, puts, spot=100.0))

    def test_chain_totals_sum_both_legs(self):
        e1 = (_chain([100.0], [0.5], vol=[10], oi=[100]), _chain([100.0], [0.5], vol=[4], oi=[40]))
        e2 = (_chain([100.0], [0.5], vol=[6], oi=[60]), _chain([100.0], [0.5], vol=[1], oi=[10]))
        totals = f.chain_totals([e1, e2])
        self.assertEqual(totals["call_vol"], 16.0)
        self.assertEqual(totals["put_vol"], 5.0)
        self.assertEqual(totals["call_oi"], 160.0)
        self.assertEqual(totals["put_oi"], 50.0)


class TestChainQuality(unittest.TestCase):
    def _ok_kwargs(self):
        return dict(
            has_chain=True,
            near=ASOF + dt.timedelta(days=17),
            far=ASOF + dt.timedelta(days=45),
            atm=100.0,
            atm_call_oi=60.0,
            atm_put_oi=55.0,
            atm_vol_total=3.0,
            spread_pct=0.05,
        )

    def test_ok(self):
        self.assertEqual(f.classify_chain_quality(**self._ok_kwargs()), f.CHAIN_QUALITY_OK)

    def test_none_when_no_chain(self):
        kw = self._ok_kwargs()
        kw.update(has_chain=False, near=None, far=None, atm=None)
        self.assertEqual(f.classify_chain_quality(**kw), f.CHAIN_QUALITY_NONE)

    def test_thin_on_single_expiry(self):
        kw = self._ok_kwargs()
        kw.update(near=None)
        self.assertEqual(f.classify_chain_quality(**kw), f.CHAIN_QUALITY_THIN)

    def test_thin_on_low_oi(self):
        kw = self._ok_kwargs()
        kw.update(atm_put_oi=10.0)
        self.assertEqual(f.classify_chain_quality(**kw), f.CHAIN_QUALITY_THIN)

    def test_thin_on_zero_volume(self):
        kw = self._ok_kwargs()
        kw.update(atm_vol_total=0.0)
        self.assertEqual(f.classify_chain_quality(**kw), f.CHAIN_QUALITY_THIN)

    def test_thin_on_wide_spread(self):
        kw = self._ok_kwargs()
        kw.update(spread_pct=0.25)
        self.assertEqual(f.classify_chain_quality(**kw), f.CHAIN_QUALITY_THIN)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.options_telemetry.test_features -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'alphalens_pipeline.thematic.options_telemetry'`

- [ ] **Step 3: Write minimal implementation**

`alphalens_pipeline/thematic/options_telemetry/__init__.py`:

```python
"""Display-only options telemetry stamped at the thematic score stage.

Design memo: docs/research/options_telemetry_design_2026_07_07.md.
Forward-only yfinance chain snapshot; never touches selection, ordering,
or the brief sort.
"""
```

`alphalens_pipeline/thematic/options_telemetry/features.py`:

```python
"""Pure feature construction over already-fetched option-chain frames.

No network in this module: every function takes yfinance-shaped chain
DataFrames (per-contract ``strike``, ``bid``, ``ask``, ``impliedVolatility``,
``openInterest``, ``volume``) plus scalars. The vendor IV field has
documented bugs (stale/zero-bid inversions), so every IV passes
:func:`sane_iv` before use — an insane leg degrades, never propagates.
"""

from __future__ import annotations

import datetime as dt
import math

import pandas as pd

OPTIONS_CONFIG_VERSION = "options-telemetry-v1-yf-snapshot"

IV_SANITY_MIN = 0.01
IV_SANITY_MAX = 5.0
ATM_MIN_OI = 50
ATM_MAX_SPREAD_PCT = 0.10
NEAR_LEG_MIN_DTE = 7
IV30_TARGET_DTE = 30
TERM_LEG_DTE_BAND = (120, 270)
TERM_LEG_TARGET_DTE = 180
SKEW_OTM_PUT_MONEYNESS = (0.80, 0.95)
SKEW_ATM_CALL_MONEYNESS = (0.95, 1.05)

CHAIN_QUALITY_NONE = "NONE"
CHAIN_QUALITY_THIN = "THIN"
CHAIN_QUALITY_OK = "OK"


def sane_iv(value: float | None) -> bool:
    """True when the vendor IV is inside the plausibility band."""
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if math.isnan(v):
        return False
    return IV_SANITY_MIN <= v <= IV_SANITY_MAX


def select_bracketing_expiries(
    expiries: list[dt.date], asof: dt.date
) -> tuple[dt.date | None, dt.date | None]:
    """``(near, far)`` legs bracketing 30 DTE.

    ``near`` is the latest expiry with ``NEAR_LEG_MIN_DTE <= dte <= 30``
    (sub-7-DTE legs are gamma-week noise, skipped). ``far`` is the first
    expiry strictly past 30 DTE. Either can be ``None``.
    """
    near = None
    far = None
    for e in sorted(expiries):
        dte = (e - asof).days
        if NEAR_LEG_MIN_DTE <= dte <= IV30_TARGET_DTE:
            near = e
        elif dte > IV30_TARGET_DTE and far is None:
            far = e
    return near, far


def select_term_expiry(expiries: list[dt.date], asof: dt.date) -> dt.date | None:
    """The expiry with DTE closest to 180 inside ``TERM_LEG_DTE_BAND``."""
    lo, hi = TERM_LEG_DTE_BAND
    in_band = [e for e in expiries if lo <= (e - asof).days <= hi]
    if not in_band:
        return None
    return min(in_band, key=lambda e: abs((e - asof).days - TERM_LEG_TARGET_DTE))


def _row_at_strike(frame: pd.DataFrame, strike: float) -> pd.Series | None:
    if frame is None or frame.empty or "strike" not in frame.columns:
        return None
    hits = frame[frame["strike"] == strike]
    if hits.empty:
        return None
    return hits.iloc[0]


def atm_strike(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> float | None:
    """Nearest-to-spot strike listed on BOTH legs (midpoint IV needs both)."""
    if calls is None or puts is None or calls.empty or puts.empty:
        return None
    common = set(calls["strike"]) & set(puts["strike"])
    if not common:
        return None
    return min(common, key=lambda k: abs(float(k) - spot))


def expiry_atm_iv(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> float | None:
    """Midpoint of the sane call/put IVs at the ATM strike.

    One insane leg degrades to the other; both insane -> ``None``.
    """
    strike = atm_strike(calls, puts, spot)
    if strike is None:
        return None
    legs = []
    for frame in (calls, puts):
        row = _row_at_strike(frame, strike)
        if row is not None and sane_iv(row.get("impliedVolatility")):
            legs.append(float(row["impliedVolatility"]))
    if not legs:
        return None
    return sum(legs) / len(legs)


def interpolate_iv30(
    iv_near: float | None,
    dte_near: int | None,
    iv_far: float | None,
    dte_far: int | None,
) -> float | None:
    """Linear-in-DTE interpolation to 30 DTE; a single leg is used flat.

    Telemetry-grade simplification (spec §4 / review §8): NOT a traded
    curve — the audit columns keep it recomputable at analysis time.
    """
    have_near = iv_near is not None and dte_near is not None
    have_far = iv_far is not None and dte_far is not None
    if have_near and have_far:
        if dte_far == dte_near:
            return iv_near
        w = (IV30_TARGET_DTE - dte_near) / (dte_far - dte_near)
        return iv_near + w * (iv_far - iv_near)
    if have_near:
        return iv_near
    if have_far:
        return iv_far
    return None


def _pick_in_moneyness(
    frame: pd.DataFrame, spot: float, window: tuple[float, float], anchor: float
) -> float | None:
    """Sane IV of the contract whose K/S is inside ``window``, closest to ``anchor``."""
    if frame is None or frame.empty:
        return None
    lo, hi = window
    best_iv = None
    best_dist = None
    for _, row in frame.iterrows():
        strike = float(row["strike"])
        m = strike / spot
        if not (lo <= m <= hi) or not sane_iv(row.get("impliedVolatility")):
            continue
        dist = abs(m - anchor)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_iv = float(row["impliedVolatility"])
    return best_iv


def skew_xzz(calls: pd.DataFrame, puts: pd.DataFrame, spot: float) -> float | None:
    """Xing-Zhang-Zhao smirk: OTM-put IV minus ATM-call IV (moneyness-based)."""
    otm_put = _pick_in_moneyness(puts, spot, SKEW_OTM_PUT_MONEYNESS, anchor=SKEW_OTM_PUT_MONEYNESS[1])
    atm_call = _pick_in_moneyness(calls, spot, SKEW_ATM_CALL_MONEYNESS, anchor=1.0)
    if otm_put is None or atm_call is None:
        return None
    return otm_put - atm_call


def _usable_quote(row: pd.Series | None) -> tuple[float, float] | None:
    """``(mid, spread_pct)`` from a contract row, or ``None`` when untradable."""
    if row is None:
        return None
    try:
        bid = float(row.get("bid"))
        ask = float(row.get("ask"))
    except (TypeError, ValueError):
        return None
    if math.isnan(bid) or math.isnan(ask) or bid <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return mid, (ask - bid) / mid


def atm_quote(
    calls: pd.DataFrame, puts: pd.DataFrame, spot: float
) -> tuple[float, float, float] | None:
    """``(strike, mid, spread_pct)`` at the ATM strike — call leg first,
    put leg as fallback; ``None`` when both quotes are unusable."""
    strike = atm_strike(calls, puts, spot)
    if strike is None:
        return None
    for frame in (calls, puts):
        quote = _usable_quote(_row_at_strike(frame, strike))
        if quote is not None:
            mid, spread_pct = quote
            return float(strike), mid, spread_pct
    return None


def chain_totals(legs: list[tuple[pd.DataFrame, pd.DataFrame]]) -> dict[str, float]:
    """Raw put/call volume + OI summed over the bracketing expiries.

    These are the *ingredients* of the abnormal-P/C construction — the raw
    P/C ratio itself is a validated null (Pan-Poteshman) and is deliberately
    not a column.
    """
    def _sum(frame: pd.DataFrame, col: str) -> float:
        if frame is None or frame.empty or col not in frame.columns:
            return 0.0
        return float(pd.to_numeric(frame[col], errors="coerce").fillna(0).sum())

    totals = {"call_vol": 0.0, "put_vol": 0.0, "call_oi": 0.0, "put_oi": 0.0}
    for calls, puts in legs:
        totals["call_vol"] += _sum(calls, "volume")
        totals["put_vol"] += _sum(puts, "volume")
        totals["call_oi"] += _sum(calls, "openInterest")
        totals["put_oi"] += _sum(puts, "openInterest")
    return totals


def classify_chain_quality(
    *,
    has_chain: bool,
    near: dt.date | None,
    far: dt.date | None,
    atm: float | None,
    atm_call_oi: float | None,
    atm_put_oi: float | None,
    atm_vol_total: float | None,
    spread_pct: float | None,
) -> str:
    """Spec §4 pinned dimensions: NONE / THIN / OK.

    OK needs both bracketing expiries, an ATM strike on both legs, per-leg
    OI >= ATM_MIN_OI, non-zero ATM volume on the asof session, and an ATM
    relative spread <= ATM_MAX_SPREAD_PCT. Anything less (but with a chain
    present) is THIN.
    """
    if not has_chain:
        return CHAIN_QUALITY_NONE
    if near is None or far is None or atm is None:
        return CHAIN_QUALITY_THIN
    if atm_call_oi is None or atm_call_oi < ATM_MIN_OI:
        return CHAIN_QUALITY_THIN
    if atm_put_oi is None or atm_put_oi < ATM_MIN_OI:
        return CHAIN_QUALITY_THIN
    if atm_vol_total is None or atm_vol_total <= 0:
        return CHAIN_QUALITY_THIN
    if spread_pct is None or spread_pct > ATM_MAX_SPREAD_PCT:
        return CHAIN_QUALITY_THIN
    return CHAIN_QUALITY_OK
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.options_telemetry.test_features -v`
Expected: PASS (all ~22 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/thematic/options_telemetry/ apps/alphalens-research/tests/thematic/options_telemetry/
git commit -m "feat(thematic): pure option-chain feature functions for options telemetry"
```

---

### Task 4: realized vol from the grouped-daily store + VRP ratio

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/thematic/options_telemetry/features.py` (append)
- Test: `apps/alphalens-research/tests/thematic/options_telemetry/test_features.py` (append a class)

**Interfaces:**
- Consumes: `alphalens_pipeline.data.rs_history.read_grouped_day(root: Path, date: dt.date) -> dict[str, dict] | None` (bars keyed by upper ticker, close under `"c"`), `alphalens_pipeline.paper.calendar.previous_trading_day`, `is_trading_day`.
- Produces:
  - `trailing_session_closes(root: Path, tickers: list[str], asof: dt.date, n_sessions: int) -> dict[str, list[float]]` — per-ticker close series (oldest→newest), loading each grouped-day parquet ONCE for all tickers.
  - `realized_vol_20d(closes: list[float]) -> float | None` — annualized stdev of daily log returns over the last 20 returns (needs >= 21 closes).
  - `vrp_ratio(ivx30: float | None, rv20: float | None) -> float | None`

- [ ] **Step 1: Write the failing test** — append to `test_features.py`:

```python
class TestRealizedVolAndVrp(unittest.TestCase):
    def test_realized_vol_20d_constant_returns_is_zero(self):
        closes = [100.0 * (1.01**i) for i in range(21)]  # constant 1% daily
        self.assertAlmostEqual(f.realized_vol_20d(closes), 0.0, places=10)

    def test_realized_vol_20d_known_value(self):
        # Alternating +1%/-1% log-ish moves -> non-zero annualized stdev.
        closes = [100.0]
        for i in range(20):
            closes.append(closes[-1] * (1.01 if i % 2 == 0 else 0.99))
        rv = f.realized_vol_20d(closes)
        self.assertIsNotNone(rv)
        self.assertGreater(rv, 0.10)

    def test_too_few_closes_is_none(self):
        self.assertIsNone(f.realized_vol_20d([100.0] * 20))  # 19 returns < 20

    def test_vrp_ratio(self):
        self.assertAlmostEqual(f.vrp_ratio(0.5, 0.25), 2.0)
        self.assertIsNone(f.vrp_ratio(None, 0.25))
        self.assertIsNone(f.vrp_ratio(0.5, None))
        self.assertIsNone(f.vrp_ratio(0.5, 0.0))  # zero RV: ratio undefined


class TestTrailingSessionCloses(unittest.TestCase):
    def test_reads_store_once_per_session(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 3 consecutive XNYS sessions ending Mon 2026-07-06.
            days = [dt.date(2026, 7, 1), dt.date(2026, 7, 2), dt.date(2026, 7, 6)]
            for i, day in enumerate(days):
                pd.DataFrame(
                    {"T": ["QUBT", "IONQ"], "c": [10.0 + i, 20.0 + i]}
                ).to_parquet(root / f"{day.isoformat()}.parquet")
            out = f.trailing_session_closes(root, ["QUBT", "MISSING"], dt.date(2026, 7, 6), 3)
        self.assertEqual(out["QUBT"], [10.0, 11.0, 12.0])
        self.assertEqual(out["MISSING"], [])
```

Note: the exact parquet layout `read_grouped_day` expects must match what `backfill_grouped_daily_history.py` writes. Before finalizing this test, open `alphalens_pipeline/data/rs_history.py::read_grouped_day` (lines 54-73) and mirror its expected columns (symbol key + `"c"` close). If it reads a different column name than `"T"`, adjust the fixture — the assertion values stay the same.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.options_telemetry.test_features.TestRealizedVolAndVrp tests.thematic.options_telemetry.test_features.TestTrailingSessionCloses -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'realized_vol_20d'`

- [ ] **Step 3: Write minimal implementation** — append to `features.py`:

```python
RV_WINDOW_RETURNS = 20
RV_ANNUALIZATION = 252.0


def trailing_session_closes(
    root, tickers: list[str], asof: dt.date, n_sessions: int
) -> dict[str, list[float]]:
    """Per-ticker closes over the last ``n_sessions`` XNYS sessions ending at
    the newest session <= ``asof``, read disk-only off the grouped store.

    Loads each grouped-day snapshot ONCE for all tickers (the snapshots are
    whole-market). A ticker missing from ANY loaded session returns [] —
    a gapped series would silently understate realized vol.
    """
    from alphalens_pipeline.data.rs_history import read_grouped_day
    from alphalens_pipeline.paper.calendar import is_trading_day, previous_trading_day

    session = asof if is_trading_day(asof) else previous_trading_day(asof)
    sessions: list[dt.date] = []
    for _ in range(n_sessions):
        sessions.append(session)
        session = previous_trading_day(session)
    sessions.reverse()

    wanted = {t.upper() for t in tickers}
    per_ticker: dict[str, list[float]] = {t.upper(): [] for t in tickers}
    for day in sessions:
        snapshot = read_grouped_day(root, day)
        if snapshot is None:
            continue
        for t in wanted:
            bar = snapshot.get(t)
            if bar is not None and bar.get("c") is not None:
                per_ticker[t].append(float(bar["c"]))
    return {
        t: closes if len(closes) == n_sessions else []
        for t, closes in per_ticker.items()
    }


def realized_vol_20d(closes: list[float]) -> float | None:
    """Annualized stdev of the last 20 daily log returns; None when short."""
    if len(closes) < RV_WINDOW_RETURNS + 1:
        return None
    rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(len(closes) - RV_WINDOW_RETURNS, len(closes))
    ]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * RV_ANNUALIZATION)


def vrp_ratio(ivx30: float | None, rv20: float | None) -> float | None:
    """IV30 / 20d realized vol (v9D ``ivx30_over_hv20``); None on missing/zero RV."""
    if ivx30 is None or rv20 is None or rv20 <= 0:
        return None
    return ivx30 / rv20
```

Also adjust `trailing_session_closes` to strictly require full coverage (the dict comprehension already returns `[]` on gaps). If `read_grouped_day`'s snapshot access differs (e.g. bars are dataclasses not dicts), adapt `bar.get("c")` accordingly — check the module first.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.options_telemetry.test_features -v`
Expected: PASS (whole module)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/thematic/options_telemetry/features.py apps/alphalens-research/tests/thematic/options_telemetry/test_features.py
git commit -m "feat(thematic): realized-vol + VRP ratio off the grouped-daily store"
```

---

### Task 5: the enricher — window rule, per-ticker snapshot, freeze/carry-forward, stamping

**Files:**
- Create: `apps/alphalens-pipeline/alphalens_pipeline/thematic/options_telemetry/enrichment.py`
- Test: `apps/alphalens-research/tests/thematic/options_telemetry/test_enrichment.py`

**Interfaces:**
- Consumes: Task 1 `session_close_utc`, calendar `next_trading_open`, `is_trading_day`, `previous_trading_day`; Task 2 client methods; Task 3/4 feature functions.
- Produces (consumed by Task 6 CLI wiring):
  - `OPTIONS_COLUMNS: tuple[str, ...]` — the 16 column names in the table order above.
  - `stamp_window_utc(asof: dt.date, exchange: str = "XNYS") -> tuple[dt.datetime, dt.datetime]` — `(session_close, next_open)` for the newest session <= asof.
  - `enrich(frame: pd.DataFrame, *, asof: dt.date, now_utc: dt.datetime | None = None, previous: pd.DataFrame | None = None, snapshot_fn: SnapshotFn | None = None, grouped_root: Path | None = None) -> pd.DataFrame`
  - `TickerSnapshot` dataclass: `spot: float | None`, `expiries: list[dt.date]`, `chains: dict[dt.date, tuple[pd.DataFrame, pd.DataFrame]]` (only fetched expiries present), `fetch_failed: bool`.
  - `SnapshotFn = Callable[[str, dt.date], TickerSnapshot]` — `(ticker, asof) -> TickerSnapshot`; default wires the canonical yfinance client (`last_price` for spot, `option_expiries`, `option_chain` for the near/far/term legs only — max 4 HTTP calls per ticker).

**Behavior contract (test these exactly):**
1. `now_utc` inside the window and no previous stamp → all 16 columns stamped; `options_snapshot_utc` = ISO of `now_utc`; `options_config_version` constant everywhere.
2. `now_utc` outside the window, `previous=None` → all columns present but null (`options_config_version` still stamped — it is constant metadata, not a measurement).
3. `now_utc` outside the window, `previous` has a stamped row for the ticker → the 16 values are copied from `previous` (freeze semantics).
4. `now_utc` INSIDE the window but `previous` already stamped (non-null `options_snapshot_utc`) → previous values carried, NOT re-fetched (first success freezes; also saves HTTP).
5. Snapshot fetch failure (`fetch_failed=True`) → `options_chain_quality="NONE"`, numerics NaN, `options_snapshot_utc` stamped (the attempt happened) — per spec §3.1/§4.
6. Ticker with no listed options (`expiries=[]`) → quality `"NONE"`, totals NaN.
7. Multi-row tickers (same ticker under two themes) get identical values (computed once per unique ticker).
8. A per-ticker exception inside feature computation is caught and degrades that ticker to `"NONE"` — never raises out of `enrich`.

- [ ] **Step 1: Write the failing test** — `tests/thematic/options_telemetry/test_enrichment.py`:

```python
"""Enricher contract tests: window rule, freeze/carry-forward, NaN discipline."""

from __future__ import annotations

import datetime as dt
import math
import unittest

import pandas as pd

from alphalens_pipeline.thematic.options_telemetry import enrichment as en
from alphalens_pipeline.thematic.options_telemetry import features as f

ASOF = dt.date(2026, 7, 6)  # Monday, regular XNYS session (close 20:00 UTC)
IN_WINDOW = dt.datetime(2026, 7, 7, 0, 30, tzinfo=dt.UTC)     # 00:30 UTC slot
OUT_OF_WINDOW = dt.datetime(2026, 7, 7, 16, 30, tzinfo=dt.UTC)  # during next session


def _frame(tickers=("QUBT", "IONQ")) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "theme": ["quantum"] * len(tickers),
            "ticker": list(tickers),
            "company_name": [f"{t} Corp" for t in tickers],
        }
    )


def _chain_frame(iv=0.5, oi=100, vol=10, bid=3.0, ask=3.2) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "strike": [100.0],
            "bid": [bid],
            "ask": [ask],
            "impliedVolatility": [iv],
            "openInterest": [oi],
            "volume": [vol],
        }
    )


def _good_snapshot(ticker: str, asof: dt.date) -> en.TickerSnapshot:
    near = asof + dt.timedelta(days=18)
    far = asof + dt.timedelta(days=46)
    term = asof + dt.timedelta(days=186)
    return en.TickerSnapshot(
        spot=100.0,
        expiries=[near, far, term],
        chains={
            near: (_chain_frame(iv=0.60), _chain_frame(iv=0.62)),
            far: (_chain_frame(iv=0.55), _chain_frame(iv=0.57)),
            term: (_chain_frame(iv=0.45), _chain_frame(iv=0.47)),
        },
        fetch_failed=False,
    )


def _no_options_snapshot(ticker: str, asof: dt.date) -> en.TickerSnapshot:
    return en.TickerSnapshot(spot=100.0, expiries=[], chains={}, fetch_failed=False)


def _failed_snapshot(ticker: str, asof: dt.date) -> en.TickerSnapshot:
    return en.TickerSnapshot(spot=None, expiries=[], chains={}, fetch_failed=True)


class TestStampWindow(unittest.TestCase):
    def test_window_for_monday_session(self):
        close, nxt_open = en.stamp_window_utc(ASOF)
        self.assertEqual(close, dt.datetime(2026, 7, 6, 20, 0, tzinfo=dt.UTC))
        self.assertEqual(nxt_open, dt.datetime(2026, 7, 7, 13, 30, tzinfo=dt.UTC))

    def test_weekend_asof_rolls_to_prior_session(self):
        # Sunday asof -> Friday 2026-07-03 session window... 2026-07-03 is the
        # Independence Day observed holiday; the prior session is Thu 07-02.
        close, _ = en.stamp_window_utc(dt.date(2026, 7, 5))
        self.assertEqual(close.date(), dt.date(2026, 7, 2))


class TestEnrichInWindow(unittest.TestCase):
    def test_stamps_all_columns(self):
        out = en.enrich(
            _frame(), asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_good_snapshot
        )
        for col in en.OPTIONS_COLUMNS:
            self.assertIn(col, out.columns)
        row = out.iloc[0]
        # near 18 DTE @ 0.61 mid, far 46 DTE @ 0.56 mid -> 30 DTE interp
        self.assertAlmostEqual(
            row["options_ivx30"], 0.61 + (30 - 18) / (46 - 18) * (0.56 - 0.61), places=6
        )
        self.assertAlmostEqual(row["options_term_slope"], 0.46 - row["options_ivx30"], places=6)
        self.assertEqual(row["options_chain_quality"], f.CHAIN_QUALITY_OK)
        self.assertEqual(row["options_snapshot_utc"], IN_WINDOW.isoformat())
        self.assertEqual(row["options_config_version"], f.OPTIONS_CONFIG_VERSION)
        self.assertEqual(row["options_put_vol"], 20.0)   # near+far puts: 10+10
        self.assertEqual(row["options_call_oi"], 200.0)  # near+far calls: 100+100
        self.assertEqual(row["options_asof_expiry_near"], (ASOF + dt.timedelta(days=18)).isoformat())

    def test_no_listed_options_is_quality_none(self):
        out = en.enrich(
            _frame(), asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_no_options_snapshot
        )
        row = out.iloc[0]
        self.assertEqual(row["options_chain_quality"], f.CHAIN_QUALITY_NONE)
        self.assertTrue(math.isnan(row["options_ivx30"]))
        self.assertTrue(math.isnan(row["options_put_vol"]))

    def test_fetch_failure_is_quality_none_and_never_raises(self):
        out = en.enrich(
            _frame(), asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_failed_snapshot
        )
        self.assertEqual(out.iloc[0]["options_chain_quality"], f.CHAIN_QUALITY_NONE)

    def test_feature_exception_degrades_to_none_quality(self):
        def _raising(ticker: str, asof: dt.date):
            raise RuntimeError("boom")

        out = en.enrich(_frame(), asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_raising)
        self.assertEqual(out.iloc[0]["options_chain_quality"], f.CHAIN_QUALITY_NONE)

    def test_duplicate_ticker_rows_get_identical_values_one_fetch(self):
        calls = {"n": 0}

        def _counting(ticker: str, asof: dt.date):
            calls["n"] += 1
            return _good_snapshot(ticker, asof)

        frame = _frame(tickers=("QUBT", "QUBT"))
        out = en.enrich(frame, asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_counting)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(out.iloc[0]["options_ivx30"], out.iloc[1]["options_ivx30"])


class TestEnrichWindowAndFreeze(unittest.TestCase):
    def test_out_of_window_no_previous_leaves_nulls(self):
        out = en.enrich(
            _frame(), asof=ASOF, now_utc=OUT_OF_WINDOW, snapshot_fn=_good_snapshot
        )
        row = out.iloc[0]
        self.assertTrue(math.isnan(row["options_ivx30"]))
        self.assertIsNone(row["options_snapshot_utc"])
        self.assertIsNone(row["options_chain_quality"])
        self.assertEqual(row["options_config_version"], f.OPTIONS_CONFIG_VERSION)

    def test_out_of_window_carries_previous_stamp(self):
        first = en.enrich(
            _frame(), asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_good_snapshot
        )
        out = en.enrich(
            _frame(),
            asof=ASOF,
            now_utc=OUT_OF_WINDOW,
            previous=first,
            snapshot_fn=_failed_snapshot,  # must not matter: no fetch out of window
        )
        pd.testing.assert_series_equal(
            out["options_ivx30"], first["options_ivx30"], check_names=False
        )
        self.assertEqual(out.iloc[0]["options_snapshot_utc"], IN_WINDOW.isoformat())

    def test_in_window_previous_stamp_freezes_no_refetch(self):
        first = en.enrich(
            _frame(), asof=ASOF, now_utc=IN_WINDOW, snapshot_fn=_good_snapshot
        )
        calls = {"n": 0}

        def _counting(ticker: str, asof: dt.date):
            calls["n"] += 1
            return _good_snapshot(ticker, asof)

        later = dt.datetime(2026, 7, 7, 4, 30, tzinfo=dt.UTC)  # still in window
        out = en.enrich(
            _frame(), asof=ASOF, now_utc=later, previous=first, snapshot_fn=_counting
        )
        self.assertEqual(calls["n"], 0)
        self.assertEqual(out.iloc[0]["options_snapshot_utc"], IN_WINDOW.isoformat())

    def test_previous_unstamped_row_refetches_in_window(self):
        # Previous run was out-of-window (nulls) -> this in-window run stamps.
        unstamped = en.enrich(
            _frame(), asof=ASOF, now_utc=OUT_OF_WINDOW, snapshot_fn=_good_snapshot
        )
        out = en.enrich(
            _frame(),
            asof=ASOF,
            now_utc=IN_WINDOW,
            previous=unstamped,
            snapshot_fn=_good_snapshot,
        )
        self.assertEqual(out.iloc[0]["options_chain_quality"], f.CHAIN_QUALITY_OK)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.options_telemetry.test_enrichment -v`
Expected: FAIL with `ModuleNotFoundError` / `AttributeError` on `enrichment`

- [ ] **Step 3: Write minimal implementation** — `enrichment.py`:

```python
"""Frame enricher: stamp the 16 ``options_*`` telemetry columns.

Mirrors the Buffett/O'Neil ``enrich(frame, *, asof)`` pattern (per-unique-
ticker computation, tri-state None -> NaN in float64 columns, fail-soft per
ticker). The §3.1 snapshot-window rule and first-success freeze live here.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from alphalens_pipeline.thematic.options_telemetry import features as f

logger = logging.getLogger(__name__)

_FLOAT_COLUMNS: tuple[str, ...] = (
    "options_ivx30",
    "options_term_slope",
    "options_vrp_ratio",
    "options_skew_xzz",
    "options_put_vol",
    "options_call_vol",
    "options_put_oi",
    "options_call_oi",
    "options_spread_pct_atm",
    "options_atm_strike",
    "options_atm_mid",
    "options_spot",
)
_STR_COLUMNS: tuple[str, ...] = (
    "options_chain_quality",
    "options_asof_expiry_near",
    "options_snapshot_utc",
    "options_config_version",
)
OPTIONS_COLUMNS: tuple[str, ...] = _FLOAT_COLUMNS + _STR_COLUMNS

RV_SESSIONS_NEEDED = f.RV_WINDOW_RETURNS + 1  # 21 closes -> 20 returns

_DEFAULT_GROUPED_ROOT = Path.home() / ".alphalens" / "grouped_daily_history"


@dataclass(frozen=True)
class TickerSnapshot:
    """Already-fetched chain state for one ticker (no network past here)."""

    spot: float | None
    expiries: list[dt.date]
    chains: dict[dt.date, tuple[pd.DataFrame, pd.DataFrame]] = field(default_factory=dict)
    fetch_failed: bool = False


SnapshotFn = Callable[[str, dt.date], TickerSnapshot]


def stamp_window_utc(
    asof: dt.date, exchange: str = "XNYS"
) -> tuple[dt.datetime, dt.datetime]:
    """``(session_close, next_open)`` for the newest session <= ``asof``.

    Snapshots inside this window see the asof session's FINAL daily volume,
    the day's cleared OI, and at-close quotes — the only state valid to
    attribute to ``asof`` (spec §3.1).
    """
    from alphalens_pipeline.paper.calendar import (
        is_trading_day,
        next_trading_open,
        previous_trading_day,
        session_close_utc,
    )

    session = asof if is_trading_day(asof, exchange) else previous_trading_day(asof, exchange)
    close = session_close_utc(session, exchange)
    return close, next_trading_open(close, exchange)


def _null_values() -> dict[str, object]:
    values: dict[str, object] = {col: None for col in OPTIONS_COLUMNS}
    values["options_config_version"] = f.OPTIONS_CONFIG_VERSION
    return values


def _compute_values(
    snapshot: TickerSnapshot,
    *,
    asof: dt.date,
    now_utc: dt.datetime,
    rv20: float | None,
) -> dict[str, object]:
    values = _null_values()
    values["options_snapshot_utc"] = now_utc.isoformat()
    values["options_spot"] = snapshot.spot

    near, far = f.select_bracketing_expiries(snapshot.expiries, asof)
    term = f.select_term_expiry(snapshot.expiries, asof)
    has_chain = not snapshot.fetch_failed and bool(snapshot.expiries) and (
        near is not None or far is not None
    )
    if not has_chain or snapshot.spot is None:
        values["options_chain_quality"] = f.CHAIN_QUALITY_NONE
        return values

    spot = float(snapshot.spot)
    legs: list[tuple[pd.DataFrame, pd.DataFrame]] = []
    iv_near = dte_near = iv_far = dte_far = None
    quote = None
    skew = None
    atm_call_oi = atm_put_oi = atm_vol_total = None

    near_leg = snapshot.chains.get(near) if near is not None else None
    far_leg = snapshot.chains.get(far) if far is not None else None
    # The quote/skew/OI reference leg: near when present, else far.
    ref_leg = near_leg or far_leg

    if near_leg is not None:
        legs.append(near_leg)
        iv_near = f.expiry_atm_iv(near_leg[0], near_leg[1], spot)
        dte_near = (near - asof).days
    if far_leg is not None:
        legs.append(far_leg)
        iv_far = f.expiry_atm_iv(far_leg[0], far_leg[1], spot)
        dte_far = (far - asof).days

    ivx30 = f.interpolate_iv30(iv_near, dte_near, iv_far, dte_far)
    values["options_ivx30"] = ivx30
    values["options_vrp_ratio"] = f.vrp_ratio(ivx30, rv20)
    if near is not None:
        values["options_asof_expiry_near"] = near.isoformat()
    elif far is not None:
        values["options_asof_expiry_near"] = far.isoformat()

    term_leg = snapshot.chains.get(term) if term is not None else None
    if term_leg is not None and ivx30 is not None:
        iv_term = f.expiry_atm_iv(term_leg[0], term_leg[1], spot)
        if iv_term is not None:
            values["options_term_slope"] = iv_term - ivx30

    if ref_leg is not None:
        calls, puts = ref_leg
        skew = f.skew_xzz(calls, puts, spot)
        values["options_skew_xzz"] = skew
        quote = f.atm_quote(calls, puts, spot)
        if quote is not None:
            strike, mid, spread_pct = quote
            values["options_atm_strike"] = strike
            values["options_atm_mid"] = mid
            values["options_spread_pct_atm"] = spread_pct
            call_row = calls[calls["strike"] == strike]
            put_row = puts[puts["strike"] == strike]
            if not call_row.empty:
                atm_call_oi = float(call_row.iloc[0].get("openInterest") or 0)
            if not put_row.empty:
                atm_put_oi = float(put_row.iloc[0].get("openInterest") or 0)
            atm_vol_total = float(
                (0 if call_row.empty else call_row.iloc[0].get("volume") or 0)
                + (0 if put_row.empty else put_row.iloc[0].get("volume") or 0)
            )

    if legs:
        totals = f.chain_totals(legs)
        values["options_put_vol"] = totals["put_vol"]
        values["options_call_vol"] = totals["call_vol"]
        values["options_put_oi"] = totals["put_oi"]
        values["options_call_oi"] = totals["call_oi"]

    values["options_chain_quality"] = f.classify_chain_quality(
        has_chain=True,
        near=near,
        far=far,
        atm=values["options_atm_strike"],
        atm_call_oi=atm_call_oi,
        atm_put_oi=atm_put_oi,
        atm_vol_total=atm_vol_total,
        spread_pct=values["options_spread_pct_atm"],
    )
    return values


def _previous_by_ticker(previous: pd.DataFrame | None) -> dict[str, dict[str, object]]:
    """Ticker -> stamped 16-column dict from a previous same-asof output.

    Only rows with a non-null ``options_snapshot_utc`` count as stamped —
    that is the freeze marker (spec §3.1: first successful stamp wins).
    """
    if previous is None or "options_snapshot_utc" not in getattr(previous, "columns", ()):  # noqa: E501
        return {}
    stamped: dict[str, dict[str, object]] = {}
    for _, row in previous.iterrows():
        marker = row.get("options_snapshot_utc")
        if marker is None or (isinstance(marker, float) and pd.isna(marker)):
            continue
        ticker = str(row.get("ticker", "")).upper()
        if ticker and ticker not in stamped:
            stamped[ticker] = {
                col: (None if pd.isna(row[col]) else row[col])
                if col in row.index
                else None
                for col in OPTIONS_COLUMNS
            }
    return stamped


def _default_snapshot_fn(asof: dt.date) -> SnapshotFn:
    """Production wiring: canonical yfinance client, max 4 HTTP calls/ticker."""
    from alphalens_pipeline.data.alt_data.yfinance_client import (
        get_default_yfinance_client,
    )

    client = get_default_yfinance_client()

    def _fetch(ticker: str, asof_date: dt.date) -> TickerSnapshot:
        expiries = client.option_expiries(ticker)
        if expiries is None:
            return TickerSnapshot(spot=None, expiries=[], fetch_failed=True)
        spot = client.last_price(ticker)
        near, far = f.select_bracketing_expiries(expiries, asof_date)
        term = f.select_term_expiry(expiries, asof_date)
        chains: dict[dt.date, tuple[pd.DataFrame, pd.DataFrame]] = {}
        for expiry in {e for e in (near, far, term) if e is not None}:
            leg = client.option_chain(ticker, expiry)
            if leg is not None:
                chains[expiry] = leg
        return TickerSnapshot(spot=spot, expiries=expiries, chains=chains)

    return _fetch


def enrich(
    frame: pd.DataFrame,
    *,
    asof: dt.date,
    now_utc: dt.datetime | None = None,
    previous: pd.DataFrame | None = None,
    snapshot_fn: SnapshotFn | None = None,
    grouped_root: Path | None = None,
) -> pd.DataFrame:
    """Return ``frame`` with the 16 ``options_*`` columns appended.

    Display-only telemetry — never reads or writes any selection/sort
    column. Per-ticker fail-soft: an exception degrades that ticker to
    ``chain_quality="NONE"`` and logs, never raises.
    """
    out = frame.copy()
    tickers = [str(t).upper() for t in out["ticker"]] if "ticker" in out.columns else []
    unique = list(dict.fromkeys(tickers))

    now = now_utc or dt.datetime.now(dt.UTC)
    close, next_open = stamp_window_utc(asof)
    in_window = close < now < next_open
    frozen = _previous_by_ticker(previous)

    per_ticker: dict[str, dict[str, object]] = {}
    fetch: SnapshotFn | None = None
    rv_by_ticker: dict[str, float | None] = {}

    to_fetch = [t for t in unique if t not in frozen] if in_window else []
    if to_fetch:
        fetch = snapshot_fn or _default_snapshot_fn(asof)
        root = grouped_root or _DEFAULT_GROUPED_ROOT
        try:
            closes = f.trailing_session_closes(root, to_fetch, asof, RV_SESSIONS_NEEDED)
        except Exception:  # store missing/corrupt: RV degrades to None
            logger.warning("options telemetry: grouped store read failed", exc_info=True)
            closes = {}
        rv_by_ticker = {
            t: f.realized_vol_20d(closes.get(t, [])) for t in to_fetch
        }

    for ticker in unique:
        if ticker in frozen:
            per_ticker[ticker] = frozen[ticker]
            continue
        if not in_window:
            per_ticker[ticker] = _null_values()
            continue
        try:
            snapshot = fetch(ticker, asof)
            per_ticker[ticker] = _compute_values(
                snapshot, asof=asof, now_utc=now, rv20=rv_by_ticker.get(ticker)
            )
        except Exception:
            logger.warning("options telemetry failed for %s", ticker, exc_info=True)
            failed = _null_values()
            failed["options_snapshot_utc"] = now.isoformat()
            failed["options_chain_quality"] = f.CHAIN_QUALITY_NONE
            per_ticker[ticker] = failed

    for col in _FLOAT_COLUMNS:
        out[col] = pd.Series(
            [per_ticker[t][col] if t else None for t in tickers],
            index=out.index,
            dtype="float64",
        )
    for col in _STR_COLUMNS:
        out[col] = pd.Series(
            [per_ticker[t][col] if t else None for t in tickers],
            index=out.index,
            dtype="object",
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.options_telemetry -v`
Expected: PASS (features + enrichment modules)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/thematic/options_telemetry/enrichment.py apps/alphalens-research/tests/thematic/options_telemetry/test_enrichment.py
git commit -m "feat(thematic): options telemetry enricher with snapshot-window + freeze"
```

---

### Task 6: CLI wiring in `thematic score`

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_cli/commands/thematic.py` (inside `score()`, after the `market_state.enrich` block, before `output_dir.mkdir`)
- Test: `apps/alphalens-research/tests/thematic/options_telemetry/test_cli_wiring.py`

**Interfaces:**
- Consumes: Task 5 `enrichment.enrich(frame, *, asof, previous=...)`.
- Produces: the scored parquet at `output_dir/<date>.parquet` now carries the 16 columns; earlier same-date output feeds `previous=` (carry-forward across the 6 daily runs).

The thematic-CLI-boundary bug class (function-tested but CLI-untested) is the known trap here — the test exercises the wiring, not just the module.

- [ ] **Step 1: Write the failing test** — `tests/thematic/options_telemetry/test_cli_wiring.py`:

```python
"""Score-CLI wiring: options enrichment is called with carry-forward previous."""

from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import patch

import pandas as pd


class TestScoreCliWiresOptionsEnrichment(unittest.TestCase):
    def test_score_command_source_wires_options_enrichment(self):
        # Static wiring pin (cheap, catches accidental removal): the score
        # command body must import + call the options enricher with previous=.
        import inspect

        from alphalens_cli.commands import thematic

        src = inspect.getsource(thematic.score)
        self.assertIn("options_telemetry", src)
        self.assertIn("previous=", src)

    def test_enrich_receives_previous_frame_when_output_exists(self):
        # Behavior pin through the helper the CLI calls (keeps the CLI thin).
        import tempfile
        from pathlib import Path

        from alphalens_cli.commands import thematic

        frame = pd.DataFrame({"theme": ["q"], "ticker": ["QUBT"], "company_name": ["Q"]})
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "2026-07-06.parquet"
            prev = frame.copy()
            prev["options_snapshot_utc"] = ["2026-07-07T00:30:00+00:00"]
            prev.to_parquet(out_path, index=False)

            captured = {}

            def _fake_enrich(fr, *, asof, previous=None, **kw):
                captured["previous"] = previous
                return fr

            with patch(
                "alphalens_pipeline.thematic.options_telemetry.enrichment.enrich",
                side_effect=_fake_enrich,
            ):
                thematic._apply_options_telemetry(frame, target=dt.date(2026, 7, 6), out_path=out_path)

        self.assertIsNotNone(captured["previous"])
        self.assertIn("options_snapshot_utc", captured["previous"].columns)

    def test_helper_is_fail_soft(self):
        import tempfile
        from pathlib import Path

        from alphalens_cli.commands import thematic

        frame = pd.DataFrame({"theme": ["q"], "ticker": ["QUBT"], "company_name": ["Q"]})
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "alphalens_pipeline.thematic.options_telemetry.enrichment.enrich",
                side_effect=RuntimeError("boom"),
            ),
        ):
            out = thematic._apply_options_telemetry(
                frame, target=dt.date(2026, 7, 6), out_path=Path(tmp) / "x.parquet"
            )
        pd.testing.assert_frame_equal(out, frame)  # unchanged, no raise


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.options_telemetry.test_cli_wiring -v`
Expected: FAIL with `AttributeError: module ... has no attribute '_apply_options_telemetry'`

- [ ] **Step 3: Write minimal implementation** — in `thematic.py`, add a module-level helper near the other private helpers, then call it inside `score()`:

```python
def _apply_options_telemetry(
    enriched: pd.DataFrame, *, target: dt.date, out_path: Path
) -> pd.DataFrame:
    """Stamp the display-only options telemetry (design memo 2026-07-07).

    yfinance chain snapshot, stamped only inside the post-close window for
    the asof session; the previous same-date output parquet (earlier run
    slot) provides carry-forward so the first successful stamp freezes.
    NOT in the brief sort. Fail-soft: any failure returns the frame
    unchanged rather than aborting the score stage. Lazy import keeps the
    frequent-cron `alphalens` startup cheap.
    """
    try:
        from alphalens_pipeline.thematic.options_telemetry import enrichment as options_enrichment

        previous = pd.read_parquet(out_path) if out_path.exists() else None
        return options_enrichment.enrich(enriched, asof=target, previous=previous)
    except Exception:
        logger.warning("options telemetry pass failed; columns left absent", exc_info=True)
        return enriched
```

(If `thematic.py` has no module `logger`, follow its existing error-reporting idiom — check the top of the file; add `logger = logging.getLogger(__name__)` + `import logging` if absent.)

Inside `score()`, after the `market_state.enrich(...)` line and BEFORE `output_dir.mkdir(...)`, insert:

```python
    out_path = output_dir / f"{target.isoformat()}.parquet"
    enriched = _apply_options_telemetry(enriched, target=target, out_path=out_path)
```

and change the existing two lines below from

```python
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{target.isoformat()}.parquet"
```

to

```python
    output_dir.mkdir(parents=True, exist_ok=True)
```

(`out_path` is now computed once, before the telemetry pass reads the previous output.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.options_telemetry.test_cli_wiring -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_cli/commands/thematic.py apps/alphalens-research/tests/thematic/options_telemetry/test_cli_wiring.py
git commit -m "feat(cli): wire options telemetry into thematic score with carry-forward"
```

---

### Task 7: sort-lock guard for the `options_` prefix

**Files:**
- Modify: `apps/alphalens-research/tests/thematic/argumentation/test_sort_and_dedup.py` (the `_forbidden_expert_prefixes` helper, ~line 388)

**Interfaces:**
- Consumes: existing `_forbidden_expert_prefixes()` + `TestSortKeyExpertLock` guards.
- Produces: any future attempt to put an `options_*` column into `_BRIEF_SORT_KEYS` trips the prefix guard.

- [ ] **Step 1: Write the failing assertion first** — extend the helper's hardcoded set:

```python
def _forbidden_expert_prefixes() -> tuple[str, ...]:
    ids = set(expert_ids()) | {"buffett", "oneil"}
    # options_* is display-only telemetry (design memo 2026-07-07) — never
    # a sort input until an Expert-style validation says otherwise.
    return tuple(sorted({f"{eid}_" for eid in ids} | {"expert_", "options_"}))
```

Then add one test to `TestSortKeyExpertLock` proving the guard bites:

```python
    def test_options_prefix_is_forbidden_in_sort(self):
        self.assertIn("options_", _forbidden_expert_prefixes())
```

- [ ] **Step 2: Run the sort-lock module**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.argumentation.test_sort_and_dedup -v`
Expected: PASS (the current sort chain contains no `options_*` key, so the strengthened guard passes; the new test pins the prefix's presence)

- [ ] **Step 3: Commit**

```bash
git add apps/alphalens-research/tests/thematic/argumentation/test_sort_and_dedup.py
git commit -m "test(thematic): forbid options_ prefix in the brief sort chain"
```

---

### Task 8: live probe (opt-in) for the option-chain shape

**Files:**
- Modify: `apps/alphalens-research/tests/live/test_yfinance_live.py` (append one probe following the file's existing `run_probes` / `skipUnless` pattern)

**Interfaces:**
- Consumes: Task 2 client methods; the shared transient/permanent classifier already used in `tests/live/`.
- Produces: `YFINANCE_LIVE_TEST=1` now also shape-checks a real chain (non-emptiness + keys, NEVER values), so a silent Yahoo options-API change surfaces in the weekly `live-probes` CI job instead of corrupting telemetry.

- [ ] **Step 1: Read the existing file's probe idiom first**, then append (adapt names to the file's helpers — the assertion content is what matters):

```python
    def test_option_chain_shape(self):
        # AAPL always has a liquid chain; assert SHAPE only, never values.
        client = get_default_yfinance_client()
        expiries = client.option_expiries("AAPL")
        self.assertIsNotNone(expiries)
        self.assertGreater(len(expiries), 0)
        leg = client.option_chain("AAPL", expiries[0])
        self.assertIsNotNone(leg)
        calls, puts = leg
        for frame in (calls, puts):
            self.assertFalse(frame.empty)
            for col in ("strike", "bid", "ask", "impliedVolatility", "openInterest", "volume"):
                self.assertIn(col, frame.columns)
```

- [ ] **Step 2: Run it live once (opt-in)**

Run: `cd apps/alphalens-research && YFINANCE_LIVE_TEST=1 ../../.venv/bin/python -m unittest tests.live.test_yfinance_live -v`
Expected: PASS (needs network; if Yahoo throttles, the shared classifier treats 429 as transient — rerun)

- [ ] **Step 3: Verify it is SKIPPED without the flag**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.live.test_yfinance_live -v`
Expected: all tests skipped (`skipUnless` gate)

- [ ] **Step 4: Commit**

```bash
git add apps/alphalens-research/tests/live/test_yfinance_live.py
git commit -m "test(live): option-chain shape probe in the yfinance live suite"
```

---

### Task 9: full suite, PR, zen review

**Files:**
- None new. Verification + delivery.

- [ ] **Step 1: Run the full research suite**

Run from the worktree root: `uv run python -m unittest discover -s apps/alphalens-research/tests -t apps/alphalens-research`
Expected: PASS (live probes skip; no Django changes were made so the Django suite is unaffected, but run `just test` if available to confirm)

- [ ] **Step 2: Lint**

Run: `just lint` (or `uv run ruff check . && uv run ruff format --check .`)
Expected: clean

- [ ] **Step 3: Push branch, open PR**

PR body follows the What/Why/How house pattern; include a `## Known issues / behaviour notes` section listing: (1) telemetry values are vendor-grade (Yahoo IV bugs mitigated, not eliminated — audit columns allow recompute); (2) `options_ivp30` deliberately absent (censored pseudo-percentile, spec §4); (3) first-look analysis must condition on `chain_quality=OK` and control earnings-within-30d; (4) VPS deploy is an image rebuild (thematic-build runs the baked-in CLI) — forward-only, operator-owned.

```bash
git push -u origin docs/options-telemetry-design
gh pr create --repo kamilpajak/AlphaLens --title "feat(thematic): options telemetry columns at the score stage" --body-file <(...)
```

- [ ] **Step 4: Zen pre-merge codereview (mandatory)**

Run `mcp__zen__codereview` with `deepseek/deepseek-v4-pro`, `thinking_mode="high"` on the open PR. Apply findings as ADDITIONAL commits (never amend + force-push). Wait for CI green on the latest commit before merge.

- [ ] **Step 5: Post-merge reminders (do NOT bundle into this PR)**

- CLAUDE.md stewardship: the VPS-backfills `alphalens-thematic-build` row + "Runtime data" section mention what the score stage stamps — update in a SEPARATE small doc PR (house rule: CLAUDE.md edits ride their own PR).
- Deploy: VPS-local `alphalens-pipeline:latest` image rebuild activates the new columns on the next thematic-build slot; verify against the persisted parquet (deploy gotcha: check the artifact, not the log).

---

## Self-review record

- **Spec coverage:** §3 source (Task 2), §3.1 window+freeze (Tasks 1, 5), §4 all 16 columns + quality criteria + sanity filter + skew + no-raw-P/C (Tasks 3-5), §5 integration points — score stage (Task 6), sort-lock (Task 7), canonical client (Task 2), Django: no changes needed (verified: ingest drops unknown columns; `test_no_orphan_brief_fields` only checks model fields, none added), §6 ship criterion (Task 9 full suite; zero selection change is structural — the enricher only appends columns). LEGACY_CONTRACT_COLUMNS registration from spec §5 is intentionally NOT done: that registry exists for columns that must reach the Django model; options telemetry's SoT is the parquet, and adding unregistered parquet columns trips no parity test (both directions verified in-session).
- **Placeholder scan:** none; every code step is complete. Two steps direct the implementer to verify an existing idiom before finalizing (grouped-day parquet layout in Task 4, live-probe helper names in Task 8) — with concrete fallbacks stated.
- **Type consistency:** `TickerSnapshot`, `SnapshotFn`, `OPTIONS_COLUMNS`, `stamp_window_utc`, `session_close_utc`, `option_expiries`/`option_chain` names match across Tasks 1-6.
