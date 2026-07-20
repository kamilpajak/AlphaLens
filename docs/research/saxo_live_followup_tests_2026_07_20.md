# Saxo SIM follow-up live tests — 2026-07-20 (ongoing US session)

**Status:** PLAN (attended; becomes the log after execution)
**Context:** written right after PR #872 merged (`a8f59889`) — the `__nextPoll`
fix that unblocks `broker reconcile`. Follows
[`saxo_first_fill_experiment_2026_07_18.md`](saxo_first_fill_experiment_2026_07_18.md).
**Prereq state (verified this session):** account flat (0/0), venv on main
checkout carries the fix, `PositionNettingProfile=FifoRealTime`, stock
`infoprices` null → yfinance is the ref-price fallback.

> Same doctrine as first-fill: **SIM only, infra verification, NOT alpha.**
> Attended (operator watches every placement on saxotrader.com/sim; assistant
> executes). No hypothesis-budget charge.

## 0. Operating window + preflight

- **WHEN:** US RTH. Now ~10:5x ET; new-order cutoff **15:00 ET**, flat by
  **15:45 ET**. ~4h left — ample.
- **Re-auth first (operator):** the OAuth token died (~40 min TTL). Start with
  `! .venv/bin/alphalens broker auth`.
- **Shell (assistant):** `set -a; source .env; set +a` +
  `export ALPHALENS_BROKER_ALLOW_ORDERS=1` in every placement call.
- **Instrument:** KO @ XNYS unless a test says otherwise. Price rule = §2 of the
  first-fill memo (yfinance ref; `entry=ref*1.01`, `close=ref*0.99`,
  `stop=entry*0.97`, `tp=entry*1.03`).
- **Audit reads:** the client now follows `__next` only — `broker reconcile`
  and `dump_activities.py` should work WITHOUT the raw-GET workaround. That is
  exactly what T1 verifies.

---

## T1 — Live validation of the `__nextPoll` fix (PRIORITY — do first)

**Why:** the fix is proven hermetically, but the whole point of the bug was that
it only manifests against REAL SIM audit responses (always-present `__nextPoll`).
This is the one test that can only be done live, and it closes today's loop.

**Steps:**
1. Place a marketable BUY bracket, qty=2 (Phase-A driver):
   `step_a_entry.py --qty 2 --entry <e> --stop <s> --tp <t>` → wait for fill.
2. **`.venv/bin/alphalens broker reconcile --json`** →
   **EXPECT `FILLED` (NOT `UNRESOLVED(audit_error)` / no 429).** This is the
   pass/fail line for the fix. Capture to `$SCRATCH/t1_10_reconcile_open.json`.
3. `dump_activities.py <entry_id> --all` → **EXPECT it succeeds via the CLI
   path now** (no raw-GET needed). Capture.
4. Cancel the TP child, cancel the stop child (position naked), then naked SELL
   close qty=2 (`step_c_close.py --qty 2 --limit <ref*0.99>`) → wait for fill.
5. **`broker reconcile --json`** → **EXPECT** entry → `FILLED(closed)` with
   `realized_r` computed (it had a stop), close → `FILLED` with `r=None`
   (naked). Capture to `$SCRATCH/t1_20_reconcile_closed.json`.

**Success:** reconcile returns FILLED verdicts end-to-end (no 429), realized_r
present for the stop-bearing entry and honest-None for the naked close. If it
STILL 429s → the fix is incomplete; capture and stop (do not merge anything
further). If FILLED → the fix is live-verified; note it in PR #872 / the
first-fill memo §9.

---

## T2 — Resting (non-marketable) Limit + partial-fill probe (best-effort, same session)

Exercises the **resting-Limit order type** (sits `Working`, not marketable) —
the first-fill run only used marketable Limit + StopIfTraded. Also folds in the
deferred **Phase D / O4 partial-fill** probe.

**Steps:**
1. Naked resting BUY limit BELOW market, qty=10:
   `step_a_entry.py --qty 10 --entry <round(ref*0.999,2)> --naked --note "T2 resting limit / partial probe" --out-name t2_50_probe_place`.
2. Confirm it sits **`Working`** (not filled) → capture open-orders (raw). This
   is the resting-Limit observation (its `OpenOrderType`, `Duration`).
3. Watch ~10 min (`dump_activities.py <id> --all` every ~2 min): any
   `Status=Fill` (non-final) row = partial jackpot → archive `EntryType=All`
   immediately.
4. **Branches:** (a) fills fully → close qty 10 naked SELL; (b) partial → cancel
   remainder, close the exact position qty read from the positions view;
   (c) no fill (most likely) → `broker cancel <id>`, confirm terminal.

**Success:** resting-Limit `OpenOrderType`/`Duration` captured raw; O4 verdict =
real partial row (jackpot) OR documented "not provokable via OpenAPI SIM" with
the qty-10 probe as the data point.

**RESULT (2026-07-20, DONE):** placed qty=10 naked BUY limit @ 81.15 (ref 81.56,
~0.5% below → guaranteed resting). Order `5039290953`: `OpenOrderType=Limit`,
`BuySell=Buy`, `Amount=10.0`, `FilledAmount=None`, `Duration=GoodTillDate`,
`Status=Working`, `OrderRelation=StandAlone` (`t2_51_open_orders.json`).
Findings: (1) a standalone non-marketable Limit sits `Working` (does not fill);
(2) duration contrast — a standalone entry with `ttl=1` → `GoodTillDate`, whereas
OCO bracket children came back `GoodTillCancel`; (3) `OrderRelation=StandAlone`
vs `Oco` for children. **O4:** not provokable via OpenAPI SIM (no deterministic
partial trigger; below-market limit won't partial) — opportunistic-capture
doctrine armed. Cancelled, account flat.

---

## T4 — Full `broker submit` end-to-end on a real brief (production placement path)

**Why:** the first-fill / T1 / T2 drivers called `place_bracket_order` DIRECTLY,
bypassing the production path. T4 exercises `alphalens broker submit` itself:
brief load → risk-sizing (`paper/sizing.py`) → FX conversion → `decompose_setup_plan`
per-tier brackets → server precheck → placement → reconcile. Nothing else has
validated that chain live.

**Preflight (2026-07-20, done):**
- Brief `--date 2026-07-13` (latest local; `brief_trade_setup` populated 12/12).
  Candidates: FDS, FCN, QLYS, TENB, S, NXST, KTOS, AVAV (all US equities → US-venue probe).
- **Token DEAD** → full attended re-auth required first.
- **`--equity` defaults to the €1M SIM total** → MUST pass a small `--equity`
  (start €2000, tune down) so the risk-sized position stays tiny.

**Steps:**
1. Operator: `! .venv/bin/alphalens broker auth` (full re-auth).
2. **DRY-RUN (safe — precheck only, places NOTHING):**
   `broker submit <TICKER> --date 2026-07-13 --equity 2000` → prints instrument
   resolution, sized plan, per-tier bracket decomposition, bracket table, and
   per-bracket precheck. Inspect: sizing sane, geometry stop<entry<tp, precheck PASS.
3. Operator review (attended eyeball of sizing/geometry vs the setup + chart).
4. **EXECUTE (SIM, small):** `export ALPHALENS_BROKER_ALLOW_ORDERS=1` then
   `broker submit <TICKER> --date 2026-07-13 --equity 2000 --execute --yes`.
   Verify orders/positions on saxotrader.
5. `broker reconcile --json` on the placed brackets → expect FILLED (or Working
   if the setup entry is a resting non-marketable limit — that's a valid
   production path too), realized_r via the #873 fix.
6. **Cleanup:** cancel working orders, close any open position (naked opposite),
   verify FLAT, `unset ALPHALENS_BROKER_ALLOW_ORDERS`.

**Success:** the production submit chain runs end-to-end on a real brief; account
flat at end. **Note:** the setup entry may be RESTING (limit away from market) →
sits Working, won't fill immediately; that still validates the resting-entry
production path (cancel at cleanup).

**RESULT (2026-07-20, DONE — surfaced a blocking production gap):**
- DRY-RUN on `S --date 2026-07-13 --equity 2000`: brief load → risk-sizing →
  **FX conversion EUR→USD** (1.1415 mid, 1% buffer) → **3-tier laddered decompose**
  (entries 18.08/16.68/15.81, common disaster-stop 12.57, TPs 18.81/19.30/21.40,
  ttl 7) → **precheck all 3 = `Ok`** (est_cash ~40-54 EUR, Commission 15, FX
  cross-check 0.01%). Whole chain up to precheck validated.
- EXECUTE: **REJECTED 0/3 — Saxo `HTTP 400 TooFarFromEntryOrder`.** The setup's
  single wide disaster-stop (12.57) is **20-30% below each tier entry**, far beyond
  Saxo's ~5% bracket child-distance window. So the production `submit` → Saxo OCO
  bracket path **cannot place real setups** — the disaster stop can't be an OCO
  child of the entry. Nothing was placed; account stayed flat.
- **Precheck is false-green here:** all 3 prechecks returned `Ok` yet placement
  failed on the child-distance rule — precheck does NOT validate `TooFarFromEntryOrder`.
- **Direction (design, not done):** put the disaster stop as a STANDALONE
  `StopIfTraded` placed after the entry fills (position-level stop, not an
  entry-child), so it isn't subject to the entry-child-distance limit; and add a
  LOCAL child-distance fail-fast in `_validate_price_relations` (or the CLI) so we
  fail with a clear message instead of a mid-batch raw HTTP 400. Needs a design memo.

## T3 — Broad order-type coverage (FUTURE — code-gated, NOT a same-session live test)

The current `place_bracket_order` wrapper only emits **Limit** (entry/exit) +
**StopIfTraded** (stop) as an **OCO** bracket. These types are UNTESTED and need
wrapper/contract extension BEFORE any live probe:
- **Market** (true market entry — wrapper builds Limit from `entry_limit`).
- **StopLimit**, **TrailingStop / TrailingStopIfTraded**, StopIfBid/Offered.
- Durations: **DayOrder**, explicit **GTD**, **FillOrKill**,
  **ImmediateOrCancel**.

**Path:** decide per-type whether the live strategy needs it; for each needed
type, extend the contract + `_build_bracket_body` (TDD) in a worktree PR, THEN a
short attended live probe (place → capture `OpenOrderType`/audit shape → cancel).
Do NOT attempt these live today — no code path exists yet.

---

## Cleanup + end-of-session gate (mandatory, every run)

Same as first-fill §5: universal abort sweep available at any point (cancel
entry → close position with exact-qty naked opposite limit → cancel every
survivor to terminal → verify flat). End: `broker orders` empty, `broker
positions` empty, final `reconcile --json`, account snapshot, journal copy,
`unset ALPHALENS_BROKER_ALLOW_ORDERS`. No orphan protective order left working.

## Recommended order for today

**T1 now** (validates the merged fix — highest value, ~15 min) → **T2** if time
and appetite (~15 min) → **T3 deferred** (needs code). Re-auth is the first
operator action.
