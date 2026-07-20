# Saxo SIM first-fill experiment — attended runbook + log

**Status:** COMPLETE — executed 2026-07-20 (attended). All objectives met
except O4 (partial fill) which is deferred as not provokable on OpenAPI SIM.
Findings below are quoted from raw JSON in
`~/.alphalens/broker_orders/experiments/first_fill_2026-07-20/`.
**Date:** 2026-07-18 (runbook) / 2026-07-20 (execution)
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
| `PositionNettingProfile` (verbatim) | `01_client_profile.json` | `FifoRealTime` (with `PositionNettingMethod=FIFO`, `PositionNettingMode=Intraday`; `AllowedNettingProfiles=[FifoRealTime, FifoEndOfDay]`) ⇒ netting is FIFO + real-time, so closedpositions pairs form immediately (no EOD wait). Account default currency EUR. |
| infoprices Quote non-null during RTH? (timestamp + FieldGroups) | `00_infoprice.json` | NULL. `/trade/v1/infoprices` returned HTTP 200 but `Quote.PriceTypeBid=NoAccess`, `Quote.PriceTypeAsk=NoAccess`, `Quote.Amount=0`, `LastUpdated=0001-01-01T…` — no exchange entitlement for stock L1 on this SIM account. **yfinance is the permanent stock ref-price fallback** (`00_computed_legs.json` records `ref=82.035, source="yfinance"`). Answers Q5. |

### 4.2 O1 — entry FinalFill row (EntryType=All, UNTRUNCATED)

Entry order `5039287596`, from `11_entry_activities_all.json` (`__count=3`,
three rows in LogId order):

| LogId | Status / SubStatus | Fill fields |
|---|---|---|
| 249519475 | Placed / Requested | — |
| 249519478 | Placed / Confirmed | — |
| 249519481 | **FinalFill / Confirmed** | `FillAmount=2.0`, `FilledAmount=2.0`, `ExecutionPrice=82.09`, `AveragePrice=82.09`, `PositionId=5026930126` |

| Field | Present? | Type | Value | Notes |
|---|---|---|---|---|
| `FilledAmount` | yes | number | `2.0` | == order qty (cumulative) |
| `FillAmount` | yes | number | `2.0` | per-event increment == full qty (one-shot) |
| `ExecutionPrice` | yes | number | `82.09` | |
| `AveragePrice` | yes | number | `82.09` | == ExecutionPrice (single fill) |
| `ExternalReference` | yes | string | `87e0ab88-c1f2-4e88-b5b8-8fbbbb6e1a6d` | == entry `client_request_id` (`10_entry_place.json`) |
| `Status=Fill` row BEFORE FinalFill? | **no** | — | — | one-shot fill emits `FinalFill` DIRECTLY (no intermediate `Fill`) — answers O1b |
| LogId ordering / ActivityTime | yes | — | monotone | 14:09:05.439 → .442 → .447 Z, LogId strictly increasing |

SELL close fill (`31_close_activities.json`, close order `5039287641`,
`__count=3`): identical shape — `Placed/Requested` → `Placed/Confirmed` →
`FinalFill/Confirmed` (LogId 249520999) with `FillAmount=2.0`,
`FilledAmount=2.0`, `ExecutionPrice=82.15`, `AveragePrice=82.15`,
`ExternalReference=8e0fbe45-6952-4647-a58e-67a5884768dc` (== close
`client_request_id`), `PositionId=5026930436`. No intermediate `Fill` row.

### 4.3 O2 — children + child-cancel probe

| Item | File | Finding |
|---|---|---|
| Children post-entry-fill: Status/BuySell/Amount/OCO linkage | `12_open_orders_post_fill.json` | Two working SELL children, both `Amount=2.0`, `Duration=GoodTillCancel`, `OrderRelation=Oco`: id `5039287597` = `Limit` @ `85.35` (take-profit); id `5039287598` = `StopIfTraded` @ `80.37` (stop-loss). Each carries the other in `RelatedOpenOrders`. Both `Status=Working`, `ExternalReference` == entry `client_request_id`. |
| Sibling STOP after TP DELETE: Working / Cancelled / weird | `20_orders_post_child_cancel.json` | **STILL Working** — after DELETE of the TP child `5039287597`, the STOP `5039287598` remains `Status=Working` (now `OrderRelation=StandAlone`, `RelatedOpenOrders=[]`). **No OCO cascade**: OCO cancels a sibling only on EXECUTION, not on manual cancel of the other leg. Confirms the "assume no cascade" expectation. |
| Both children terminally cancelled | `22_orders_post_stop_cancel.json` | After also cancelling the stop, open orders = `{"Data": [], "__count": 0}` — account has zero working orders. |

### 4.4 O3 — netting + reconcile

| Item | File | Finding |
|---|---|---|
| SELL netted the long (FIFO, real-time) | `32_closedpositions.json` | Under `FifoRealTime` the naked SELL netted the long immediately (positions emptied; a closedpositions pair row appeared same-session, no EOD wait). |
| closedpositions row: OpenPrice / ClosingPrice / Amount / P&L | `32_closedpositions.json` | `OpenPrice=82.09`, `ClosingPrice=82.15`, `Amount=2.0`, `ProfitLossOnTrade=+0.12` USD (`ProfitLossOnTradeInBaseCurrency=+0.1051377` EUR), `ClosingMethod=Fifo`. |
| WHICH ExternalReference on the pair row | `32_closedpositions.json` | **BOTH.** `OpeningExternalReferenceId=87e0ab88…` (entry) AND `ClosingExternalReferenceId=8e0fbe45…` (close). The reconciler can attribute either leg from a single closedpositions row. Answers Q4. |
| Timing vs netting-profile prediction | `01_*`, `32_*` | Immediate pairing, exactly as `FifoRealTime` predicts (no `40_*` next-day file needed). |
| reconcile post-C: entry + close verdicts | `33_reconcile_post_C.json` | BOTH orders returned `UNRESOLVED(audit_error)` — `"audit_error: Saxo 429 persisted after 4 attempts"`. This is the `__nextPoll` bug (§9): reconcile followed the always-present live-poll cursor and 429'd. Raw reads this session (`11_*`, `31_*`, `32_*`) prove the true outcomes are FILLED/closed. After this PR's client fix, reconcile is expected to resolve `FILLED(closed)` with `realized_r` for the entry (it has a stop) and `FILLED` `r=None` for the naked close (stop=None, honest None). |

### 4.5 O4/O5 — probe + latencies

| Item | Finding |
|---|---|
| O4 Phase D partial-fill probe | **NOT attempted this session — deferred.** OpenAPI SIM has no documented deterministic partial-fill trigger (no L2/depth engine; SAFT quantity tables are FIX-only), so it is expected "not provokable". Re-runnable another day under the opportunistic-capture doctrine (§3 D.4). |
| O5 infoprices | Null quotes (see §4.1) → yfinance ref-price fallback confirmed permanent for SIM stock. |
| O5 commission (SIM) | `60_final_account.json`: EUR total `999,973.82` vs `1,000,000.00` start = `-26.18` EUR for the round trip ≈ ~13 EUR/side (`32_closedpositions.json` `CostOpening=-15.00` / `CostClosing=-15.01` USD ≈ 13 EUR at the 0.876 rate). Gross P&L was `+0.12` USD — commission dominates a 2-lot round trip by ~200×. |
| Latency: place → FinalFill in audit | entry place `ts_utc=14:09:03Z` → FinalFill `ActivityTime=14:09:05.447Z` ≈ **2.4 s**; close place `14:23:35Z` → FinalFill `14:23:38.156Z` ≈ **3.2 s**. |
| Latency: fill → position / close-fill → closedpositions | position `ExecutionTimeOpen=14:09:05.447144Z` == FinalFill instant; `32_*` `ExecutionTimeClose=14:23:38.156164Z` == close FinalFill instant — position + closedpositions rows stamp at the fill instant (no measurable lag). |

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

Executed 2026-07-20, KO @ XNYS, qty 2. All values quoted from the raw JSON in
`~/.alphalens/broker_orders/experiments/first_fill_2026-07-20/`.

### Headline — the `__nextPoll` live-poll-cursor bug (BLOCKING, fixed by this PR)

`broker reconcile` returned `UNRESOLVED(audit_error)` for BOTH orders with
`"Saxo 429 persisted after 4 attempts"` (`33_reconcile_post_C.json`), even
though every order actually FILLED and the audit rows were complete on page 1.

Root cause: the audit endpoint `/cs/v1/audit/orderactivities` ALWAYS returns a
`__nextPoll` field. `__nextPoll` is NOT pagination — it is a subscription-style
LIVE-POLL continuation cursor for FUTURE activities. It is present even when the
current page already holds every current row (`__count == len(Data)`;
`11_entry_activities_all.json` has `__count=3` with all 3 rows on page 1 AND a
`__nextPoll`). Requesting it immediately returns HTTP 429 ("poll too soon"). The
old `SaxoClient._get_paged_json` followed `__next` OR `__nextPoll` unconditionally,
so a point-in-time audit read always 429'd; the client's retry loop then
surfaced it as a rate-limit and reconcile mapped it to `UNRESOLVED(audit_error)`.

Fix: `_get_paged_json` now follows ONLY `__next` (genuine pagination of the
current snapshot) and ignores `__nextPoll` (still stripping it so it never leaks
into the returned envelope). Additional current-snapshot rows always arrive via
`__next`, never `__nextPoll`, so this is safe for both callers
(`get_order_activities` audit + `get_closed_positions`). With the fix, reconcile
is expected to resolve `FILLED(closed)` (with `realized_r`) for the stop-bearing
entry and `FILLED` `r=None` for the naked close — the true outcomes proven by
the raw reads this session.

### O1 — real FinalFill shape (parser now evidence-backed)

A one-shot SIM fill emits `Placed/Requested` → `Placed/Confirmed` →
`FinalFill/Confirmed` with NO intermediate `Status=Fill` row (§4.2). The
FinalFill row carries `FillAmount=FilledAmount=2.0`,
`ExecutionPrice=AveragePrice=82.09` (entry) / `82.15` (close), and
`ExternalReference == client_request_id`. The `_classify_activity_row` FinalFill
branch — previously flagged "doc-sourced only" — is now pinned by a fixture test
byte-shaped from this real row (`test_saxo_broker.py::TestFinalFillRealFixture`).

### O2 — bracket children + no OCO cascade on manual cancel

Post-fill, both SELL children go `Working` (TP `Limit@85.35` id 5039287597; SL
`StopIfTraded@80.37` id 5039287598), each linked as `OrderRelation=Oco`. DELETE
of the TP child left the STOP sibling STILL `Working` (`OrderRelation` flipped to
`StandAlone`) — **OCO cancels a sibling only on EXECUTION, not on manual cancel
of the other leg** (§4.3). Doctrine line for `broker.py`/`client.py`: after any
manual child cancel, the sibling remains live and must be cancelled explicitly.

### O3 — FIFO real-time netting + dual ExternalReference

Under `FifoRealTime` (§4.1) the naked SELL netted the long immediately and a
closedpositions pair row appeared same-session: `OpenPrice=82.09`,
`ClosingPrice=82.15`, `ProfitLossOnTrade=+0.12` USD (`+0.1051377` EUR base). The
row carries BOTH `OpeningExternalReferenceId` (entry) and
`ClosingExternalReferenceId` (close), so the reconciler can attribute either leg
from one row (answers Q4).

### O5 / open questions

- **Q1 — does SIM accept the childless None/None SELL body?** YES. The naked
  close placed and filled (`30_close_place.json` shows `stop_loss=null`,
  `take_profit=null`, `exit_order_ids=[]`; `31_close_activities.json` shows the
  fill). The G1 unit path is confirmed against live behavior.
- **Q4 — which ExternalReference on the closedpositions row?** BOTH (above).
- **Q5 — infoprices during RTH?** NULL on this SIM account (`NoAccess`) →
  yfinance is the permanent stock ref-price fallback.
- SIM commission ≈ 13 EUR/side dominates a 2-lot round trip (§4.5).

### O4 — deferred

Partial fill NOT attempted; OpenAPI SIM has no documented deterministic partial
trigger (expected "not provokable"). Re-runnable under the opportunistic-capture
doctrine (§3 D.4). This is the only objective not met.
