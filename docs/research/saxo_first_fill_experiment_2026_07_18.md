# Saxo SIM first-fill experiment — attended runbook + log

**Status:** READY (becomes the experiment log after execution)
**Date:** 2026-07-18
**Related:** [`saxo_broker_layer_design_2026_07_17.md`](saxo_broker_layer_design_2026_07_17.md), [ADR 0014](../adr/0014-broker-agnostic-execution-layer.md)
**Drivers:** `apps/alphalens-research/scripts/first_fill/` (copy to `/tmp/first_fill/` per Phase 0)

> **This is INFRASTRUCTURE VERIFICATION (parameter estimation), NOT an alpha
> look.** No hypothesis is tested against market outcomes; nothing here reads
> or influences selection, and NO hypothesis-budget charge is logged in
> `docs/research/edge_hypothesis_budget_2026_07.md`. The deliverable is the
> raw shape of Saxo SIM's fill/audit/netting behavior so the P3 parser and
> reconcile loop rest on evidence instead of doc-sourced guesses.

## 1. Objectives

- **O1 (primary):** capture the RAW audit-row shape of a real SIM fill — the
  exact FinalFill row (`FilledAmount` / `FillAmount` / `ExecutionPrice` /
  `AveragePrice` presence, types, values) and whether an intermediate
  `Status=Fill` row precedes it — so the P3 parser's deliberate
  `UNRESOLVED(fill_fields_unverified)` branch (`broker.py:383-390`) can be
  confirmed against reality. Two samples: BUY entry fill and SELL close fill.
- **O2:** observe bracket children after the entry fills (do both children go
  Working? amounts? OCO linkage fields on the open-orders rows), then SAFELY
  probe DELETE-on-one-child: cancel the TAKE-PROFIT child while the STOP
  still protects the position; record the sibling's fate raw (docs say OCO
  cancels only on EXECUTION — assume no cascade, verify).
- **O3:** drive one full reconcile cycle through the journal: place → FILLED
  verdict from `broker reconcile --json` → manual opposite close via the
  naked (stop=None, tp=None) SELL limit → closedpositions FIFO pair (timing
  branched on `PositionNettingProfile`) → FILLED(closed) verdict; confirm
  `realized_r` is computed for the stop-bearing entry record and honestly
  `None` for the stop=None close record.
- **O4 (best-effort):** partial fill. NOT deterministically provokable via
  OpenAPI SIM (only FIX SAFT has quantity-keyed partial scenarios,
  unreachable from the OpenAPI gateway) — one cheap opportunistic attempt
  (qty-10 at-the-touch resting limit); regardless of outcome, establish the
  opportunistic-capture doctrine (any future SIM order emitting a non-final
  Fill row gets its `EntryType=All` payload archived); the partial-fill
  parser path is otherwise validated with synthetic rows faked faithfully
  from the O1-captured real shape.
- **O5 (byproduct findings, free):** does `/trade/v1/infoprices` return
  non-null delayed quotes during RTH on this account (nulls seen before);
  which side's `ExternalReference` lands on the closedpositions row; does
  the SELL naked limit NET the long (FIFO) or open a short; latency notes
  per step.

## 2. Session constraints

- **WHERE:** main checkout `/Users/jacoren/Developer/Personal/AlphaLens`
  (the broker surface must be on `main` there; this memo's branch merges
  first). All commands from that root; `.venv/bin/...`.
- **WHEN:** US RTH only. 09:30-16:00 ET = 15:30-22:00 Europe/Warsaw (EDT).
  Start >= 10:00 ET (16:00 Warsaw). NO new placements after 15:00 ET;
  cleanup verified flat by 15:45 ET. Budget ~60-90 min.
- **Instrument:** KO @ XNYS, qty 2 (Phase A) / 10 (Phase D). Tick $0.01.
- **Attended:** operator watching every placement; assistant executes.
- **House rule:** NO raw HTTP writes — every placement via
  `SaxoBroker.place_bracket_order`, every cancel via `broker cancel`,
  raw reads via `get_order_activities` / `get_json` (GET-only escape hatch).

### Price rule (SIM feed is delayed L1; fills synthetic ~at-quote)

`ref` = `/trade/v1/infoprices` Quote mid, fetched READ-ONLY via
`client.get_json("trade/v1/infoprices", params={"Uic": <uic>, "AssetType":
"Stock", "AccountKey": <account_key>, "FieldGroups":
"Quote,PriceInfoDetails"})` → `$SCRATCH/00_infoprice.json`. If Bid/Ask null
(seen before): `ref` = last price from the CANONICAL yfinance client wrapper.
KO 15-min delayed drift << 1%:

| Leg | Formula |
|---|---|
| BUY entry (marketable) | `round(ref * 1.01, 2)` |
| SELL close (marketable) | `round(ref * 0.99, 2)` |
| stop_loss | `round(entry * 0.97, 2)` |
| take_profit | `round(entry * 1.03, 2)` (both within the ±5%-from-ENTRY child window) |
| Phase D probe entry | `round(ref * 0.999, 2)` (at/just under the touch) |

## 3. Runbook

### Phase 0 — preflight (10 min)

```bash
cd /Users/jacoren/Developer/Personal/AlphaLens
git pull                      # 0.1 main must include brokers/ (>= 8f103a9c) + the G1/G2 gap-closer commits
mkdir -p /tmp/first_fill && cp apps/alphalens-research/scripts/first_fill/*.py /tmp/first_fill/
export SCRATCH=~/.alphalens/broker_orders/experiments/first_fill_$(date +%F); mkdir -p "$SCRATCH"

.venv/bin/alphalens broker auth --status          # 0.2 dead -> broker auth (attended), then --refresh
.venv/bin/alphalens broker account                # 0.3 baseline
.venv/bin/alphalens broker positions              #     MUST be zero positions
.venv/bin/alphalens broker orders                 #     MUST be zero open orders (else CLEANUP path, abort until flat)
.venv/bin/python /tmp/first_fill/read_netting_profile.py   # 0.4 -> $SCRATCH/01_client_profile.json
# 0.5 price ref per §2 -> $SCRATCH/00_infoprice.json; compute entry/stop/tp/close; operator eyeballs vs chart
export ALPHALENS_BROKER_ALLOW_ORDERS=1            # 0.6 THIS shell only; keep set until flat
```

`PositionNettingProfile` recorded: `FifoRealTime`/`AverageRealTime` ⇒
closedpositions immediate; `FifoEndOfDay` ⇒ pair sits open as Square until
exchange EOD.

### Phase A — marketable BUY bracket, qty=2 (O1 + O2 setup)

```bash
.venv/bin/python /tmp/first_fill/step_a_entry.py --qty 2 --entry <computed> --stop <computed> --tp <computed>
# A.2 script polls <=60s (3s interval) until the entry leaves the open-orders view
.venv/bin/python /tmp/first_fill/dump_activities.py <entry_order_id> --all --out-name 11_entry_activities_all   # A.3 O1 CAPTURE
.venv/bin/alphalens broker orders                                   # A.4 -> expect 2 working SELL children, Amount=2
# raw children rows: client.get_open_orders() payload -> $SCRATCH/12_open_orders_post_fill.json
.venv/bin/python /tmp/first_fill/dump_activities.py <tp_child_id> --all --out-name 13_child_tp_activities
.venv/bin/python /tmp/first_fill/dump_activities.py <sl_child_id> --all --out-name 13_child_sl_activities
.venv/bin/alphalens broker positions                                # A.5 -> $SCRATCH/14_positions.json (raw via get_positions)
.venv/bin/alphalens broker reconcile --json > "$SCRATCH/15_reconcile_post_A.json"   # A.6 checkpoint verdict
```

**A.6 DECISION:** verdict `FILLED` ⇒ parser's fill fields verified on real
data. Verdict `UNRESOLVED(fill_fields_unverified)` ⇒ the raw row in
`11_*.json` shows exactly which field deviates — that IS the O1 finding;
continue regardless (position management does not depend on the verdict).

### Phase B — child-cancel probe (O2) — position stays stop-protected

```bash
.venv/bin/alphalens broker cancel <tp_child_id>    # B.1 cancel the TP child, NOT the stop
# (ignore the CLI's unconditional entry-cascade message — cosmetic, G4)
.venv/bin/alphalens broker orders                  # B.2 IMMEDIATELY -> $SCRATCH/20_orders_post_child_cancel.json
.venv/bin/python /tmp/first_fill/dump_activities.py <tp_child_id> --all --out-name 21_child_tp_post_cancel
.venv/bin/python /tmp/first_fill/dump_activities.py <sl_child_id> --all --out-name 21_child_sl_post_cancel
```

**B.2 DECISION (three branches):**

| Branch | Observation | Action |
|---|---|---|
| (a) expected | sibling STOP still Working (OCO cancels on EXECUTION only) | record finding; `broker cancel <sl_child_id>`; confirm both Cancelled/Confirmed → `$SCRATCH/22_*`; position now naked → Phase C within 2 min |
| (b) cascade | sibling ALSO Cancelled | record finding; position naked → Phase C immediately |
| (c) weird | child stuck Cancelled/Requested, unrecognized pair, child reappears | capture everything raw, do NOT debug live → Phase C (closing is safe under every branch), then universal cleanup sweep |

### Phase C — manual opposite close (O3): naked SELL limit

```bash
# C.1 re-fetch ref price (§2 rule)
.venv/bin/python /tmp/first_fill/step_c_close.py --qty 2 --limit <round(ref*0.99,2)>
```

**C.1 CONTINGENCY** — Saxo REJECTS the childless None/None body (acceptance
is live-unverified even after the G1 unit test): capture the rejection
payload (`$SCRATCH/30_close_place.json` carries it), then close stop-only
(tested path): `step_c_close.py --qty 2 --limit <same> --stop
<round(entry*1.03,2)>` — stop ABOVE entry is valid SELL geometry; after it
fills the orphan StopIfTraded child MUST be deleted immediately (it would
OPEN a short later): `broker cancel <orphan_id>`, confirm Cancelled/Confirmed.

```bash
.venv/bin/python /tmp/first_fill/dump_activities.py <close_order_id> --all --out-name 31_close_activities  # C.2 O1 sample #2 (SELL)
# C.3 netting verification, branched on Phase-0 profile:
#   FifoRealTime/AverageRealTime: poll positions + broker.get_closed_position_rows() every 30s up to 5 min
#     -> expect positions empty AND a closedpositions FIFO pair -> $SCRATCH/32_closedpositions.json
#     (record ClosePrice, ProfitLossOnTrade, WHICH ExternalReference — resolves Q3/Q4)
#   FifoEndOfDay: expect BOTH offsetting positions open (Square) + closedpositions EMPTY until EOD
#     -> documented behavior, NOT a failure -> $SCRATCH/32_positions_square.json; schedule C.5
.venv/bin/alphalens broker reconcile --json > "$SCRATCH/33_reconcile_post_C.json"   # C.4
# C.5 (EOD-netting branch only) next US morning: reconcile --json + closedpositions dump -> $SCRATCH/40_nextday_*
```

**C.4 EXPECT:** entry bracket → `FILLED(closed)` with `realized_r` computed
(journal row HAS a stop); close row → `FILLED` with `r=None` (stop=None;
`compute_realized_r` honest-None by design). Any UNRESOLVED ⇒ the raw
activities files explain it.

### Phase D — best-effort partial fill (O4) — only if >= 45 min before the 15:00 ET cutoff

OpenAPI SIM offers NO documented deterministic partial-fill trigger (no L2,
no depth-coupled engine; SAFT quantity tables are FIX-only). Cheap
opportunistic probe, not a guarantee.

```bash
.venv/bin/python /tmp/first_fill/step_a_entry.py --qty 10 --entry <round(ref*0.999,2)> --naked \
    --note "first-fill experiment phase D partial probe" --out-name 50_partial_probe_place
# D.2 watch 10 min: every 2 min ->
.venv/bin/python /tmp/first_fill/dump_activities.py <probe_order_id> --all --out-name 50_partial_probe_t<N>
# any Status=Fill (non-final) row = jackpot: archive immediately, note FillAmount vs FilledAmount increments
```

**D.3 branches:** (a) fills fully → long 10 → `step_c_close.py --qty 10
--limit <ref*0.99>` (Phase C pattern); (b) partial → let run ≤10 min, then
`broker cancel <id>`, close whatever position exists with MATCHING qty (read
from positions view, never assumed); (c) no fill → `broker cancel <id>`,
confirm Cancelled/Confirmed.

**D.4** if no partial observed (likely): record verdict "not provokable
in-session; opportunistic capture doctrine armed" — any future SIM order
showing Fill-before-FinalFill gets its `EntryType=All` payload archived to
this experiments dir; the multi-fill parser path is covered by unit tests
whose fixture rows are byte-shaped from `$SCRATCH/11_*` (real FinalFill) +
documented Fill semantics (`FillAmount`=last increment,
`FilledAmount`=cumulative; ENS example FillAmount=4 / FilledAmount=10
completes a 10-lot). Optional follow-up (out of attended scope): far resting
GTD limit across sessions (Saxo guarantees OrderId persistence for partially
filled orders) + email openapisupport@saxobank.com about SIM partial
scenarios.

### End-of-session gate (mandatory, every run incl. aborts)

```bash
.venv/bin/alphalens broker orders        # E.1 MUST be empty (else cancel each survivor — orphans can OPEN positions later)
.venv/bin/alphalens broker positions     # E.2 empty (or documented Square pair under EOD netting)
.venv/bin/alphalens broker reconcile --json > "$SCRATCH/60_final_reconcile.json"
.venv/bin/alphalens broker account       # E.3 -> snapshot to $SCRATCH/60_final_account.json
cp ~/.alphalens/broker_orders/submissions.jsonl "$SCRATCH/61_submissions_copy.jsonl"
unset ALPHALENS_BROKER_ALLOW_ORDERS
# E.4 write the findings into THIS memo from $SCRATCH files only (no numbers from memory)
```

## 4. Observation checklist (fill in during the session)

### 4.1 Preflight

| Item | File | Finding |
|---|---|---|
| `PositionNettingProfile` (verbatim) | `01_client_profile.json` | _(fill)_ |
| infoprices Quote non-null during RTH? (timestamp + FieldGroups) | `00_infoprice.json` | _(fill)_ |

### 4.2 O1 — entry FinalFill row (EntryType=All, UNTRUNCATED)

| Field | Present? | Type | Value | Notes |
|---|---|---|---|---|
| `FilledAmount` | | | | expect ==2 |
| `FillAmount` | | | | |
| `ExecutionPrice` | | | | |
| `AveragePrice` | | | | |
| `ExternalReference` | | | | == client_request_id? |
| `Status=Fill` row BEFORE FinalFill? | | — | | one-shot fills typically emit FinalFill directly |
| LogId ordering / ActivityTime | | — | | |

Same table for the SELL close fill (`31_close_activities.json`): _(fill)_

### 4.3 O2 — children + child-cancel probe

| Item | File | Finding |
|---|---|---|
| Children post-entry-fill: Status/BuySell/Amount/Duration/OCO linkage fields | `12_open_orders_post_fill.json` | _(fill)_ |
| Children emit Placed/Confirmed rows at placement or on entry-fill activation? | `13_child_*_activities.json` | _(fill)_ |
| Sibling STOP after TP DELETE: Working / Cancelled / weird | `20_*`, `21_*` | _(fill)_ |
| Terminal Cancelled/Confirmed rows for every cancelled id | `21_*`, `22_*` | _(fill)_ |

### 4.4 O3 — netting + reconcile

| Item | File | Finding |
|---|---|---|
| SELL netted the long vs offsetting short (positions at +0/+1/+5 min) | `32_*` | _(fill)_ |
| closedpositions row: ClosePrice / OpenPrice / ProfitLossOnTrade | `32_closedpositions.json` | _(fill)_ |
| WHICH ExternalReference on the pair row (entry's vs close's) | `32_closedpositions.json` | _(fill)_ |
| Timing vs netting-profile prediction (immediate vs next-day) | `32_*`, `40_*` | _(fill)_ |
| reconcile post-A: entry verdict + raw_status | `15_reconcile_post_A.json` | _(fill)_ |
| reconcile post-C: `FILLED(closed r=…)` entry / `FILLED` close `r=None` | `33_reconcile_post_C.json` | _(fill)_ |

### 4.5 O4/O5 — probe + latencies

| Item | Finding |
|---|---|
| Phase D probe outcome (full/partial/none); FillAmount vs FilledAmount arithmetic vs ENS example | _(fill)_ |
| Latency: place → Working visible in port/v1 | _(fill)_ |
| Latency: place → FinalFill in audit | _(fill)_ |
| Latency: fill → position row; close-fill → closedpositions row | _(fill)_ |

## 5. Cleanup decision tree

**UNIVERSAL ABORT SWEEP** (runnable at ANY point, any branch):

1. `alphalens broker orders` → UNFILLED entry: DELETE the ENTRY id only
   (`broker cancel <entry_id>`) — the documented entry→children cascade
   cleans the whole bracket; do NOT delete children first.
2. `alphalens broker positions` → any open position: naked marketable
   opposite limit (`step_c_close.py` pattern; qty = EXACT position qty;
   limit = `ref*0.99` closing a long / `ref*1.01` closing a short).
   `ALPHALENS_BROKER_ALLOW_ORDERS` stays exported until this succeeds
   (cancel is ungated, placement is not).
3. Re-poll `broker orders`; DELETE every surviving child/orphan one id at a
   time, confirming each reaches Cancelled/Confirmed via
   `dump_activities.py` — an orphan protective order left working can OPEN a
   new position after the account looks flat.
4. Verify flat: positions empty (or Square under FifoEndOfDay) AND orders
   empty; snapshot to `$SCRATCH`.

**Branch rules:**

- **Entry-filled-but-children-weird:** close the POSITION first (naked SELL,
  Phase C pattern), debug never — then enumerate every order id seen this
  session and cancel any non-terminal one. Rationale: a naked long in KO
  qty<=10 on SIM is bounded risk; a mismanaged working order is
  unbounded-later risk.
- **Naked None/None close REJECTED:** fall back to the stop-only SELL (stop
  above entry, tp=None); the moment it fills, immediately DELETE the orphan
  stop child and confirm Cancelled/Confirmed.
- **Manual-close caveat (every close):** Saxo documents auto-cancel of
  related orders on external close as "can", not "will" — after ANY close
  fill, always re-poll open orders and cancel survivors; never trust
  auto-cleanup.
- **OCO-sibling unknown (Phase B branch (c)):** treat both child ids as live
  until each shows Cancelled/Confirmed in the audit log; if a DELETE keeps
  failing, close the position anyway (flat position + stray protective order
  is recoverable; the reverse is not) and retry cancels until terminal.
- **FifoEndOfDay:** intraday "flat" = NetPosition Square (long 2 + short 2)
  with ZERO working orders — max achievable same-day flatness under EOD
  netting (related orders all cancelled, nothing blocks the nightly net).
  MANDATORY next-US-morning verification: positions empty + closedpositions
  pair + reconcile re-run; the experiment is not closed out until then.
- **Mid-session auth death:** cancel is ungated but still needs a live token
  — run `alphalens broker auth --refresh` (or full `broker auth`) BEFORE the
  sweep; the 15:45 ET cleanup deadline exists precisely so RTH remains to
  recover auth and still flatten.
- **Partial-fill probe:** the qty-10 order is NAKED so cleanup is exactly
  one DELETE; a partial leaves a position whose qty is READ from the
  positions view (never assumed) and closed with a matching-qty naked
  opposite limit.
- **Session hard-stop:** no new placements after 15:00 ET; anything
  unresolved at 15:30 ET → skip remaining objectives, run the universal
  sweep — objectives are re-runnable another day, an un-flat account
  overnight is not acceptable.

## 6. Success criteria

- **O1:** ≥1 real FinalFill audit row archived raw; "does FilledAmount
  parse?" answered definitively — `broker reconcile --json` returns FILLED
  (not `UNRESOLVED(fill_fields_unverified)`) for the Phase-A bracket, OR a
  documented raw row shows exactly which field deviates (either outcome
  unblocks the parser with evidence).
- **O1b:** Fill-vs-FinalFill sequencing answered for a one-shot SIM fill,
  with LogId-ordered rows as evidence.
- **O2:** children observed post-entry-fill with raw rows, and the
  child-cancel question answered with evidence (sibling state captured
  within seconds of the TP DELETE) — "no cascade" confirmed or refuted;
  finding phrased as a doctrine line for `broker.py`/`client.py` docstrings.
- **O3:** full cycle demonstrated: journal row → FILLED verdict → manual
  opposite close → position netted (immediately or at EOD per the recorded
  profile) → closedpositions FIFO pair archived → final reconcile shows
  `FILLED(closed)` with `realized_r` for the entry record and `None` for
  the stop=None close record.
- **O4:** either a real partial-fill row captured (jackpot), or this memo
  states plainly "not provokable via OpenAPI SIM" with the mechanics
  citations, the qty-10 probe result as the empirical data point, and the
  opportunistic-capture doctrine + synthetic-fixture plan in place.
- **SAFETY:** account verifiably flat at session end (positions empty or
  documented Square-pending-EOD with next-morning verification completed)
  AND zero working orders — confirmed by raw snapshots, not assumed; no
  orphan protective order ever left working unattended.
- **ARTIFACTS:** every checklist item exists as a raw JSON file in
  `$SCRATCH`; this memo's §4 tables filled from those files only (no numbers
  from memory); journal lines in
  `~/.alphalens/broker_orders/submissions.jsonl` reconcile cleanly.
- **NO raw HTTP writes anywhere:** placements via
  `SaxoBroker.place_bracket_order`, cancels via `broker cancel`, raw reads
  via `get_order_activities` / `get_json` — house rule intact.

## 7. Code gaps closed pre-session (this branch)

- **G1 (blocking, closed):** unit coverage for the naked None/None path in
  `SaxoBroker._build_bracket_body` —
  `tests/brokers/test_saxo_broker_orders.py::TestNakedNoneNoneBracketBody`
  pins: no `Orders` key, plain Limit entry, StopIfTraded capability check
  skipped without a stop, relation validator receives `(None, None)`, empty
  `exit_order_ids` on the response. The path was structurally supported but
  zero-covered; the Phase-C close is its first consumer.
- **G2 (blocking, closed):** attended driver scripts
  `apps/alphalens-research/scripts/first_fill/{step_a_entry, step_c_close,
  dump_activities, read_netting_profile}.py` (+ `_common.py`) — thin
  compositions over the gated wrappers; every placement journals via
  `build_submission_record`/`append_submission_record` so `broker reconcile`
  stays the verdict engine; every payload dumps to `$SCRATCH`. Hermetic
  tests: `tests/brokers/test_first_fill_scripts.py`. The durable
  `broker submit-manual` CLI decision stays DEFERRED per protocol.
- **G3 (nice-to-have, open):** `broker activities <order_id> --json` CLI —
  `dump_activities.py` covers this session.
- **G4 (cosmetic, open, post-experiment):** update the `broker cancel`
  cascade message + docstrings with the evidenced child-DELETE behavior
  after Phase B resolves it.
- **G5 (post-experiment, gated on O1):** if the real FinalFill row parses,
  add a fixture test byte-shaped from `$SCRATCH/11_entry_activities_all.json`
  (retiring "doc-sourced only" in the `broker.py:381` comment); if not, fix
  `_classify_activity_row` against the real shape. Either way one small,
  evidence-carrying PR.

## 8. Open questions (answered by this session)

1. Does Saxo SIM ACCEPT the childless None/None "bracket" body at all?
   (G1 proves our body shape; live acceptance is C.1's first decision point.)
2. DELETE on a child id: sibling survives / cascades / weird state? (Phase B.)
3. Does a SELL naked limit against an open long NET the position (FIFO) or
   open an offsetting short per the `PositionNettingProfile` — and what IS
   this account's profile? (Phase 0.4 reads it; C.3 observes.)
4. Which order's `ExternalReference` lands on the closedpositions row —
   opener's, closer's, or both? (Determines reconcile's close attribution.)
5. Does `/trade/v1/infoprices` return non-null delayed quotes during US RTH
   with `FieldGroups=Quote`, or is yfinance the permanent SIM ref-price
   fallback?
6. Does the OpenAPI SIM fill engine share the FIX SAFT quantity-keyed
   scenario tables? (Phase D is one weak data point; definitive answer
   likely needs openapisupport@saxobank.com.)
7. Do bracket children emit their own Placed/Confirmed audit rows at
   placement time or only upon entry-fill activation? (A.4 child dumps.)
8. Does the ±5%-from-ENTRY child-distance rule (`TooFarFromEntryOrder`) bind
   only at placement, or also on a later PATCH? (NOT exercised this session;
   relevant only if a future protocol adopts PATCH-TP-to-market closes,
   which would also need a new gated PATCH wrapper.)
9. Same-day GTD (`entry_ttl_days=0`) acceptance remains unverified — this
   protocol deliberately uses ttl=1 everywhere.

## 9. Findings (post-session)

_(empty until the session runs — fill §4 tables + this narrative from
`$SCRATCH` files only)_
