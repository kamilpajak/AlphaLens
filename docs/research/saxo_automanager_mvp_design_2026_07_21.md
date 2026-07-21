# Saxo Auto-Manager MVP — Architecture Design (SIM-first exit-management engine)

**Status:** DRAFT — design APPROVED in brainstorming 2026-07-21 (pending user spec review)
**Date:** 2026-07-21
**Approved decisions (brainstorming):** semi-auto (human cherry-picks → tool auto-manages); SIM-first + a bounded `$1000`/`~$100` live TEST escape behind a future ADR (not strategy capital); **Approach 1** (always-on polling daemon on the VPS); hand-off via CLI `arm` → `picks.jsonl` (NOT web — Django is read-only); **disaster stop ALWAYS standalone-after-fill** (never an OCO child — correct realized-qty sizing; accepted ~30–60 s unprotected window on SIM); **fill-detection is a pluggable `fill-source`** (polling impl in the MVP; streaming is a phase-B drop-in behind the same interface).
**Scope owner:** trade-side (ADR-0013 T6 IN-FLIGHT / T7 EXIT live consumer)
**Predecessors:** Option A (`#874`, MERGED), Option B (`saxo_wide_stop_bracket_design_2026_07_20.md`, primitive SIM-validated), Option C (`saxo_far_tp_tranche_design_2026_07_21.md`, interim design only), reconcile fix (`#872`), closed-pair fix (`#873`)

---

## Goal

Give the deferred exit-manager a **live consumer**. Today the human cherry-picks a candidate off the read-only thematic brief and hand-types `alphalens broker submit`, which is **one-shot**: it places brackets and returns — no fill wait, no post-fill action, `T6 IN-FLIGHT does nothing`. The MVP closes the loop end-to-end **on SIM**: a picked candidate is sized (exists), placed with the shipped Option-A guard plus the Option-C in-band-subset interim (**always** placing the disaster stop exactly once), then a persistent monitor watches for fill, manages the base position to a terminal state, reconciles, and can be killed instantly — all while a session-keeper holds the OAuth chain alive unattended.

This is **infrastructure, deliberately decoupled from the alpha question**. Capital deployment stays off-table (no validated alpha). The `$1000` live account with `~$100` risk tolerance is a **bounded test escape** for behaviours SIM cannot exercise — not strategy capital. SIM-first is structural: `SaxoClient` refuses any non-SIM base URL.

The design principle stays **"augmentation, not replacement"**: the human remains the picker (the `(ticker, brief_date)` tuple is the pick key). What becomes automatic is only the **post-pick placement AND lifecycle management** on the CLI side.

---

## Scope — MVP vs deferred

### In the MVP (ships)

| Stage | What ships | Reuses |
|---|---|---|
| **Pick** | Human reads unchanged read-only web card, types `alphalens broker arm TICKER --date` → appends intent to `picks.jsonl` | `load_brief`, existing CLI |
| **Size** | Equal-risk sizing per pick | `compute_setup_plan` (done) |
| **Place** | Option-C **in-band-subset classifier**: place only children clearing the 15% guard; **always** place the disaster stop exactly once (Option-B standalone `StopIfTraded` after entry fill); report far-TP tiers as operator-managed, never silently dropped | Option-A guard (`#874`), `decompose_setup_plan`, `_run_prechecks`, `place_bracket_order`; **NEW** `place_standalone_stop` |
| **Fill-monitor** | Poll loop (30–60 s) diffing successive broker snapshots vs journal to detect entry fill | `list_open_orders`, `get_order`, `reconcile_brackets` (read-only) — **NEW** cadence driver |
| **Base management to terminal** | After entry fills → size + place standalone disaster stop to realized filled qty; watch to terminal (stop hit / expiry / manual close); classify realized_r | `reconcile.py`, `resolve_order_outcome`, `get_closed_position_rows` |
| **Reconcile** | Stateless re-derivation of every bracket's status each tick (also the crash-recovery engine) | `reconcile_brackets` (done) |
| **Kill-switch** | In-loop `KILL` file stat every tick → halt placement, run reconcile + cancel only | `cancel_order` (ungated, cascades); **NEW** file check |
| **Token keep-alive** | VPS systemd timer/daemon rotating the OAuth chain inside the 40-min window | `refresh_now()` (done); **NEW** always-on driver |

### Deferred to phase B (full synthetic-OCO manager)

- **Far-TP tranches as standalone limit-sells** (unproven vs `TooFarFromMarket`/`OnWrongSideOfMarket`; needs its own SIM precheck — risk R1)
- **Cancel-on-fill** (OCO synthesis: stop and TP are not in a Saxo OCO group; double-exit hazard)
- **Resize-on-partial PATCH** (FifoRealTime/Intraday: a full-size stop over-hedges after a partial and can flip short)
- **TP → breakeven ratchet**
- **42-session time-stop** (`TIME_STOP_DAYS=42` exists in `paper/constants` but is unused on the live path)
- **Streaming fill detection** (WebSocket + ContextId/ReferenceId + snapshot/delta + second reauth path)
- **Web "arm" button** (Django is read-only today; multi-PR net-new)

The MVP deliberately keeps the base position's **single disaster stop** as the only exit. TP scale-out (`3×33%`) is no-alpha discipline scaffold that `decompose_setup_plan` drops by design and never reaches Saxo — building the OCO machinery to manage it has no payoff until there is a reason to hold multiple exits.

---

## Approaches + recommendation

### Approach 1 — Always-on polling daemon on the VPS (`Type=simple`)

A single long-lived `systemd --user` service (`Restart=on-failure`, linger-enabled) runs a control loop: check kill-file → check chain → drain picks → enforce caps → place → poll+reconcile open brackets → act → sleep 30–60 s. State lives entirely in append-only JSONL (`picks.jsonl` + `submissions.jsonl`); status is **recomputed** each tick by `reconcile_brackets`, never stored. The poll cadence itself rotates the OAuth token as a side effect (each `get_access_token()` refreshes at `expires_in − 120 s`); a dedicated `refresh_now()` timer covers idle stretches.

- **+** Matches the fill/session/crash analysis exactly: polling reuses the entire shipped read+reconcile stack; the daemon self-refreshes the token by touching it each loop; crash-recovery is "re-run reconcile."
- **+** Precedent exists (`alphalens-form4-backfill.service` is `Type=simple`+`Restart=on-failure`).
- **+** Lowest new surface — one net-new driver around existing primitives.
- **−** A >40-min VPS outage still kills the chain (attended re-login). Mitigated by alert, not eliminated.
- **−** A single long-lived process must be crash-safe; the place-before-journal window needs an orphan sweep.

### Approach 2 — Streaming daemon (WebSocket push)

Same daemon shell, but fill detection is Saxo streaming: `/streamingws/connect?contextid=…` + `/port/v1/orders|positions/subscriptions`, snapshot+delta merge, heartbeats, reconnect-and-resubscribe, and a **second** token-reauth path (`PUT /streaming/ws/authorize`, 202).

- **+** Sub-second fill latency.
- **−** Zero of this infrastructure exists today. Adds a second independent keep-alive mechanism, compounding the exact unattended-session problem the MVP is trying to solve.
- **−** A dropped socket + missed delta silently desyncs local state — so it **still needs the polling/reconcile path underneath** as the reconciliation floor. You build both.
- **−** No rate pressure or latency requirement justifies it: stated tolerance is "seconds-to-minutes over a multi-day ladder"; a 30–60 s poll is ~2–4 req/min against a 120 req/min ceiling.

### Approach 3 — On-demand / oneshot+timer (episodic, edgar-detect shape)

`Type=oneshot` + `OnUnitActiveSec=Nmin`, each fire: reconcile all brackets, take any due actions, exit. No persistent process.

- **+** Simplest lifecycle; crash-recovery is trivial (every fire is cold-start reconcile); precedent is `alphalens-edgar-detect`.
- **+** No long-lived-process crash-window worries between fires.
- **−** Fires are coarse: an unattended-fill → place-disaster-stop reaction is delayed up to a full interval, widening the **unprotected window** between entry fill and standalone stop placement (Option B's stated gap). A 1-min interval approximates a poll but pays process-spawn cost 1440×/day.
- **−** OAuth chain: a oneshot that fires every 15 min is inside 40 min, but during a genuinely idle stretch (no picks, timer still firing) it keeps the chain alive only if each fire touches the token — workable but you still want the explicit refresh. Between-fire gaps must never approach 40 min.
- **−** Harder to hold the "manage a filled position to terminal promptly" guarantee — the whole value proposition is timely post-fill action.

### Recommendation — **Approach 1 (always-on polling daemon on the VPS)**

It is the simplest thing that delivers a real end-to-end "picked candidate is bought and managed to terminal on SIM." Polling reuses `reconcile.py` (already a snapshot-diff verdict engine) and every read primitive that already exists; the daemon's own poll cadence keeps the OAuth chain rotating with no separate mechanism during active monitoring; and crash-recovery is "re-run the read-only verdict engine over the append-only journal" — no new persistent state machine. Streaming (Approach 2) is over-build for a single-account handful-of-positions ladder and doubles the keep-alive problem; oneshot (Approach 3) widens the post-fill unprotected window that Option B specifically flagged. Keep polling as the reconciliation floor; defer streaming to phase B only if second-level latency or many concurrent positions ever actually demand it.

**Nuance kept from Approach 3:** the idle-period keep-alive. When no bracket is open the poll loop backs off, so a dedicated `refresh_now()` timer (`~20–25 min`, well inside 40 min) covers idle stretches — effectively re-creating the `alphalens-saxo-refresh` unit ADR-0012 removed. This is a second small timer unit alongside the manager daemon, not a second streaming reauth path.

---

## Components (small single-responsibility units)

Each is a thin seam over existing primitives; net-new code is minimal and named.

1. **picks-queue** (`~/.alphalens/broker_orders/picks.jsonl`, append-only)
   - Interface: `arm_pick(ticker, date) -> None` (new `alphalens broker arm` subcommand appends `{ticker, date, armed_ts, status:"armed"}`); `iter_picks() -> Iterator[Pick]`.
   - Responsibility: durable human-intent inbox. Mirrors `submission_log.py`'s append-only idiom. The clean seam between attended intent and autonomous management.

2. **hand-off surface** = the `arm` CLI subcommand (attended). Reuses `load_brief` for validation at arm time (fail fast if no brief row for `(ticker,date)`). No web work.

3. **safety-gate** (pure predicate module)
   - Interface: `check(pick, journal_view, broker_view, session_state) -> Allow | Refuse(reason)`.
   - Enforces, before any placement: kill-file absent, chain alive, `ALPHALENS_BROKER_ALLOW_ORDERS=1`, **position-count cap** (open journaled brackets + `get_positions()` vs `MAX_OPEN`), **portfolio gross cap** (sum gross across armed+live vs a portfolio limit — the existing `setup_plan_gross_guard_limit` is per-submit only), and **daily-loss limit** (cumulative realized loss vs bound; trips the kill-file).

4. **session-keeper** (session-lifecycle)
   - Interface: `ensure_alive() -> ChainStatus`; wraps `OAuthTokenProvider.get_access_token()` / `refresh_now()`.
   - Responsibility: called at the top of every tick; refreshes at `expires_in − 120 s`; on dead chain fires the existing Telegram `_chain_lost` alert and signals the loop to stop placing. Companion idle-timer unit calls `refresh_now()` every ~20–25 min.

5. **placement-planner** (the Option-C interim classifier — net-new, stateless)
   - Interface: `classify(setup_plan, instrument) -> PlacementPlan` returning per-tier: the entry bracket **plus its TP child ONLY when the take-profit clears the 15% guard** (else entry-only, TP reported operator-managed). The **disaster stop is never a bracket child** — it is always placed as a standalone `StopIfTraded` **after** the entry fills (position-manager), sized to realized qty. This uniformly avoids the FifoRealTime partial-fill oversize hazard (an OCO child stop sized to *planned* qty over-hedges on a partial and can flip short), at the cost of the ~30–60 s unprotected window (accepted on SIM). Emits the **disaster-stop-exactly-once** decision and a whole-plan operator report (which tiers place, which TPs are operator-managed).
   - Consumes `decompose_setup_plan` output; upgrades the bare `#874` reject into place-in-band-subset + honest report.

6. **placer** (thin wrapper over shipped placement)
   - Interface: `place(placement_plan) -> list[PlacedOrder]` looping `place_bracket_order` (Option-A guard, precheck, single POST, `x-request-id=client_request_id`, auto-repair on partial-accept) + journaling via `_place_and_record`'s path.
   - Idempotency: reuse the same `client_request_id` on retry of the same logical bracket; fresh uuid4 for new ones.

7. **standalone-stop placer** (net-new SaxoBroker method `place_standalone_stop(...)`)
   - Interface: `place_standalone_stop(uic, side, qty, stop_price) -> PlacedOrder`.
   - Option-B primitive (SIM-validated: standalone Sell `StopIfTraded`, `relation=StandAlone`, no `TooFarFromEntryOrder`). Called **after** entry fill, sized to **realized filled qty**, journaled out-of-band (the disaster-stop price is needed by `compute_realized_r`).

8. **fill-source** (pluggable fill-detection interface — net-new)
   - Interface: `poll_tick() -> list[Transition]` = "was WORKING last tick, now absent/terminal". The MVP ships a **polling implementation** (`PollingFillSource`) over `list_open_orders` + `reconcile_brackets`; a **streaming implementation** (`StreamingFillSource`, WebSocket) is a phase-B drop-in behind the SAME interface — **no control-loop change**. Detects entry fills + terminal transitions; drives back-off when idle. Polling remains the reconciliation floor under any future streaming layer.

9. **position-manager** (base-management orchestrator — net-new, the "act" half)
   - Interface: `advance(bracket_verdict) -> Action`.
   - MVP action set: on entry-FILLED with no protective stop yet → place standalone disaster stop to realized qty; on any terminal verdict → nothing / cancel remaining working children; on PAST-TTL divergence → alert (do not auto-cancel in MVP).

10. **reconcile-bridge** (adapter over the shipped read-only engine)
    - Interface: `verdicts(records, broker) -> list[ReconcileVerdict]`. Direct call to `reconcile_brackets`; also the recovery engine on start.

11. **state-store** = the two append-only JSONL files (`picks.jsonl`, `submissions.jsonl`) + **no** new lifecycle entity. Status is recomputed. If a durable phase marker proves unavoidable, it is an **append-only status line** honoring config-version-cohort discipline (T8: live fills never pool with broker-free replays) — never a rewritten record.

12. **orphan-sweeper** (crash-window closer — net-new, read-only)
    - Interface: `sweep(broker, journal) -> list[Orphan]`.
    - On start, flags any open order/position whose `ExternalReference`/`OpeningExternalReferenceId` (carries the bracket `client_request_id`) is absent from the journal — an order placed but never journaled (mid-place crash). Alert a human; never auto-manage an unrecorded order.

13. **control-loop** (the daemon shell) — orchestrates 3→12 each tick.

14. **job-metrics/health** — `ExecStopPost=alphalens-emit-job-metrics broker-manager` + `AlphalensJobStale` Prometheus rule; `MemoryMax`/`TasksMax` caps.

---

## Data flow + control loop

**Pick key** stays the `(ticker, brief_date)` tuple. The human reads the unchanged read-only web card, discusses with the group, then types `alphalens broker arm KO --date 2026-07-20` → **picks-queue** appends intent.

**Control loop (each tick, ~30–60 s while any bracket non-terminal; back off when idle):**

```
1. safety-gate: stat KILL file       → present ⇒ skip placement, run reconcile + cancel only
2. session-keeper.ensure_alive()     → dead chain ⇒ Telegram alert, stop placing
3. orphan-sweeper (start-of-process only) → alert on unjournaled orders/positions
4. drain picks-queue (status=="armed", not yet joined to submissions.jsonl)
5. for each new pick:
     safety-gate.check (ALLOW_ORDERS + MAX_OPEN + portfolio gross + daily-loss)
     _resolve_instrument_and_plan → gross-guard → decompose_setup_plan
     placement-planner.classify (Option-C in-band subset; disaster-stop-exactly-once)
     placer.place (Option-A guard, precheck, POST, journal)   ← reuse client_request_id on retry
6. reconcile-bridge.verdicts(records, broker)   ← recompute status for every bracket
7. position-manager.advance per verdict:
     entry FILLED, no stop yet     → standalone-stop placer (realized qty), journal out-of-band
     terminal (stop/expiry/closed) → cancel remaining working children (if any)
     PAST-TTL / divergence / orphan → Telegram alert, no auto-action
8. sleep (poll interval, or back-off if idle)
```

**Idempotency.** Every logical bracket carries a uuid4 `client_request_id` used as Saxo `x-request-id` (15 s dedup window). POST is never blind-retried — only on provably-unsent/429, always reusing the same id. The journal is the idempotency ledger: the loop reads `submissions.jsonl` to know what is already placed and reuses ids; any future phase-B side-effect must derive idempotency from journaled intent + reused id, never from in-memory memory.

**Crash-recovery = stateless reconstruction.** The manager holds no durable in-memory state that isn't rederivable. On restart:
1. Run `reconcile_brackets` over `submissions.jsonl`.
2. TERMINAL verdicts → done.
3. WORKING → resume poll-to-fill.
4. FILLED + position-open → resume base management to terminal (MVP: monitor + ensure standalone stop present).
5. `UNRESOLVED(audit_error)` → transient, retry next tick.
6. FILLED-but-no-position-and-no-closed-pair → divergence, alert, don't guess.
7. **orphan-sweeper** covers the one real gap — the place-before-journal crash window (`_place_and_record` journals *after* placement). Read-only sweep flags orders at Saxo the journal never recorded. (Optional harder guarantee: intent-first journaling — write a "submitting" line with the id before POST — deferred; orphan sweep fits the current append-only model with less change.)

Because every tick recomputes truth, the fill-poller needs **no "which fills have I seen" checkpoint**.

---

## Session lifecycle

**One attended login + a VPS keep-alive timer inside the 40-min window = indefinite SIM session.** Documented lifetimes: access token ~1200 s (20 min), refresh token ~2400 s (40 min), rotating single-use. Saxo imposes **no** max session length, no chain-age cap, no forced re-auth; the session ends only on refresh-token expiry or manual `SaxoTraderGO → Application Access` revocation.

- **Active monitoring:** the 30–60 s poll calls `get_access_token()` far more often than every 18 min, so the provider self-refreshes as a side effect — no separate timer needed while a ladder is live.
- **Idle stretches:** dedicated `refresh_now()` timer unit every ~20–25 min (re-creates the `alphalens-saxo-refresh` unit ADR-0012 removed).
- **Rotation safety:** persist the rotated pair **before** using the new access token (done); single-host ownership + flock on sibling `.lock` + sibling-adoption prevents a two-process rotation race (keep the daemon the sole refresher).
- **Dominant failure = missed refresh window >40 min** (VPS down, daemon crash-gap, SIM-maintenance outage). Recovery is the attended `alphalens broker auth` browser login; `_chain_lost` fires the Telegram alert. Run on the always-on VPS (not the sleeping Mac).

---

## Safety model

Layered, most reused, defense from structural rail down to daily-loss trip:

1. **LIVE-vs-SIM gate — STRUCTURAL, reuse as-is.** `SaxoClient` refuses non-SIM base URL (`SaxoLiveEnvironmentBlockedError`); `LIVE_TRADING_ENABLED=False`; `from_env` fails loud unless `SAXO_ENV=="sim"`. LIVE is **unreachable in code** — mirrors the research-side `capital_deploy_clause`. When the `$100` live escape is eventually needed, gate it behind a **separate** explicit env (`ALPHALENS_BROKER_LIVE=1`) **AND** the `$100` caps and its own ADR — never collapse the rail into a runtime flag.
2. **ALLOW_ORDERS master arm.** `ALPHALENS_BROKER_ALLOW_ORDERS=1` checked at the top of `place_bracket_order` before any client call. `cancel_order` / `precheck` deliberately ungated (safe ops). Lives in the unit `EnvironmentFile`: set=armed, unset+restart=inert.
3. **Kill-switch — net-new, in-loop.** `~/.alphalens/broker_orders/KILL` stat'd every tick. Present ⇒ stop placing/managing, run reconcile + cancel only. `touch KILL` is **instant** (the env arm needs a unit restart; the file does not). Two-layer: env = arm, file = emergency stop.
4. **Per-pick sizing + gross guard — reuse.** `compute_setup_plan` equal-risk; `setup_plan_gross_guard_limit = GROSS_SAFETY_FRAC(1.0) × equity × fx` before decompose. The disaster stop (Option-B `StopIfTraded`) bounds single-pick loss.
5. **Portfolio-level caps — net-new in safety-gate.** Position-count cap (`MAX_OPEN`) and portfolio gross cap across all armed+live picks (the shipped guard is per-submit only). Refuse a new pick if either exceeded.
6. **Daily-loss limit — net-new.** Track cumulative realized r (`compute_realized_r` from closed FIFO pairs) and trip the kill-file when daily loss crosses the bound. Belt (disaster stop, per-pick) + suspenders (daily limit, per-day).
7. **$1000/$100 live bounding (when the rail lifts, future ADR).** Sizing equity `= $1000`, risk fraction so max per-pick loss `≈ $100`; disaster stop bounds the single pick; daily-loss limit bounds the day; separate live env + `MAX_OPEN=1` recommended for the first live escapes.

---

## Phasing to full (B)

The MVP's seams are chosen so phase B is additive, not a rewrite:

- **Far-TP standalone limit-sells** — the placement-planner already emits an "operator-managed far-TP" report; phase B turns that report entry into a real standalone `LIMIT` order via a new `place_standalone_limit` (after its own SIM precheck vs `TooFarFromMarket`/`OnWrongSideOfMarket` — risk R1). No loop-shape change.
- **Cancel-on-fill (synthetic OCO)** — position-manager gains an action: when standalone stop or a TP fills, cancel the sibling. Saxo OCO is strictly 2 orders / related-cap 2, so synthesis stays client-side. The reconcile-bridge already sees terminal transitions to trigger it.
- **Resize-on-partial PATCH** — fill-poller already sees `PARTIALLY_FILLED`; phase B adds a PATCH to the standalone stop to match realized qty (guards the FifoRealTime over-hedge/flip-short hazard). The standalone-stop placer becomes standalone-stop *manager*.
- **TP → breakeven ratchet** — position-manager gains a ratchet action keyed to a journaled ratchet-state line (append-only, config-version-cohort-safe).
- **42-session time-stop** — the loop already knows each bracket's anchor date from the journal; wire `TIME_STOP_DAYS=42` into an `advance` action (currently `DEFAULT_ORDER_TTL_DAYS=7` on the entry).
- **Streaming** — a `StreamingFillSource` implementation of the pluggable **fill-source** interface (Components §8): a drop-in for the polling impl with **no control-loop change**, layered **above** the poll+reconcile floor (which remains the reconciliation + desync-recovery path). Adds the `PUT /streaming/ws/authorize` reauth to the session-keeper. **Recommended before the `$100` live escape** (shrinks the fill→standalone-stop unprotected window from ~30–60 s to ~seconds).
- **Web arm button** — later just POSTs into the **same** picks-queue (a small Django write endpoint appending to `picks.jsonl` or an `ArmedPick` table the manager also reads). The manager doesn't change — starting with the CLI does not paint phase B into a corner.

Each phase-B behaviour is a new **T-layer version key** (ADR-0013): a change to policy constants drifts `execution_config_version`, creating a forward-only cohort boundary; live fills never pool with broker-free replays.

---

## TDD + SIM-live-probe strategy

**Doctrine:** research/broker tests are `unittest.TestCase` (pytest-style silently skipped in CI); TDD always, red→green even for 2-line fixes; live probes are opt-in `skipUnless(env flag)` shape-only (assert non-emptiness/keys/finish-reason, never values), collected-but-skipped by default so they never gate PRs.

**Unit / offline (fixtures, deterministic):**
- placement-planner: LAZ (tier 0 places, tier 1 TP far), S 2026-07-13 (all 3 tiers far — stop-far case), knife-edge `+15.00%` vs `+15.01%`, **disaster-stop-exactly-once** invariant, entry+stop-only when only-TP-far, standalone-stop-path when stop-far.
- fill-poller: WORKING→absent transition detection; `PAST-TTL` divergence surfaced not auto-cancelled.
- position-manager: entry-FILLED → standalone stop sized to **realized** filled qty (real SIM fixture `FillAmount==2.0`, `ExecutionPrice==82.09`), not planned qty.
- reconcile non-regression: existing verdicts unchanged (the read-only engine must not shift).
- crash-recovery: restart re-runs reconcile → identical classification; orphan-sweeper flags an order present at broker but absent from journal; idempotency — retry of a logical bracket reuses `client_request_id`.
- safety-gate: kill-file present ⇒ no placement; `MAX_OPEN`/portfolio-gross/daily-loss refusals; `ALLOW_ORDERS != 1` ⇒ `BrokerCapabilityError`.
- session-keeper: refresh at `expires_in − 120 s`; dead-chain ⇒ Telegram alert path; rotation persists-before-use.

**SIM-live probes (opt-in, `SAXO_LIVE_TEST=1`, cost = SIM only):**
- End-to-end happy path on SIM: arm a real ticker → daemon sizes/places in-band subset + disaster stop → provoke/await entry fill → assert standalone `StopIfTraded` placed at realized qty, `relation=StandAlone`, no `TooFarFromEntryOrder` → cancel/close → assert terminal reconcile + realized_r.
- `place_standalone_stop` primitive re-probe (already validated once: KO qty 2 `@61.36`, OrderId 5039296412).
- Unattended-session probe: leave the daemon idle past one access-token life, assert the keep-alive timer kept the chain (no `_chain_lost`).
- `__nextPoll` non-regression (T1 already live-validated): audit resolution must follow only `__next`, never `__nextPoll` (429 masks as UNRESOLVED).
- Weekly CI `live-probes` schedule job (never push/PR), Telegram/GitHub-issue on failure — extend the existing pattern with a `saxo` probe.

**Note (from Option C companion log):** the resting-limit partial-fill case (O4) is **not provokable via SIM** — resize-on-partial validation is therefore a phase-B item gated on a live-provokable path, not an MVP blocker.

---

## Risks + open questions

1. **Unprotected window (entry fill → standalone stop placed).** Between entry fill and the next poll tick that places the disaster stop, the position is naked. 30–60 s exposure on SIM is acceptable; for the live escape, tighten the poll interval or place a wide protective stop faster. Open: is a tighter interval (or a targeted post-fill immediate placement) worth it for the `$100` path? (Option B flagged this as the reason it was deferred — the MVP owns it now.)
2. **FifoRealTime / Intraday planned-vs-realized qty.** The standalone stop **must** size to realized filled qty, never planned — a planned-qty stop over-hedges and can flip short after a partial. MVP handles the full-fill case cleanly; partial-fill resize is explicitly phase B. Open: does the MVP place a stop at all on a *partial* entry fill, or wait for full fill / TTL? (Recommend: place to the realized partial qty, accept it may need phase-B PATCH; alert on partial.)
3. **>40-min VPS outage kills the chain.** Recovery is attended browser login. Mitigation is alerting only. Open: acceptable for SIM MVP; for any live escape, consider a redundant refresher or faster human-recovery runbook.
4. **Place-before-journal crash window.** Orphan-sweeper covers detection (read-only, alert-only). Open: adopt intent-first journaling in phase B for auto-recovery, or keep alert-only?
5. **Config-version cohort discipline vs a lifecycle marker.** The MVP recomputes status and avoids a lifecycle entity. If any durable phase marker becomes necessary, it must be an append-only status line that respects the `execution_config_version` cohort boundary (T8). Open: can the whole MVP truly stay recompute-only, or does the standalone-stop-placed fact need one journaled line? (Likely yes — the standalone stop's own submission line, out-of-band, already carries it.)
6. **Far standalone SELL LIMIT unproven (R1).** Only the standalone `StopIfTraded` is SIM-validated; a far standalone limit-sell is untested vs `TooFarFromMarket`/`OnWrongSideOfMarket`/`PriceExceedsAggressiveTolerance`. MVP does **not** place far TPs (reports them operator-managed) — so this risk is fully deferred with the far-TP feature. Do not let a phase-B far-TP land without its own SIM precheck.
7. **Portfolio caps have no shipped baseline.** `MAX_OPEN` and portfolio-gross are net-new numbers with no validated basis (no alpha). Set conservatively (`MAX_OPEN` small; portfolio gross ≤ equity) and treat as operator policy, not tuned parameters.
8. **Single-host refresher invariant.** Two Claude/daemon processes sharing the token store race on rotation. Doctrine: the VPS daemon is the sole refresher; no concurrent `broker` CLI that refreshes on the same host while the daemon runs. Open: enforce with the existing flock/adoption only, or add an explicit "daemon owns refresh" lock file?
9. **Daily-loss / realized-r timing.** Realized r comes from closed FIFO pairs, which under Intraday are EoD-netted — intraday the daily-loss trip may lag. Open: is the disaster-stop bound sufficient intraday, with the daily-loss limit as an EoD backstop? (Recommend yes for MVP.)
