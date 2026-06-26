# Edge-Job Timeout Resilience — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make the nightly `alphalens-feedback-shadow-returns` edge job never leave `/edge` stale — even when Polygon is flaky — by (1) bounding the run with a wall-clock deadline so it finishes in-window with whatever resolved, and (2) decoupling the edge Postgres mirror so it runs on every terminal outcome, including a timeout-kill.

**Architecture:** Two orthogonal fixes that compose. CODE: a `_RunDeadline` (wall-clock budget + consecutive-Polygon-error breaker) constructed once per run and threaded — exactly like the existing `budget: _FetchBudget` param — through the population replay and the three enrichments; once it trips, new Polygon fetches stop and remaining tickers defer via the EXISTING carry-forward path. SYSTEMD: drop `ExecStartPost` from the compute unit; move the mirror to its own `alphalens-edge-mirror.service` fired by `OnSuccess=`+`OnFailure=` plus a mandatory hourly self-heal timer.

**Tech Stack:** Python 3.13, unittest (injectable `time.monotonic`), systemd user units, the existing `rebuild_ladder_outcomes_cache` Django mgmt command, the existing `alphalens-emit-job-metrics` textfile-gauge script.

**Design source:** the multi-agent workflow synthesis (root cause + solution) summarized below.

## Root cause (verified)
1. **Hang:** `TimeoutStartSec=90min` covers FOUR Polygon-bound phases inside `_refresh_population_ladders` (replay + benchmark_excess + size + chart_payloads). The only guard `_FetchBudget` caps fetch COUNT, not wall-clock. Under a degraded Polygon, one `get_agg_range` burns up to ~170-270s in the 4-attempt retry chain (`_MAX_REQUEST_ATTEMPTS=4`, `_SERVER_ERROR_BACKOFFS=(5,15,30)`, `timeout=30.0`), so a bad night runs past 90 min → SIGTERM.
2. **Stale /edge:** the mirror is `ExecStartPost`, which systemd runs ONLY after `ExecStart` exits 0. A timeout-kill skips it, freezing `/edge` while fresh parquets sit on disk.

## Global Constraints
- Canonical client only: no edits to `polygon_client.py` (don't widen blast radius / change shared retry). The deadline check sits BETWEEN fetches in the consumers.
- No precision downgrade: deferring un-fetchable tickers to the next run via the existing carry-forward path is allowed; silently dropping data and presenting a day as complete is NOT.
- `/edge` must never show a partial day as complete — relies on the existing atomic per-date parquet writes + carry-forward rows (both already-shipped legitimate states).
- Deadline default `75*60` s (under the 90-min backstop), env-overridable via `ALPHALENS_FEEDBACK_FETCH_DEADLINE_S`. Breaker: `6` consecutive real Polygon errors.
- Breaker counts ONLY retry-exhausting `PolygonError`/timeout — never a clean empty/`NO_FILL`/404/implausible carry.
- English-only; TDD (injectable monotonic clock, no real waiting); commit `-s` (DCO).
- systemd `OnSuccess=` needs systemd ≥ 249 — the mandatory hourly mirror timer is the backstop that makes the mirror correct regardless.
- Test invocation: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_population_ladder_monitor -v`

---

### Task 1: `_RunDeadline` guard (pure, TDD)

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/feedback/population_ladder_monitor.py` (add class + constants near the existing `_MAX_FETCHES_PER_RUN` block ~line 99)
- Test: `apps/alphalens-research/tests/test_run_deadline.py`

**Interfaces:**
- Produces: `_RunDeadline(budget_s: float, breaker_fails: int = _BREAKER_CONSECUTIVE_FAILS, monotonic: Callable[[], float] = time.monotonic)` with `.should_stop() -> bool` (latching), `.record_fetch_result(*, ok: bool) -> None`, `.stopped_reason: str | None`; constants `_FETCH_DEADLINE_S_DEFAULT = 75*60`, `_BREAKER_CONSECUTIVE_FAILS = 6`.

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-research/tests/test_run_deadline.py
import unittest
from alphalens_pipeline.feedback.population_ladder_monitor import (
    _RunDeadline, _FETCH_DEADLINE_S_DEFAULT, _BREAKER_CONSECUTIVE_FAILS,
)


class _Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class TestRunDeadline(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(_FETCH_DEADLINE_S_DEFAULT, 75 * 60)
        self.assertEqual(_BREAKER_CONSECUTIVE_FAILS, 6)

    def test_deadline_trips_on_wallclock_and_latches(self):
        c = _Clock()
        d = _RunDeadline(60.0, monotonic=c)
        self.assertFalse(d.should_stop())
        c.t += 59.0
        self.assertFalse(d.should_stop())
        c.t += 2.0  # now 61s past start, deadline 60s
        self.assertTrue(d.should_stop())
        self.assertEqual(d.stopped_reason, "deadline")
        c.t -= 100.0  # latched: stays stopped even if clock rewinds
        self.assertTrue(d.should_stop())

    def test_breaker_trips_after_consecutive_fails_only(self):
        c = _Clock()
        d = _RunDeadline(10_000.0, breaker_fails=3, monotonic=c)
        d.record_fetch_result(ok=False)
        d.record_fetch_result(ok=False)
        self.assertFalse(d.should_stop())
        d.record_fetch_result(ok=True)   # resets the streak
        d.record_fetch_result(ok=False)
        d.record_fetch_result(ok=False)
        self.assertFalse(d.should_stop())
        d.record_fetch_result(ok=False)  # 3 in a row
        self.assertTrue(d.should_stop())
        self.assertEqual(d.stopped_reason, "breaker")

    def test_healthy_run_never_stops(self):
        c = _Clock()
        d = _RunDeadline(10_000.0, monotonic=c)
        for _ in range(100):
            d.record_fetch_result(ok=True)
            self.assertFalse(d.should_stop())
        self.assertIsNone(d.stopped_reason)
```

- [ ] **Step 2: Run RED**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_run_deadline -v`
Expected: FAIL — `ImportError: cannot import name '_RunDeadline'`

- [ ] **Step 3: Implement**

Add near the `_MAX_FETCHES_PER_RUN` constant block (~line 99). Ensure `import time` and `from collections.abc import Callable` (or `from typing import Callable`) are present at the top — add if missing.

```python
_FETCH_DEADLINE_S_DEFAULT = 75 * 60  # wall-clock budget, under TimeoutStartSec=90min
_BREAKER_CONSECUTIVE_FAILS = 6       # consecutive real Polygon errors before fast-bail


class _RunDeadline:
    """Per-run wall-clock budget + consecutive-Polygon-error breaker.

    ``should_stop()`` latches: once the wall-clock budget is spent OR
    ``breaker_fails`` real Polygon errors arrive back-to-back, every later call
    returns True so the run stops issuing NEW fetches and defers the rest via
    the existing carry-forward path. ``record_fetch_result(ok=False)`` is fed
    ONLY on retry-exhausting PolygonError/timeout — never on a clean empty /
    NO_FILL / implausible carry.
    """

    def __init__(
        self,
        budget_s: float,
        breaker_fails: int = _BREAKER_CONSECUTIVE_FAILS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mono = monotonic
        self._deadline = monotonic() + budget_s
        self._consecutive_fails = 0
        self._breaker_fails = breaker_fails
        self.stopped_reason: str | None = None

    def should_stop(self) -> bool:
        if self.stopped_reason is not None:
            return True
        if self._mono() >= self._deadline:
            self.stopped_reason = "deadline"
        elif self._consecutive_fails >= self._breaker_fails:
            self.stopped_reason = "breaker"
        return self.stopped_reason is not None

    def record_fetch_result(self, *, ok: bool) -> None:
        self._consecutive_fails = 0 if ok else self._consecutive_fails + 1
```

- [ ] **Step 4: Run GREEN** — `... tests.test_run_deadline -v` → PASS (4 tests)
- [ ] **Step 5: Commit** — `git add -A && git commit -s -m "feat(feedback): _RunDeadline wall-clock + breaker guard for the monitor run"`

---

### Task 2: Thread the deadline through the population replay (TDD)

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/feedback/population_ladder_monitor.py`
- Test: `apps/alphalens-research/tests/test_population_ladder_monitor.py`

**Interfaces:**
- Consumes: `_RunDeadline` (Task 1).
- Produces: a new keyword `deadline: _RunDeadline | None = None` on `replay_population_ladders`, `_replay_one_date`, `_resolve_queue`, and `_replay_candidate` — threaded EXACTLY like the existing `budget`/`forced_budget` params. New field `stopped_for_deadline: int = 0` on `PopulationMonitorReport` (count of items deferred because the deadline tripped).

- [ ] **Step 1: Write the failing test**

Add to `apps/alphalens-research/tests/test_population_ladder_monitor.py` (uses the existing `_MonitorTestBase`, `_write_brief`, `_OK_SETUP`). The stub fetch raises `PolygonError` to drive the breaker; an already-tripped deadline forces carry-forward.

```python
class TestRunDeadlineIntegration(_MonitorTestBase):
    def test_tripped_deadline_carries_without_fetching(self):
        import datetime as dt
        from alphalens_pipeline.feedback.population_ladder_monitor import _RunDeadline
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])
        fetched = []
        def _fetch(t, s, e):
            fetched.append(t)
            base = int(s.timestamp() * 1000)
            return [{"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1e3}]
        # deadline already past at construction (budget -1s) -> should_stop() True immediately
        dead = _RunDeadline(-1.0, monotonic=lambda: 0.0)
        reports = replay_population_ladders(
            self.briefs_dir, end_date=now.date(), store_dir=self.store_dir,
            bar_fetch=_fetch, now=now, deadline=dead,
        )
        self.assertEqual(fetched, [])  # no Polygon fetch issued once stopped
        self.assertTrue(any(r.stopped_for_deadline >= 1 for r in reports))
        # the row is still written (carried/placeholder), not missing
        df = self._read_store(brief_date)
        self.assertIn("NVDA", set(df["ticker"]))

    def test_no_deadline_resolves_normally(self):
        import datetime as dt
        brief_date = dt.date(2026, 5, 1)
        now = dt.datetime(2026, 7, 8, 7, 0, tzinfo=UTC)
        _write_brief(self.briefs_dir, brief_date, [{"ticker": "NVDA", "setup": _OK_SETUP}])
        def _fetch(t, s, e):
            base = int(s.timestamp() * 1000)
            return [{"t": base, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1e3}]
        reports = replay_population_ladders(
            self.briefs_dir, end_date=now.date(), store_dir=self.store_dir,
            bar_fetch=_fetch, now=now,  # deadline defaults to None -> never stops
        )
        self.assertTrue(all(r.stopped_for_deadline == 0 for r in reports))
```

- [ ] **Step 2: Run RED** → FAIL (`replay_population_ladders` has no `deadline` kw / report has no `stopped_for_deadline`).

- [ ] **Step 3: Implement** (4 precise edits)

(3a) `PopulationMonitorReport` (dataclass ~line 204): add field after `oldest_deferred_touch_age`:
```python
    stopped_for_deadline: int = 0  # items deferred because the run deadline tripped
```

(3b) Add `deadline: _RunDeadline | None = None` to the signatures of `replay_population_ladders` (~line 1012), `_replay_one_date` (~line 1092), and `_resolve_queue` (~line 1880) — placed alongside the existing `budget` kw — and pass it through at every call site exactly where `budget=`/`forced_budget=` are passed (`replay_population_ladders` → `_replay_one_date` → `_resolve_queue`). Mirror the existing budget threading 1:1.

(3c) In `_resolve_queue`, gate the loop BEFORE `_replay_candidate` (the `for item in ordered:` loop ~line 1904). Insert at the top of the loop body, reusing the SAME carry-forward branch already used for `result is None`:
```python
    for item in ordered:
        ticker = item.candidate.ticker.upper()
        theme = item.candidate.theme or None
        scorer_version = item.candidate.scorer_config_version or None
        if deadline is not None and deadline.should_stop():
            rows_by_ticker[ticker] = _stamp_scorer_version(
                _stamp_theme(_carried_row(item), theme), scorer_version
            )
            counts["carried"] += 1
            counts["stopped_for_deadline"] = counts.get("stopped_for_deadline", 0) + 1
            age = _deferred_age(item, last_closed_session)
            if age is not None:
                deferred_ages.append(age)
            continue
        use_budget = forced_budget if item.forced else budget
        # ... existing _replay_candidate(...) call unchanged ...
```
Wire `counts["stopped_for_deadline"]` into the `PopulationMonitorReport(...)` construction in `_replay_one_date` (set `stopped_for_deadline=counts.get("stopped_for_deadline", 0)`), and ensure the `counts` dict is initialised with that key alongside `carried`/`terminal`/`ongoing` wherever it is created.

(3d) Add `deadline: _RunDeadline | None = None` to `_replay_candidate` and feed the breaker where the fetch outcome is already known (the try/except ~lines 2002-2025): on the `except (PolygonError, ...)` branch add `if deadline is not None: deadline.record_fetch_result(ok=False)` BEFORE `return None`; immediately after a successful `bars = _extend_bar_cache(...)` (bars present) add `if deadline is not None: deadline.record_fetch_result(ok=True)`. Do NOT record on the no-bars / implausible carries (those are clean, not Polygon errors). Pass `deadline` into `_replay_candidate` from the `_resolve_queue` call.

- [ ] **Step 4: Run GREEN** — `... tests.test_population_ladder_monitor.TestRunDeadlineIntegration -v` → PASS; then the WHOLE module to confirm no regression: `... tests.test_population_ladder_monitor -v`.

- [ ] **Step 5: Commit** — `git add -A && git commit -s -m "feat(feedback): thread run deadline through population replay (defer on trip)"`

---

### Task 3: Bound the three enrichments with the same deadline (TDD)

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/feedback/benchmark_excess.py`, `population_ladder_monitor.py` (`enrich_store_with_size_fields`), `ladder_chart.py` (`enrich_store_with_chart_payloads`), and `apps/alphalens-pipeline/alphalens_cli/commands/feedback.py` (`_refresh_population_ladders`)
- Test: `apps/alphalens-research/tests/test_benchmark_excess.py` (or the existing benchmark-excess test module — locate with `grep -rl enrich_store_with_benchmark_excess apps/alphalens-research/tests`)

**Interfaces:**
- Consumes: `_RunDeadline` (Task 1).
- Produces: `deadline: _RunDeadline | None = None` keyword on `enrich_store_with_benchmark_excess`, `enrich_store_with_size_fields`, `enrich_store_with_chart_payloads`. `_refresh_population_ladders` constructs ONE `_RunDeadline` and passes the SAME instance to the replay (Task 2) and all three enrichments.

- [ ] **Step 1: Write the failing test** (benchmark_excess, representative of all three)

```python
# add to the benchmark-excess test module
def test_enrich_stops_fetching_when_deadline_tripped(self):
    # Build a store parquet with >=2 rows needing a benchmark fetch, a tripped
    # deadline, and a fetch stub that records calls. Assert: no fetch issued,
    # function returns without error, rows left unenriched (None bench/excess)
    # rather than crashing.
    from alphalens_pipeline.feedback.population_ladder_monitor import _RunDeadline
    dead = _RunDeadline(-1.0, monotonic=lambda: 0.0)
    calls = []
    def _fetch(t, s, e):
        calls.append(t); return []
    n = enrich_store_with_benchmark_excess(self.store_dir, fetch=_fetch, deadline=dead)
    self.assertEqual(calls, [])
```
(Match the module's existing setUp/store-construction helpers; if the existing test injects `fetch=`, reuse that seam — benchmark_excess already takes a `fetch` param per the per-row loop at line 243/297.)

- [ ] **Step 2: Run RED** → FAIL (no `deadline` kw).

- [ ] **Step 3: Implement**

In each enrichment's per-row loop, add a deadline check that breaks cleanly (writing what is done so far), leaving remaining rows for the next run:
- `benchmark_excess.py` ~line 243 `for _, row in df.iterrows():` → first line of the loop body:
```python
        if deadline is not None and deadline.should_stop():
            break
```
  Add `deadline: _RunDeadline | None = None` to `enrich_store_with_benchmark_excess`'s signature (import `_RunDeadline` from `population_ladder_monitor`). Rows past the break keep their existing (None) columns — the function already appends per row, so persist only the processed rows OR keep the existing append semantics and leave unprocessed rows untouched in the store (mirror how the function currently writes; do NOT mark unprocessed rows as "done").
- `ladder_chart.py` `enrich_store_with_chart_payloads` per-row loop ~line 650 → same `if deadline ... break` at the top.
- `population_ladder_monitor.py` `enrich_store_with_size_fields` → same guard in its per-row loop.

In `feedback.py` `_refresh_population_ladders` (~lines 64-93): construct once and pass through:
```python
    import os
    from alphalens_pipeline.feedback.population_ladder_monitor import (
        MONITOR_LOOKBACK_DAYS, replay_population_ladders, _RunDeadline, _FETCH_DEADLINE_S_DEFAULT,
    )
    deadline = _RunDeadline(
        float(os.environ.get("ALPHALENS_FEEDBACK_FETCH_DEADLINE_S", _FETCH_DEADLINE_S_DEFAULT))
    )
    reports = replay_population_ladders(briefs_dir, lookback_days=MONITOR_LOOKBACK_DAYS, deadline=deadline)
    ...
    if deadline.stopped_reason:
        logger.warning(
            "population-monitor: stopped fetching early (%s); remaining work deferred to next run.",
            deadline.stopped_reason,
        )
```
Then pass `deadline=deadline` into `_enrich_population_benchmark_excess`, `_enrich_population_size_fields`, `_enrich_population_chart_payloads` (add the param to those wrappers and forward to the underlying enrich functions).

- [ ] **Step 4: Run GREEN** — the new enrichment test + the full feedback/monitor suites:
`cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_population_ladder_monitor tests.test_benchmark_excess -v` (substitute the real benchmark-excess test module name).

- [ ] **Step 5: Commit** — `git add -A && git commit -s -m "feat(feedback): bound enrichments by the shared run deadline; construct once in refresh"`

---

### Task 4: Decouple the edge mirror into its own unit + self-heal timer

**Files:**
- Modify: `deploy/systemd/alphalens-feedback-shadow-returns.service`
- Create: `deploy/systemd/alphalens-edge-mirror.service`, `deploy/systemd/alphalens-edge-mirror.timer`
- Modify: `deploy/systemd/README.md`
- Test: none (systemd config) — verified by file content + the deploy runbook (Task 6); a `systemd-analyze verify` is optional and may be unavailable for user units in CI.

- [ ] **Step 1: Edit the compute unit** — in `alphalens-feedback-shadow-returns.service`:
  - REMOVE the `ExecStartPost=/usr/bin/docker compose ... rebuild-ladder-outcomes` block (lines ~59-61).
  - KEEP `TimeoutStartSec=90min` (now a backstop) and the existing `ExecStopPost=...alphalens-emit-job-metrics feedback-shadow-returns`.
  - ADD to the `[Unit]` section:
```ini
OnSuccess=alphalens-edge-mirror.service
OnFailure=alphalens-edge-mirror.service
```
  - Add a comment noting the mirror moved to `alphalens-edge-mirror.service` (fired on success OR failure/timeout) so a timeout-kill no longer skips it.

- [ ] **Step 2: Create `deploy/systemd/alphalens-edge-mirror.service`**
```ini
[Unit]
Description=AlphaLens edge mirror — rebuild ladder-outcome Postgres cache from population-ladder parquets
After=network-online.target docker.service
Wants=network-online.target
# Triggered by alphalens-feedback-shadow-returns.service (OnSuccess=/OnFailure=)
# AND by alphalens-edge-mirror.timer (mandatory hourly self-heal — the backstop
# that keeps /edge fresh even if the OnSuccess= handoff is unavailable on this
# systemd version, or a split deploy leaves the handoff target missing).

[Service]
Type=oneshot
TimeoutStartSec=20min
# Tame any re-trigger storm (handoff + timer firing close together).
StartLimitIntervalSec=300
StartLimitBurst=3
# The mgmt command is mtime-gated + idempotent, so redundant runs are ~free.
ExecStart=/usr/bin/docker compose \
    -f %h/AlphaLens/deploy/docker/django-prod/docker-compose.yaml \
    --profile maintenance run --rm rebuild-ladder-outcomes
ExecStopPost=%h/AlphaLens/deploy/systemd/bin/alphalens-emit-job-metrics edge-mirror

[Install]
WantedBy=default.target
```

- [ ] **Step 3: Create `deploy/systemd/alphalens-edge-mirror.timer`**
```ini
[Unit]
Description=Hourly self-heal for the AlphaLens edge mirror (keeps /edge fresh independent of the compute job)

[Timer]
OnCalendar=*-*-* *:05:00 UTC
Persistent=true
Unit=alphalens-edge-mirror.service

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Document** in `deploy/systemd/README.md`: a new subsection "Edge mirror (decoupled)" stating (a) the mirror is its own unit fired on every terminal outcome of the compute job PLUS an hourly timer; (b) **DEPLOY ATOMICITY:** the compute-unit edit and both new unit files must land together with a single `systemctl --user daemon-reload`, then `systemctl --user enable --now alphalens-edge-mirror.timer` — deploying the compute edit alone removes the old `ExecStartPost` safety net and leaves the handoff target missing; (c) `OnSuccess=` needs systemd ≥ 249 (`systemctl --version`) — if older, the hourly timer is the backstop; (d) the existing CLAUDE.md VPS-backfills note for this job should be updated (own follow-up doc PR).

- [ ] **Step 5: Commit** — `git add -A && git commit -s -m "feat(systemd): decouple edge mirror into its own unit + hourly self-heal timer"`

---

### Task 5: Edge-staleness alert on the decoupled mirror

**Files:**
- Modify: the Prometheus alert-rules file that defines `AlphalensJobStale` (locate: `grep -rl AlphalensJobStale deploy/`)
- Modify: `deploy/systemd/README.md` (note the new alert)
- Test: none (alerting config) — validated by `promtool check rules` if available; otherwise by inspection.

- [ ] **Step 1: Add the alert** beside `AlphalensJobStale`, watching the decoupled mirror's last success (emitted by Task 4 Step 2's `ExecStopPost ... edge-mirror`, which writes `alphalens_job_last_success_timestamp_seconds{job="edge-mirror"}` via the existing metric script):
```yaml
- alert: AlphalensEdgeStale
  expr: time() - max(alphalens_job_last_success_timestamp_seconds{job="edge-mirror"}) > 36 * 3600
  for: 15m
  labels: { severity: warning }
  annotations:
    summary: "/edge Postgres mirror has not refreshed in >36h"
    description: "alphalens-edge-mirror has not succeeded for {{ $value | humanizeDuration }} — /edge is stale regardless of whether the compute job 'succeeded'."
```
(Match the surrounding rules' exact indentation/label conventions in that file.)

- [ ] **Step 2: Verify the job name matches** — confirm `alphalens-emit-job-metrics` emits `job="edge-mirror"` when invoked as `alphalens-emit-job-metrics edge-mirror` (it labels by its `$1` JOB arg — Task 4 Step 2 passes `edge-mirror`). No code change expected; if the script derives the gauge filename from the job arg, confirm no collision with the compute job's `.prom` file (distinct job name → distinct series; if it shares one textfile, give the mirror its own `.prom`).

- [ ] **Step 3: Commit** — `git add -A && git commit -s -m "feat(monitoring): AlphalensEdgeStale alert on the decoupled edge mirror"`

---

### Task 6: Full suite + guards + deploy runbook

**Files:** `deploy/systemd/README.md` (runbook only).

- [ ] **Step 1: Full relevant suites**
`cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_run_deadline tests.test_population_ladder_monitor -v` and the benchmark-excess + any ladder_chart/size test modules. Expected: all PASS.

- [ ] **Step 2: Doctrine guards**
`cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_no_raw_polygon_http tests.test_module_dependencies tests.test_no_polish_chars -v`. Expected: PASS (no edits to `polygon_client.py`; no new cross-package imports; English-only).

- [ ] **Step 3: Broader feedback regression** — discover the feedback test set:
`cd apps/alphalens-research && ../../.venv/bin/python -m unittest discover -s tests -t . -p 'test_*ladder*.py' -v` plus `-p 'test_*feedback*.py'`. Expected: OK.

- [ ] **Step 4: Write the deploy runbook** into `deploy/systemd/README.md` (one ordered block):
  1. Pipeline code change ships in the HOST VENV (this job runs `%h/AlphaLens/.venv/bin/alphalens` directly, NOT the Docker image) → on the VPS: `cd ~/AlphaLens && git pull --ff-only origin main` (the venv is editable, so the code is live).
  2. `systemctl --version` → confirm ≥ 249 (else rely on the hourly timer).
  3. Copy all three unit files to `~/.config/systemd/user/`, `systemctl --user daemon-reload`, `systemctl --user enable --now alphalens-edge-mirror.timer`.
  4. Verify: `systemctl --user start alphalens-feedback-shadow-returns.service` (or wait for 06:30 UTC) → confirm on a clean run that `alphalens-edge-mirror.service` fired (`journalctl --user -u alphalens-edge-mirror.service`), `/edge` advanced, and `alphalens_job_last_success_timestamp_seconds{job="edge-mirror"}` updated. Then simulate the failure path once (e.g. set `ALPHALENS_FEEDBACK_FETCH_DEADLINE_S=1` for one manual run) → confirm the compute "fails"/stops early BUT the mirror still fires and `/edge` reflects the carried store.

---

## Self-Review notes
- **Root-cause coverage:** hang → Tasks 1-3 (deadline across replay + 3 enrichments, constructed once); stale-/edge → Task 4 (decoupled mirror on success/failure + hourly timer); missing signal → Task 5 (edge-staleness alert). Deploy atomicity + systemd-version risk → Task 4 doc + Task 6 runbook.
- **Type consistency:** `deadline: _RunDeadline | None` threads identically to the existing `budget`/`forced_budget` params through `replay_population_ladders → _replay_one_date → _resolve_queue → _replay_candidate`; the same instance flows to the three enrichments; `record_fetch_result(ok=...)` fed only at the PolygonError/success site; `stopped_for_deadline` added to `PopulationMonitorReport`.
- **Doctrine:** no `polygon_client.py` edit; carry-forward (existing `_carried_row`) reused — no new persistence/precision change; `/edge` never shows a torn day (atomic per-date writes).
- **Known follow-up (out of scope):** a PolygonClient-level adaptive cross-ticker breaker for sustained multi-day outages; a small CLAUDE.md VPS-backfills update (own doc PR).
