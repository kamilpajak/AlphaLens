# Trailing SL exit policy — bot-amend first cut (build-seq step 3, "Tor A")

**Status:** DRAFT — operator-approved 2026-08-09 (brainstorm). Scope = the buildable,
offline-testable half of build-seq step 3 from
[`trailing_execution_design_2026_08_07.md`](trailing_execution_design_2026_08_07.md) §10.3.
Native `TrailingStopIfTraded` execution is explicitly OUT of this cut and is gated on the
2026-08-10 15:30 attended LIVE probe ("Tor B" below).

This memo is grounded in the CURRENT code (paths + line numbers verified 2026-08-09), not in the
base memo's prose. Where the base memo and the code disagree, the code wins and it is called out.

---

## 1. Goal

Ship a **trailing stop-loss** — a stop that ratchets UP as price makes new highs and never moves
down — as one more named entry in the existing `ExitPolicy` registry. It must be:

- **flag-gated + default OFF** (a new policy name; existing policies byte-identical),
- **fully testable offline** (hermetic unit tests; no market, no broker, no network — today is a
  weekend, no SIM/LIVE market is open, so no probe of any kind is possible today),
- **measured from day one** (each trailing decision stamps execution-quality telemetry, reusing the
  `_fire_telemetry` / `reconcile-fills` machinery shipped in PR #1005-#1007),
- **ML-ready** (the trailing math is a PURE function; a future non-deterministic / ML policy plugs
  into the same registry seam with the same inputs — per
  [`nondeterministic_exit_policy` design](nondeterministic_exit_policy_plan_2026_08_03.md)).

Non-goal for this cut: server-side native trailing, the `+0.5R` break-even JUMP via cancel+replace,
and the amend-route reposition. Those depend on Saxo mechanics that are LIVE-only-unverified and are
decided by the Tor B probe (§9).

## 2. Why bot-amend first (not native)

The base memo's preferred long-term design is a **native hybrid**: the bot stages the break-even
jump (cancel+replace, measured ~576 ms), then a native `TrailingStopIfTraded` trails server-side and
survives bot-down. But two of its core mechanics are **unverified until a LIVE session with real
ticks**:

- native server-side **ratchet** (SIM price is static — could not be induced, base memo §8b),
- whether a narrow **amend repositions the stop level** on a live tick (SIM: amend accepted 200 but
  the level did not move — base memo §8b.1 N1).

Building the native hybrid today means shipping code whose core cannot be tested until tomorrow.

The **bot-amend** cut sidesteps all of that: the bot keeps a plain `StopIfTraded` and simply
`AmendStop`s its LEVEL up as the peak rises. This reuses the exact mechanism the one-shot reanchor
arm already emits (`AmendStop`), needs **no order-type switch**, and therefore has **no
cancel+replace naked window** (the ~576 ms window applied only to switching `StopIfTraded` →
`TrailingStopIfTraded`). Trade-off accepted: a bot-amend stop is **frozen while the bot is down**
(native would keep trailing). That robustness gap is exactly what a later native increment buys,
once Tor B confirms the native mechanics pay for themselves.

## 3. Current code — what this builds on (verified 2026-08-09)

- **`broker_contract/exit_geometry/policy.py`** — `ExitPolicy` is a `@runtime_checkable` Protocol
  and a **pure price oracle**: `decide_placement_geometry(blended, atr, *, ceiling_price)` and
  `decide_reanchor(avg_price, atr)`. It NEVER emits an Action. `SetupStaticPolicy` (inert null) and
  `AtrBracketPolicy` (wraps a numeric `ExitGeometryPolicy`) are the two shipped implementations.
  `min_stop_distance_frac` is a policy attribute so the safety envelope stays policy-agnostic.
  This cut adds ONE more capability attribute, `trails: bool` (default `False`), so the daemon
  selects the trailing arm by an explicit flag, not by sniffing a return value (§4.2).
- **`broker_contract/exit_geometry/levels.py::clamp_reanchor_target`** — the economic envelope:
  returns `None` (do not reanchor) on any degenerate input OR when the target would drop below
  `prior_stop`. It enforces **never-below-brief-floor** (`prior_stop = plan.stop_price`), NOT
  never-loosen-vs-the-live-resting-stop (`OrderState` carries no stop price). It also pushes a
  too-close proposal farther from `anchor_price` via `min_distance_frac`.
- **`position_manager.py:626 _maybe_reanchor(...) -> AmendStop | None`** — the shipped reanchor arm.
  It is **one-shot per blend**: it moves the resting stop to `avg_price - k·ATR` ONCE when a fill
  completes, latched by `view.reanchored_by_uic` (per-uic, `_REANCHOR_AVG_PRICE_EPS` tolerance).
  It is gated on `plan.reanchor is not None`, a finite `avg_price > 0`, a finite `atr > 0`, a sole
  standalone stop, and the amend-backoff set, then runs `policy.decide_reanchor(avg_price, atr)` →
  `clamp_reanchor_target(plan.stop_price, proposed, ...)` → `AmendStop(...)`. It anchors off
  `avg_price` (entry blend), NOT a live peak — so it is NOT trailing.
- **`position_manager.py:599 reconcile_protection(view: ProtectionView)`** — a **pure** function over
  a snapshot; `_maybe_reanchor` runs inside it. Any new per-tick input (peak, last price) must be
  pre-computed by the impure daemon layer and placed INTO the `ProtectionView` snapshot before this
  pure call.
- **`control_loop.py` — the daemon tick has TWO passes.** `_run_live_exits_pass` (control_loop.py
  ~553-598) already **holds a price feed**: it builds `managed` exits and calls
  `run_live_exits(deps.broker, feed, managed)` where `feed = live_exits_feed_factory(...)` (injectable,
  control_loop.py:224; default `_default_live_exits_feed_factory` → `YfinancePriceFeed`,
  control_loop.py:543-550). The **protection/reanchor pass** (`reconcile_protection` /
  `_maybe_reanchor`) has NO feed today. So the live price a trailing stop needs already flows into the
  daemon — in the OTHER pass.
- **`live_exit_engine.py:155 run_live_exits(broker, feed, managed)`** reads `point = feed.latest(uic)`
  (`broker_contract.price_feed.PriceFeed`, `latest() -> PricePoint | None`; `None` = stream-health
  veto).
- **Single-resolution invariant (PR #972):** the policy is resolved ONCE at startup and cached on
  `LoopDeps` / `ProtectionView`; hot paths never resolve (enforced by `test_no_exit_policy_sentinel.py`).
  The cached policy instance is therefore **shared and must stay stateless** — it cannot hold a
  per-position peak.

## 4. Architecture — the trailing arm

### 4.1 Peak & price ownership (decision (a), operator-approved)

The high-water **peak** is per-position and time-varying, so it CANNOT live in the shared, stateless
policy instance. It lives in the **impure daemon layer** (alongside owned qty / avg price — the other
per-position execution state). Concretely:

- A new per-uic peak store owned by the daemon (`LoopDeps`), e.g. `PeakTracker` mapping
  `uic -> peak_price`. On each tick, for every trailing-managed position, the daemon reads
  `feed.latest(uic)` and updates `peak = max(peak, last_price)` (monotone; a `None` feed point does
  NOT lower the peak and vetoes a trailing move that tick).
- The daemon writes `peak` + `last_price` (+ a `feed_ok` flag) per uic INTO the `ProtectionView`
  snapshot before the pure `reconcile_protection` call.
- The policy stays a pure calculator that receives the peak as an argument (§4.3).

Feed for this cut = `YfinancePriceFeed` (the daemon default; ~1 min lag, acceptable because trailing
uses COARSE steps larger than the 1-min noise band). A Saxo real-time feed slots in later behind the
same `PriceFeed` Protocol with no change to this layer.

Reuse note: the price read already happens in `_run_live_exits_pass`. To avoid two feed fetches per
tick, the peak update should reuse the same per-tick price the live-exits pass fetches (fetch once,
feed both the TP-fire path and the peak tracker). Exact wiring is a plan detail; the invariant is
**one feed fetch per uic per tick**.

### 4.2 The trailing arm in the protection pass

A NEW arm (call it `_maybe_trail`), sibling to `_maybe_reanchor`, inside the pure
`reconcile_protection`. Unlike `_maybe_reanchor` it is **repeatable** (fires whenever the ratchet
lets the stop rise a full step), not one-shot-per-blend. It:

1. runs only when `policy.trails` is `True` (the explicit capability flag from §3; the inert/static
   and plain-ATR policies have `trails=False`, so the arm stays dark for them),
2. requires a sole standalone stop, a finite `avg_price > 0`, a finite `atr > 0`, `feed_ok` true and a
   finite `peak > 0`, and the uic not in the amend-backoff set (same guards as `_maybe_reanchor`),
3. computes `proposed = policy.decide_reanchor(avg_price, atr, peak=peak, last_price=last_price)`,
4. applies the **ratchet** (§4.4) and `clamp_reanchor_target(plan.stop_price, ...)`,
5. emits the SAME `AmendStop` action (level up) with `reason="trail"`.

Arm selection is by the `trails` flag, so the two arms never both fire for one position:
`_maybe_trail` runs iff `policy.trails`; `_maybe_reanchor` runs iff `not policy.trails`. A position is
governed by exactly one policy, so only one arm is ever active for it — no interaction. (Sniffing
`decide_reanchor`'s return value would NOT distinguish them: `AtrBracketPolicy` also returns a
non-None avg-based target, so the explicit flag is required.)

### 4.3 Policy signature widening (the pure calculator; ML seam)

Widen the oracle:

```
decide_reanchor(self, avg_price: float, atr: float, *, peak: float | None = None,
                last_price: float | None = None) -> float | None
```

- `SetupStaticPolicy` — unchanged, returns `None` (ignores the new kwargs); `trails=False`.
- `AtrBracketPolicy` — unchanged behavior: ignores `peak`/`last_price`, still returns
  `avg_price - stop_atr_mult·atr` (the one-shot reanchor); `trails=False`. **Byte-identical** for
  `atr_bracket_1p5`.
- **NEW `TrailingAtrPolicy`** (registry name e.g. `trailing_atr`; `trails=True`):
  - **activation gate** — returns `None` until the position is in enough profit
    (`peak >= avg_price + activation·<unit>`; the unit and threshold are `[in_sample]` params, e.g.
    `+0.5R` or `≥1 ATR`). Before activation the static disaster stop governs and the arm is dark.
  - **Chandelier target** — once armed: `target = peak - k·atr` (`k` is the give-back knob,
    `[in_sample]`). The peak monotonicity is enforced in the daemon (§4.1); the policy is pure given
    the peak.
  - Returns `None` on any degenerate input (non-finite / `<= 0` peak/atr/avg_price), never a bad stop.

The default kwargs keep every existing `decide_reanchor` call-site compiling; the widened signature is
purely additive.

A future ML policy is one more registry entry with the SAME signature (it may additionally read
features); the ratchet + clamp below apply to it unchanged, so the stochastic policy is bounded by the
same safety envelope.

### 4.4 Safety — the stop moves UP only

Two independent guards, both required:

- **Ratchet (new, bot-side):** a new per-uic `trailed_stop_by_uic` state records the last level we
  successfully amended to. `_maybe_trail` never proposes a target below that (a fresh proposal that is
  not at least one COARSE STEP above the last trailed level is dropped — no amend). This gives
  monotone-up vs the live trailing history, which the shipped `clamp_reanchor_target` does NOT provide
  (it only compares to the brief floor, because `OrderState` has no stop price).
- **Never-below-brief-floor (shipped):** the proposal still passes through
  `clamp_reanchor_target(plan.stop_price, ...)`. A trailing target below the brief disaster floor
  returns `None` (keep the resting stop). This bounds the stochastic/ML case too.

**Restart safety.** The daemon's in-memory peak (and `trailed_stop_by_uic`) are lost on restart.
Conservative recovery: on the first tick after restart, seed `peak = last_price` (do NOT invent a
higher past peak) and seed `trailed_stop_by_uic` from the live resting stop level read back from the
broker where available, else leave it unseeded and let the ratchet re-establish from the first
post-restart proposal. Because the ratchet forbids moving the stop down, a reset peak can only ever
FAIL TO RAISE the stop temporarily (until price re-tags the high) — it can NEVER loosen protection.
The resting broker stop is untouched by a restart, so the position stays protected throughout.

### 4.5 Flag + telemetry

- New registry name `trailing_atr`, default OFF (activated only by setting
  `ALPHALENS_BROKER_EXIT_POLICY=trailing_atr` in the daemon env, same mechanism as `atr_bracket_1p5`).
  Existing policies unchanged; `test_no_exit_policy_sentinel.py` still passes (no hot-path resolve).
- Each `_maybe_trail` decision stamps execution-quality telemetry (decision price = the trailing
  target, plus peak / last_price / k / activation-state), reusing the `_fire_telemetry` shape and the
  `reconcile-fills` reconciler (#1005-#1007). This feeds the eventual `/edge` fixed-vs-trailing lens
  (base memo §6; A/B primary + path-replay sanity — NOT a per-trade counterfactual).

## 5. Components (files touched)

- `broker_contract/exit_geometry/policy.py` — widen `decide_reanchor` signature (additive kwargs on
  the Protocol + all three impls); add the `trails: bool` capability attribute (default `False` on
  the Protocol, `False` on the two existing impls); add `TrailingAtrPolicy` (`trails=True`).
- `broker_contract/exit_geometry/registry.py` / `resolve_exit_policy` — register `trailing_atr`.
- `broker_contract/exit_geometry/levels.py` — no change to `clamp_reanchor_target`; possibly a new
  pure `chandelier_target(peak, atr, k)` helper for testability.
- `automanager/position_manager.py` — new `_maybe_trail` arm + its `ProtectionView` inputs
  (`peak_by_uic`, `last_price_by_uic`, `feed_ok_by_uic`, `trailed_stop_by_uic`).
- `automanager/control_loop.py` — `PeakTracker` on `LoopDeps`; per-tick peak update from the (single)
  feed fetch; populate the new `ProtectionView` fields; persist/restore `trailed_stop_by_uic` and the
  peak-reset-on-restart behavior.
- Tests (hermetic): `test_trailing_atr_policy.py`, extend `position_manager` tests with trailing-arm
  cases, extend control-loop tests for peak tracking + restart.

## 6. Test plan (hermetic — the whole cut ships offline today)

- **policy:** activation gate (below threshold → None); Chandelier target math; degenerate inputs →
  None; `AtrBracket` / `static` return values byte-identical with the new kwargs present.
- **ratchet:** proposals below the last trailed level are dropped; only a proposal ≥ one coarse step
  above raises the stop; never emits a down-move.
- **clamp:** a trailing target below the brief floor → None (keep resting stop).
- **repeatable fire:** a rising peak across ticks produces multiple `AmendStop`s (each a step up); a
  flat/falling peak produces none.
- **peak tracking:** `peak = max(peak, last_price)`; a `None` feed point does not lower the peak and
  vetoes the trailing move that tick.
- **restart:** peak reset to `last_price`; ratchet prevents any loosening; resting stop unchanged.
- **guards:** no sole standalone stop / amend-backoff / non-finite inputs → no action.

Known gap (stated, not silently skipped): the tests prove the DECISION logic and the AmendStop
emission. They CANNOT prove Saxo accepts an upward level amend of a plain `StopIfTraded` while it
stays below market — that is §8, resolved by the Tor B SIM probe tomorrow.

## 7. Build sequence (each task TDD; PR with mandatory zen `deepseek-v4-pro` high pre-merge)

1. `TrailingAtrPolicy` + widened Protocol/impl signatures + `chandelier_target` helper + registry
   entry (leaf, pure — cheapest tier).
2. `PeakTracker` + per-tick peak update wired to the single feed fetch (daemon).
3. `_maybe_trail` arm + `ProtectionView` inputs + ratchet + clamp reuse.
4. Restart safety (peak reset + `trailed_stop_by_uic` restore) + telemetry stamp.
5. Flag wiring + end-to-end hermetic acceptance (rising-peak path fires stepped amends; static/ATR
   unchanged).

## 8. The one open execution mechanic for this cut → Tor B SIM probe (2026-08-10 15:30)

Does Saxo accept `AmendStop` of a plain `StopIfTraded` **LEVEL upward while the stop stays BELOW
market** (expect 200), or does it hit `OnWrongSideOfMarket` like an amend ABOVE market
(`manual_close` recipe)? This is SIM-answerable (place a stop below market, amend its level up a few
cents still below market, check the response). It gates the bot-amend execution path AND unblocks the
already-shipped `_maybe_reanchor` (which has never fired in prod). If it is a dead-end, the trailing
execution must go through cancel+replace and this cut's scope is revisited.

## 9. Tor B — tomorrow's attended probe (2026-08-10 15:30, both SIM and LIVE open)

- **SIM-answerable** (accept/reject; no price movement needed): §8 upward level-amend below market;
  over-hedge / oversell mechanics; kill-9 recovery.
- **LIVE-only** (needs real ticks): native `TrailingStopIfTraded` ratchet actually follows;
  narrow-amend repositions the level on a live tick; real implementation-shortfall / slippage.
- **Output:** the data to choose native-hybrid vs bot-amend-dynamic for the NEXT increment — a
  data-driven decision, not a bet. This Tor A cut stands regardless (bot-amend), and native becomes an
  optional robustness upgrade layered on the same pure-oracle decision + telemetry.

Probe discipline (from prior sessions): attended, daemon paused with operator consent, throwaway
instrument, hard `finally` flatten, run ON THE VPS via a one-shot systemd `--user` timer (a Mac-side
long background sleep is killed in-session). See
[`reference_saxo_trailing_mechanics_sim_probe`](trailing_probes_runbook_2026_08_07.md).

## 10. Out of scope (this cut)

Native `TrailingStopIfTraded`; the `+0.5R` break-even JUMP via cancel+replace; amend-route reposition;
trailing TP trigger (Chandelier on the TP side — base memo §10.4); trailing entry (§10.5); the `/edge`
fixed-vs-trailing lens UI (§10.6 — telemetry accrues now, the lens is later). Bot-down robustness of
the trail (native's selling point) is knowingly deferred.

## 11. Open questions (resolved by data / the probe, not by faith)

- The coarse-step size (amend cadence) vs whipsaw/cost — `[in_sample]`, tuned on accrued telemetry.
- `k` (give-back), the activation threshold and its unit (`+0.5R` vs `≥1 ATR`) — `[in_sample]`.
- Whether the upward level-amend below market is accepted (§8) — Tor B SIM probe.
- Native-hybrid vs bot-amend-dynamic for the next increment — Tor B LIVE probe.
