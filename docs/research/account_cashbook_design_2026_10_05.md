# Design: the account cash book — what the money actually did (#1689)

Status: DRAFT r2, 2026-10-06. r2 adds §1.6, the results of the Phase 0 LIVE probes run GET-only on 2026-10-05, and settles three of the four open questions in §8. Still DRAFT, not LOCKED: probe 3 has not run, and §1.6 records four caveats that the probes did not close.

Code facts in §1 come from `origin/main` at `96d32966` and were re-checked against `7b1927a6`. Every broker number in §1 was measured **offline**, on the redacted LIVE capture committed at `apps/alphalens-research/tests/brokers/automanager/fixtures/trades/live_2026_10_03/venue.json` (read GET-only on 2026-10-03: 249 audit rows, 41 trades-report rows, 104 bookings rows). §1.6 is the only section built from live reads.

## 0. What issue #1689 asks for, and what already exists

#1689 was filed on 2026-10-03 from the architecture audit's context-mapping pass. Its finding: the realized outcome of the trades the owner actually took exists nowhere in this repo, and `/edge` does not answer it, because `/edge` measures the candidate population — the quality of the machine's selection — and never how the owner's money is doing.

`alphalens broker trades` (#1701, PR #1703) merged on 2026-10-04, one day after the issue's last comment. It changes the picture, so the first job of this memo is to say by how much.

| What #1689 asks for | State at `96d32966` |
|---|---|
| A read of the account's own history, behind the adapter | **Built.** `list_fill_history` reads `/cs/v1/audit/orderactivities`, `/cs/v1/reports/trades` and `/cs/v1/reports/bookings` (`brokers/saxo/broker.py:1645-1716`) |
| Real account money, not money derived from prices | **Built.** `outcome.pnl_cash_acct` and `notional_spent_acct` come from the `Share Amount` booking's `AmountAccountCurrency` (`brokers/automanager/trades.py:2502-2523`) |
| Positions closed by hand covered | **Built.** A closing order with no `ExternalReference` resolves to ownership kind `manual` and the exit carries `reason: "manual_close"`, attributed by `RelatedPositionId` or by FIFO with a warning (`trades.py:1660-1661`, `:2049-2051`, `:1882-1941`) |
| The source identified | **Built.** Probed read-only on LIVE on 2026-10-03; results published in `broker_trades_command_design_2026_10_03.md` §0 |
| A net figure | **Missing, and one vendor field away** (§1.2, §7 Phase 1) |
| A store | **Missing entirely.** `build_trades`'s only caller is the CLI; `state_paths.py:120-178` declares no path for it; no unit runs it |
| Coverage rooted in the account rather than in the picks | **Missing.** `if not lots: continue` (`trades.py:2932-2933`) drops any instrument no pick opened |
| A period total and a cross-check against the account | **Missing** |

So the honest fraction is: **the per-pick question is about two thirds answered; the account question is not started.** Two sentences of the issue are now false as written, and §10 lists the corrections it needs.

## 1. What was measured, offline, before any design

### 1.1 The cash book is complete by construction

Every booking row is a cash movement in the account's own currency. Over the captured window:

| `BkAmountType` | rows | sum `AmountAccountCurrency` (PLN) |
|---|---|---|
| `Share Amount` | 41 | −3 110.06 |
| `Commission` | 41 | −154.41 |
| `Exchange Fee` | 17 | −0.90 |
| `Cash Amount` (deposits) | 3 | +24 000.00 |
| `Corporate Actions - Cash Dividends` | 1 | +4.74 |
| `Corporate Actions - Withholding Tax` | 1 | −0.69 |
| **all rows** | **104** | **+20 738.68** |

`BkAmountId` is present and unique on 104 of 104 rows, so the store's key needs no network call to settle. There are exactly 41 `Share Amount` rows for exactly 41 trades-report rows: every execution has its cash leg. `AccountCurrency` is PLN on all 104 rows.

**One trap, measured.** The three deposits carry *non-zero* `RelatedTradeId` values (6820472599, 6833231458, 6849524900) that are not trade ids. A classifier keying on "non-zero means a trade" folds 24 000 PLN of funding into trading P&L. Classify on `BkAmountType` first, always.

### 1.2 The vendor states the net per execution, and the repo does not read it

`BookedAmountAccountCurrency` on a trades-report row:

| Check | Result |
|---|---|
| Rows where the field is null | 0 of 41 |
| Rows where it differs from `Share Amount + Commission + Exchange Fee` in the account currency | **0 of 41** |
| Rows where it equals `Share Amount` alone | 0 of 41 |
| Sum of `BookedAmountAccountCurrency` | −3 265.37 PLN |
| Sum of share + commission + exchange fee, in the account currency | −3 265.37 PLN |

The field is on a row the tree already reads and is not mapped: `Execution.from_vendor_row` takes 7 of the 39 fields on the row and leaves this one out (`brokers/fill_history.py:146-156`).

Honest limit on this evidence: one account, one window, 41 rows, all `Venue: Exchange`, `AccountCurrency: PLN`, 40 Market and 1 Limit. It does not prove Saxo books it this way for financing, a CFD or a corporate action. It is 41 of 41 measured, which is why Phase 1 publishes the identity as a pinned test rather than as an assumption.

### 1.3 The FX conversion charge is inside the account amount, via the rate

This was the design's self-declared largest risk, and it is settleable offline. Three measurements:

1. `AmountAccountCurrency == Amount × ConversionRate` on **41 of 41** `Share Amount` rows (to within 0.02 PLN).
2. `ConversionRateAccountCurrency` is **≤ 0 on all 41 rows** — on all 17 buy legs and all 24 sell legs alike. A signed component of the booked amount would flip sign with direction; a cost does not. It is 0.250 % of the account amount (range 0.248–0.254 %).
3. **The cash book contains no FX row at all.** The six `BkAmountType` values in §1.1 are the complete set, and none of them is a conversion charge.

(3) is the decisive one. If the 0.25 % were charged on top of a mid-market rate, it would have to appear as a cash movement somewhere, and it does not. So the charge is inside the rate the venue books the leg at, and `ConversionRateAccountCurrency` is a *disclosure* of how much of that rate was markup.

Two rules follow, and both are load-bearing for any net figure:

- **`fx_conversion` must never be added into a net.** It is already inside `pnl_cash_acct` through the rate. Adding it double counts, at 0.25 % of notional per leg.
- The vendor's own net (`BookedAmountAccountCurrency`) excludes the FX disclosure and is net of commission and exchange fee only. That is the net this layer publishes, and §6 says so on the number.

This corrects the earlier record in one direction: `broker_trades_command_design_2026_10_03.md:402` reached the right conclusion from arithmetic on 25 rows with Saxo's documentation unconsulted, and §1.3 is the measurement it lacked. The reading now rests on the absence of an FX booking type, which is a stronger fact than the arithmetic.

### 1.4 The existing capability cannot be the cash book

`list_fill_history` keeps only bookings whose `related_trade_id` is in the trades it kept (`brokers/saxo/broker.py:1701-1710`). On the captured window that **drops 5 of 104 rows worth 24 004.05 PLN**:

| Dropped row | `AmountAccountCurrency` | `RelatedTradeId` |
|---|---|---|
| `Cash Amount` | +2 000.00 | 6820472599 |
| `Cash Amount` | +7 000.00 | 6833231458 |
| `Cash Amount` | +15 000.00 | 6849524900 |
| `Corporate Actions - Withholding Tax` | −0.69 | 0 |
| `Corporate Actions - Cash Dividends` | +4.74 | 0 |

`CostBooking` also does not map `BkAmountId`, `Uic`, `InstrumentSymbol`, `ValueDate` or `AmountClass`. The capability is fill-shaped by construction and correctly so — the reconcile loop it serves only needs fills. The cash book needs a second capability reading the same endpoint **unfiltered**, not a widening of this one.

### 1.5 Episodes derive from the account's own arithmetic

For one `Uic`, the span over which the cumulative signed `Amount` from the trades report leaves zero and returns to zero. Measured over 2026-08-01..2026-10-03: **15 of 18 instruments return to flat**, and the 3 that do not are genuinely still open (RHI +3, TNGX +20, NESR +13).

Two mechanics worth recording. The sign comes from `Amount`: `Direction` is the literal string `'None'` on all 41 rows, so a reader that branches on it gets nothing. And the venue states open-or-close per row — `ToOpenOrClose` is populated on all 41 rows (24 `ToOpen`, 17 `ToClose`) and is already mapped at `fill_history.py:142` — but it pairs nothing: exactly 1 of 41 rows carries a `RelatedPositionId`, because the account nets at end of day. So pairing stays FIFO and `ToOpenOrClose` becomes an independent check on it, per the repo's rule that a key quantity is recounted by a second path before a gate trusts it.

## 1.6 Phase 0 probes (run 2026-10-05 on LIVE, GET-only, results)

Run on the VPS, not on the Mac: the LIVE token store has a documented single-refresher invariant, so a LIVE read from another host would damage the daemon's auth. Attended, under the recipe in `reference_live_oneoff_cli_needs_rails_and_envfile_2026_08_28` with `ALPHALENS_BROKER_ALLOW_ORDERS=0`. Only GETs were issued. ADR 0015's attended unlock covers an attended probe and ADR 0017 preserves it verbatim for exactly this, so no new authorization was needed (§8.3).

The process logs `SAXO LIVE ORDER RAIL UNLOCKED … real-money orders are now possible in THIS process`. That is the grant verifying, not a warning about something that happened.

### P1. The account's own balance separates cash from positions, exactly

| `/port/v1/balances` field | Value |
|---|---|
| `Currency` | PLN |
| `CashBalance` | 20 738.68 |
| `NonMarginPositionsValue` | 3 422.56 |
| `TotalValue` | 24 161.24 |
| `CalculationReliability` | `Ok` |

20 738.68 + 3 422.56 = 24 161.24. So `CashBalance` is the cash field and `TotalValue` includes position value; the question §5(c) called open is answered, and both fields were already mapped by `get_account` (`saxo/broker.py:380-387`) and are read on LIVE every daemon tick.

One account on the client, `Currency` PLN, and no per-currency cash breakdown anywhere in the body.

### P2. The whole account reconciles to the grosz — and this validates #1728 against the bank

The sum of the 13 closed picks' `net_cash_acct` is +386.90 PLN. Against the account's own numbers:

| Term | PLN |
|---|---|
| Deposits | 24 000.00 |
| Realized net on 13 closed picks | +386.90 |
| Dividend less withholding tax | +4.05 |
| Cost basis of the 3 open positions | −3 652.27 |
| **= `CashBalance`** | **20 738.68** |
| Market value of those 3 positions | 3 422.56 |
| **= `TotalValue`** | **24 161.24** |

Unrealized on the open positions is −229.71, which is exactly the gap between the +386.90 realized and the account's overall +161.24. All three identities close to the cent.

This is the strongest evidence this arc has. The 13 per-pick net figures were derived from the trades and bookings reports; the account balance is computed by the broker on an entirely separate path. They agree. It also confirms §4's rule that episodes sum while the period total needs a separate cost-basis line for what is still open.

### P3. The account's history begins 2026-08-06, on independent evidence

The audit read returns an identical 249 activities for a 1-year and a 3-year window, earliest `2026-08-10`. **That alone establishes nothing**: a server-side retention cap produces the same observation, and 2026-08-10 to the probe date is 57 days, which sits inside a typical 60-day window.

The bookings report is a different endpoint with its own retention, so it discriminates:

| Window asked | Rows | Earliest |
|---|---|---|
| 2026-01-01 .. 2026-08-11 | 11 | 2026-08-06 |
| 2026-06-01 .. 2026-08-11 | 11 | 2026-08-06 |
| 2026-07-15 .. 2026-08-11 | 11 | 2026-08-06 |
| 2026-08-01 .. 2026-08-11 | 11 | 2026-08-06 |

Asked from January it answers the same as asked from August. Under a retention cap it would have shown earlier rows. So the history genuinely begins 2026-08-06 — the first deposit, four days before the first trade. The one-time backfill is therefore two months, not two years, and `history_begins` is a small number rather than a constraint.

### P4. The remaining account-versus-picks gap is small

| Measurement | Result |
|---|---|
| Activities in the window | 249 |
| Executions / bookings returned by `list_fill_history` | 41 / **99** |
| Final fills with no `ExternalReference` | **3 of 41** |
| Executions with no `BookedAmountAccountCurrency` | **0 of 41** |
| Booking types `list_fill_history` returns | `Share Amount`, `Commission`, `Exchange Fee` only |

99 against the 104 of the unfiltered capture confirms on live data that the `kept_trades` filter drops exactly 5 rows (§1.4), and the returned booking types confirm which: the three deposits, the dividend and the tax are absent. The 0-of-41 line confirms on live data the assumption #1728 rests on.

The 3 unlinked fills are **two closes and one open** — LULU and QUBT sold, UBER bought. A hand-opened position on an instrument no pick armed is still unobserved; what is observed is one hand-opened leg on an instrument a pick had already touched.

### What these probes did NOT settle

Four things, each stated because the probes could have closed them and did not.

1. **The balance cross-check passed in the only condition where it cannot fail.** The window was quiet — last fill 2026-10-01, everything long booked — so no trade was in flight and the `Date` versus `ValueDate` basis could not diverge. The balances body carries `TransactionsNotBooked`, so the broker tracks precisely the state that breaks the check. "It reconciles" is true today and untested where it matters.
2. **Probe 3 has not run.** It needs a session day on which something actually executed, a read a few hours after the close and a repeat the next morning. It remains the only probe that puts the cross-check in a condition where it can fail, which makes it more important than its original "refine the cadence" framing.
3. **Paging is still untested, not fine.** 249 activities came back; whether that was one page or several is not observable from the caller, because the client folds pages itself. The sweep's first run is the largest read this layer will ever make.
4. **Single-currency is strong evidence, not proof.** Every `Share Amount` row carries its own `ConversionRate`, so each leg converts at execution and non-PLN cash should never accumulate. But the absence of a per-currency field in the response is not proof that non-PLN cash cannot exist.

One measurement is reported and then discarded, so a later reader does not mistake it for a finding: a direct call to the audit endpoint in the second probe script returned 0 rows for every window, while `list_fill_history` returned 249 in the same run. The direct call was wrong, not the account empty. Nothing is concluded from it.

## 2. Source of truth

`/cs/v1/reports/bookings`, read **unfiltered**, behind a new adapter capability. A sum over all rows for a period is that period's whole money movement by construction: nothing can be missing without the row count saying so.

What is a cross-check and not the source:

- `/port/v1/closedpositions` is genuinely refused on this account (`brokers/saxo/broker.py:173-176`, degrading to `[]` at `:1745-1778`). It stays unused.
- `/cs/v1/reports/closedPositions` and `/hist/v1/transactions` carry a vendor-computed net per the vendor's documentation, and **neither is wrapped anywhere in `apps/`**. Both are unprobed. They become an independent second path in Phase 5, never the spine. §1.2 already supplies a cheaper second path that needs no probe.

## 3. The store

```
~/.alphalens/account_cash/<env>/
    cash_events.jsonl    one line per booking row, keyed by BkAmountId
    trade_rows.jsonl     one line per trades-report row, keyed by TradeId
    balances.jsonl       one line per sweep: the balance snapshot
    sweeps.jsonl         one line per sweep: what was asked, what came back
```

A new root, not inside `broker_orders/<env>/`: that root is the daemon's live money-path state, and a derived record among four queue journals will eventually be read as queue state. Per-env, never pooled, as `exec_quality_parquet` already is for SIM and LIVE fills.

**The path is declared inside the new package, not in `state_paths.py`.** `state_paths.py` is the one *broker-state* seam (ADR 0016) and lives in the keeper's package, which travels with the Stage-1 split while this layer's semantics stay in AlphaLens. Declaring the path there would invert the split argument and create an import edge this layer's own dependency rule forbids. The precedent for a non-broker store is `feedback/` and friends, which own their roots locally.

**Append-only JSONL, folded by key, latest line wins**, through the existing appender (`brokers/journal.py:78-128`: one write, flush, fsync, torn-predecessor repair), so the registered `queue_write_failed` code applies unchanged. The reader mirrors `read_pick_fold` (`brokers/automanager/picks.py:262-309`), which counts malformed lines rather than dropping them.

The reason it is a journal and not a parquet: the bookings report lags, bounded only at "at most two days; a shorter bound was not measured". A number computed today can legitimately change tomorrow. A parquet overwrite replaces yesterday's number silently; the journal keeps both and reports `restated: N`. **A number that changed must be visible as having changed.**

**This journal is never compacted.** Compaction today touches `standalone_stops` and `entry_trails` only, and it is what destroyed `trailed`, `reanchored`, `stop_filled` and `tranche_fired` lines before the first snapshot-taking boot. A restatement history that gets compacted is not a restatement history.

**On SIM the sweep refuses and writes nothing.** The SIM report endpoints return canned rows belonging to other accounts; the adapter already names this `sim_reports_unusable` (`brokers/saxo/broker.py:157-160`). Storing fiction in a money store is worse than storing nothing. There is no SIM half of this layer, and therefore no rehearsal environment — every money rule needs a fixture case.

## 4. The unit of account

Three levels; only the bottom one is stored.

**`cash_event` — one booking row.** Key `BkAmountId`. Always summable in the account currency over any subset.

**`episode` — one instrument, flat to flat, at the account.** Derived, never stored (§1.5). A position opened by hand and one opened by the keeper land in an episode identically, because the derivation refers to no order reference, no pick and no `ExternalReference`. Called "episode", not "position" or "round trip": Saxo's `Position` means something else here and end-of-day netting takes it away.

**Episodes are summable, and their sum is not the period total.** The difference is real money: dividends arriving after an episode closed, withholding tax, financing, deposits. The report computes the period total from cash events and the episode total from episodes, and prints the residual as its own named bucket.

**Never summed:** `pnl_cash`, prices, `r_multiple`. `pnl_cash` is in the *instrument* currency while `pnl_cash_acct` is in the *account* currency. Every LIVE pick is USD today, but the venue arc already reaches XWAR, XETR and XPAR, so summing it is a currency bug waiting for the first Warsaw fill. Only the `_acct` family is ever summed.

**The pick join is a separate, optional layer that changes no number.** It emits an attribution table, never a merged record: per episode one of `one_pick` / `several_picks` / `no_pick`, per pick one of `episode` / `armed_never_filled`. The period total is byte-identical with the picks journal present and absent, and a test asserts that. The precedent is #1714, where the replay join lives in the laboratory so the engine learns nothing about `~/.alphalens`; here the account learns nothing about picks.

## 5. How an incomplete number announces itself

This is what the issue exists for. Three mechanisms, all arithmetic rather than prose.

**(a) The bucket identity.** Every stored row lands in exactly one bucket; `sum(buckets) == sum(all stored rows)` is an assertion. A `BkAmountType` nobody has seen cannot shrink the total — it can only move money into `unclassified`, which prints even at zero, so a non-zero one reads as a change. Today's equivalent is the static sentence `FEES_NOT_INCLUDED = ("financing", "dividends", "withholding_tax")` (`trades.py:295`). This layer has already read those rows, so it **counts** them: "dividends +4.74 PLN, 1 row" is a measurement, while "dividends are not included" is a sentence the owner stops reading by the third month.

**(b) The coverage partition.** Every fill in the read window is either assigned to an episode or listed in `unassigned_fills` with a reason, and the envelope publishes `assigned + unassigned == final_fills_read` as a tested identity. That one assertion makes `if not lots: continue` impossible to reintroduce: a dropped fill does not vanish, it makes the partition fail.

**(c) The balance cross-check.** Each sweep stores a balance snapshot; the next asserts that the change in the account's cash equals the sum of cash events settling in the interval. A disagreement is content, not a crash: the total reports `reconciles: false` with the difference as a number.

*Updated 2026-10-06 (§1.6).* Whether the account's cash is separable from unrealized marks is settled: `CashBalance` and `TotalValue` decompose exactly, and the whole account reconciles to the cent. Which date field the incremental sum uses is NOT settled and needs probe 3.

Two versions, and the difference matters more than it looks. The **cumulative** check — cash book since `history_begins` against today's `CashBalance` — is known to hold. The **incremental** check — the change between two sweeps against the events settling between them — is the one that catches a late restatement, which is the entire reason the store is an append-only journal. A cumulative check that only ever says yes cannot detect a correction to an old row. So if the two ship separately, the cumulative one carries on its face that it cannot see a restatement; otherwise it reads as proof the history is intact, which it is not.

**(d) `totals` is structurally inseparable from `totals.basis`.** `basis` is an object, not prose: episodes summed, episodes excluded, exclusions by reason, currency, windows clipped. The schema makes it a required sibling of every total. The human render prints the basis above the number and the currency on the number, per the repo's rule that a number beside a threshold in a different unit reads as a comfortable pass.

### What it cannot account for

| Cannot | Why | How it is visible |
|---|---|---|
| An episode that opened before the window | FIFO folding cannot see the opening fill | `window_clipped`, excluded, counted in `basis` |
| A position still open | not realized | state `open`, excluded; cost basis reported separately |
| Quantity that will not pair | partial close outside the window, overlapping round trips | state `imbalanced`, a first-class state, excluded, counted |
| A corporate action in kind (split, spin-off) | quantity changes with no trade row | needs a second source; until then the episode cannot flag it and §9 says so |
| Money settled but not yet reported | report lag ≤ 2 days, lower bound unmeasured | the sweep records what it asked for and what came back |
| Money before the first sweep, or aged out of retention | the store keeps only what a read returned | `history_begins` on every total; a `--since` before it answers `before_first_sweep`, never a smaller number |
| Anything on SIM | canned rows from other accounts | the sweep refuses; the total is null with `sim_reports_unusable`, never `0.0` |
| Which pick an old episode belongs to | compaction destroyed journal lines before 2026-10-01 | `picks: []` with a reason, stated as saying nothing about the money, which comes from the venue |

## 6. The surface

**CLI only.** The owner's complaint is logging into the broker's web interface; a terminal command fixes that. A page costs roughly 3 300 lines plus eleven migrations, a maintenance service, a bind mount, a container env var, a systemd unit, a Prometheus rule pair and an OpenAPI regeneration — the measured `/edge` precedent. Not worth it until the numbers are trusted. Phase 7 revisits it only if the owner asks after living with the command.

The shape of the answer:

```
<group> (live) · PLN · 2026-08-01 .. 2026-10-05 · last swept 2026-10-05T06:12Z

  summed 11 of 14 episodes · excluded 3 (window_clipped 2, fee_row_missing 1)

  net on closed episodes              +1 284.37     11 episodes, after commission and exchange fee
  cost basis of open episodes        -14 902.11      3 episodes, not realized
  dividends and withholding               +4.05      2 rows
  unclassified cash events                 0.00      0 rows
  ----------------------------------------------
  trading and income subtotal        -13 613.69
  deposits and withdrawals           +24 000.00      3 rows, not P&L
  ==============================================
  cash book sum                      +10 386.31

  balance moved                      +10 386.31      agrees
  coverage       104 of 104 booking rows classified · 41 of 41 fills assigned
  history begins 2026-08-06   swept through 2026-10-05
```

Five things there are load-bearing. Deposits sit **below** the subtotal and are labelled "not P&L" — 24 000 PLN of funding is the easiest number in this repo to misread as profit. Open episodes are a cost basis, never folded into the realized figure. `unclassified` prints at zero. `balance moved … agrees` is what makes the rest worth reading. And the net says *after commission and exchange fee* on the line, because per §1.3 that is exactly what it is.

JSON reuses the broker group's envelope contract: `schema` and `env` lead, exactly one strict value on stdout, `allow_nan=False`. Those helpers are private to `commands/broker.py`; this is their second use, so they are extracted on the second use, not earlier.

**The name is open (§8.1).** It is not `realized` and not `edge`: `/edge` already owns `realized_r` across four migrations and every one is a simulated replay R over the candidate population, which is the vocabulary trap the audit names. It is also not `ledger`, which in this repo means the pre-registration ledger — "ledger discipline", "ledger verdict", "ledger rule 5" — and already ships `LedgerRow.svelte` on the web side.

## 7. Phasing

### Phase 1 — the net figure (no probe, no decision, independent of everything after it)

Map `BookedAmountAccountCurrency` onto `Execution`, publish it as `outcome.net_cash_acct`, and add `commission_acct` and `exchange_fee_acct` from `CostBooking.amount_account_currency` so the vendor's net can be checked against a derived one at runtime. A warning fires when they diverge beyond a published tolerance.

This is a **read, not a derivation** (§1.2), which is the whole point: the derived path exists only to disagree with the vendor and say so. `fx_conversion` is deliberately not part of either sum (§1.3).

Gates: `trades_schema.py` plus the regenerated committed schema; the published vocabulary table in `apps/alphalens-broker-contract/README.md`, gated both ways; and the `trades.py` size baseline raised in the same commit, since the file sits exactly on it.

### Phase 0 — probes, before Phase 2 and after Phase 1 — **DONE except probe 3**

Three read-only reads were planned. Two ran on 2026-10-05 and their results are §1.6: the whole-account sweep and the `/port/v1/balances` body. Five further questions from the original probe list were settled offline in §1 and correctly did not spend a LIVE read.

**Probe 3 has not run and now gates more than it did.** It is the report lag together with the `Date`-versus-`ValueDate` question, and it needs a session day on which something actually executed, a read a few hours after that close, and a repeat the next morning. §1.6 shows why it matters more than its original framing: the balance cross-check has only been exercised in a quiet window, which is the one condition in which it cannot fail.

Phase 0 needed no authorization decision after all — ADR 0017 preserves ADR 0015's attended unlock verbatim for probes (§8.3). The host constraint stands and was honoured: the LIVE token store has a single-refresher invariant, so the probes ran on the VPS, never from the Mac.

### Phase 2 — the read, behind the adapter

A `SupportsAccountCash` Protocol with `CashEvent`, `TradeRow` and `BalanceSnapshot`, each mapping one vendor row field by field and classifying nothing, plus the Saxo read body in its own module. Unfiltered bookings. SIM returns the refusal, not rows. No new Saxo endpoint. The read body is a separate module because `saxo/broker.py` sits exactly on its size baseline, which keeps the deliberate bump to a handful of lines.

### Phase 3 — the store and the one writer

The store, the codec, the sweep and a `sync` command. `--dry-run` reads and reports the plan without appending. Gates: the new test package must install **its own** operator-state guard — the one in `tests/brokers/__init__.py` does not reach a new package, and 109 broker tests read the real `~/.alphalens` until #1696 fixed exactly this. New dependency rules in `test_module_dependencies.py`, one-way, in the same PR, following the `control_loop` precedent. The new paths go into the dead-code report's scope in the same PR or the context is invisible to it. And the AST gate that proves the CLI places nothing walks `alphalens_cli` only, by its own docstring: a pipeline-side helper that holds a broker is exactly the shape it cannot see, so its corpus is extended in the same PR.

### Phase 4 — the answer

The episode derivation and the report: the bucket identity, the coverage partition, the balance cross-check, the `history_begins` and `swept_through` watermarks. A golden test over the LIVE fixture pins the bucket sums in §1.1, with a second fixture row carrying a different value in each column the test claims to read, because a fixture uniform in the tested column cannot prove a read.

**No timer in this phase.** A hand-run sweep answers the owner's complaint. A timer brings three systemd gates, and the Prometheus rule-unit parity gate is bidirectional: a unit carrying the metrics hook with no staleness rule is red, so the "ship the alert in a later PR" lesson does not apply to a unit file, which is itself the emitter. On top of that the reports returned zero rows on 2026-10-02 and 10-03, so a daily staleness rule on this job is a weekend and holiday false-page candidate. If a timer is wanted later it ships with both rules in one PR and with a cadence chosen against measured report lag.

### Phase 5 — the second path, only if a Phase 0 probe returned rows

Wrap whichever of `/cs/v1/reports/closedPositions` or `/hist/v1/transactions` answered and publish a vendor net beside the derived one with a divergence figure. The vendor number never replaces the derived one.

### Phase 6 — the optional pick join

Per §4. A test asserts the period total is byte-identical with and without the picks journal.

### Phase 7 — a page, only on request

Explicitly its own route and its own Django app, never a tab under `/edge`, for the vocabulary reason in §6.

## 8. Open questions

1. ~~**The command group's name.**~~ **DECIDED 2026-10-06: `cashbook`.** `ledger` was taken by the pre-registration ledger (§6) and `account` collides in the ear with `broker account`.
2. **Does Phase 1 ship on its own?** It is three gates for a small change, and it gives a net figure per pick immediately. Against: the account-level net supersedes it for the account question — but not for the per-pick question, which the episode-level number cannot answer.
3. **Does a second unattended LIVE-reading process get a standing grant?** **Answered for probes, open for a timer.** ADR 0017's own header says it extends ADR 0015 and that "the attended day-bound unlock survives verbatim for probes", and its Consequences repeat it; so an attended probe needed no decision and §1.6 ran under it. An unattended sweep is still a decision, and three facts bound it. The grant is account-bound (`ALPHALENS_SAXO_LIVE_STANDING` must equal `SAXO_LIVE_ACCOUNT_KEY`), so a boolean arm or a half-copied unit is inert. The LIVE-capable surface is one factory plus one keyword, and that factory calls `assert_live_rails()` first, so a second process built through it inherits the whole order-placement rail set even though it only reads — a read-only sweep would have to pin `DAILY_LOSS_LIMIT_R` and `MAX_PICK_NOTIONAL` to boot. And there is no precedent: the grant has exactly one production caller (`saxo/broker.py:2406`); `stream_handles.py` looks like a second one and is the opposite, an explicitly SIM-rail client that a LIVE instance is structurally refused. A read-only LIVE client built WITHOUT the broker factory would skip the order rails entirely, but ADR 0017 point 2 names the factory as the only caller of that keyword, so that route needs an ADR amendment rather than code. Recommendation: run the sweep attended, or timed on the VPS, and only amend the ADR if it must run unattended — a reader should not inherit a placement rail set.
4. ~~**Is the balance cross-check in scope for the first version?**~~ **DECIDED 2026-10-06: yes, in scope.** Not because it passed — it passed in the one condition where it cannot fail (§1.6) — but because the cash field is unambiguous and the decomposition is exact. The date basis for the incremental version still needs probe 3, so the cumulative version ships first, carrying the statement that it cannot detect a restatement (§5(c)).

5. **Open, and newly the most load-bearing: probe 3.** It is the only check that puts the cross-check in a condition where it can fail, and it needs a session day on which something executed.

## 9. Risks

**`window_clipped` is the quiet killer.** An episode whose entry predates the window looks like a complete round trip with half the position. Excluding it makes the total silently too *small* rather than too large. Both directions need a fixture case. This is where the first real bug is expected.

**Episode boundaries are an inference.** The venue states open-or-close per row but pairs nothing (§1.5), so folding is FIFO over account fills and two overlapping round trips on one instrument can fold wrongly. `imbalanced` as a first-class state and a FIFO-assumed warning are containment, not a fix.

**A missing fee read as zero.** If any fee member is null and the summer treats it as `0.0`, the net looks better than reality — the worst direction for a money error. The rule is that a record contributes all of its money or none of it, and the test must mutate exactly that `or 0.0`.

**`restated` could become the normal case.** If the report lag is routinely two days and the sweep runs the same evening, nearly every episode is born incomplete and restated later, which turns a signal into noise. Probe 3 settles the cadence.

**A restatement's shape on the wire is unknown.** The fold rests on "latest line wins per `BkAmountId`". Nobody has seen Saxo correct a booking. If a correction arrives as a new id carrying an offsetting amount rather than a rewrite, the fold double counts while `restated` stays 0 — the exact failure the store exists to prevent. Probe 3 can answer it for free by diffing the id sets across two days.

**Paging has never been exercised on the reports.** Every probe response so far fit in one page, and the sweep's first run is the largest read this layer will make. A dropped page makes the total silently smaller, and the bucket identity cannot see it, because the identity is over rows the read returned. *Still open after §1.6, and sharpened: the client folds pages itself, so a caller cannot tell from a result whether one page or several were fetched. "No paging problem surfaced" is not evidence that paging works.* The backfill is now known to span two months rather than two years (§1.6 P3), which lowers the chance of paging without testing it.

**A corporate action in kind cannot be detected from trade rows.** A split or spin-off changes quantity with no trade row, so the exclusion flag for it can never fire from this source alone. It needs positions or a corporate-action booking type, and until then §5's table overstates what is visible.

**Multi-currency beyond the FX charge.** The account is PLN and every instrument is USD today. A trade whose value date falls in the next window is booked at a rate an earlier window's sum cannot reproduce. With the venue arc already at XWAR, XETR and XPAR this is the first thing a Warsaw fill breaks.

~~**Retention may already have eaten part of the answer.**~~ **Settled 2026-10-06 (§1.6 P3): nothing is missing, because nothing is older.** The bookings report answers identically whether asked from January or from August, earliest row 2026-08-06, so the account's history is two months and not truncated. Recorded as a lesson rather than deleted: the first evidence for this — the audit returning the same rows for a 1-year and a 3-year window — could not tell "the history starts there" from "the endpoint caps there", and was briefly treated as if it could. A second endpoint with its own retention is what settled it.

**Concurrency on the new journal.** The sweep is specified as the one writer, but nothing stops a manual run overlapping a scheduled one. `broker arm`'s key check is already a documented TOCTOU; this store should not add a second one by accident.

**Four files sit exactly on their size baseline:** `trades.py`, `saxo/broker.py`, `saxo/client.py` and `commands/broker.py`. Forgetting the bump is a red build on a PR that looks finished.

**The Stage-1 keeper split will move part of this.** The read under `brokers/` travels with the bracket keeper; the semantics stay in AlphaLens. #1689 keeps one area label, with a cross-cutting comment naming the keeper side.

## 10. Corrections this memo makes to earlier records

1. **#1689's premise is stale.** Its last comment says the source is unknown and names two unverified candidates; both are read today, behind the adapter, and were probed on 2026-10-03 (§0).
2. **"The realized outcome exists nowhere in this repo" is no longer true.** It exists per pick, for LIVE (§0).
3. **"Hand-closed exits would make the number silently incomplete" is no longer true** of the per-pick record; they are covered and their money is computed in full (§0).
4. **A hand-opened position on an instrument no pick ever armed has not been observed on LIVE.** Exactly 3 of 41 fill rows carry no `ExternalReference` (two closes and one open on an instrument a pick had touched). The six fills on Ford and MARA that the record cannot see all carry an `ExternalReference` and are the owner's own machine probes. The structural hole is real — those six fills and 24 004 PLN of cash are invisible — but the issue must not say hand trades are being missed today.
5. **"No net figure exists" overstates it**, and the earlier memo's FX reading is now measured rather than reasoned (§1.3).
6. **The `Exchange Fee` row count is 17, not 10.** The earlier 10 came from a shorter probe window.
7. **CLAUDE.md says 13 JSON-emitting broker commands and omits `trades`; there are 14.** That goes in its own small documentation PR, since CLAUDE.md is treated as code.
8. **The audit endpoint really is a re-rendering of instruction type.** The only `OrderType` values across all 249 audit rows are `Market` (108), `StopIfTraded` (121) and `Limit` (20) — no `TrailingStopIfTraded` anywhere, despite the keeper arming trailing entries. The claim currently lives only in prose; the adapter deserves a comment.

## 11. Side finding, outside this issue: the daily-loss rail looks structurally dead on LIVE

Traced, not run, so this is a code-path reading rather than a measured fact. The only writer of `details["realized_r"]` is `_reconcile_closed_pair` (`brokers/reconcile.py:772`), reachable only when a row of `cross_check.closed_rows` matches on `OpeningExternalReferenceId`. Those rows come from `get_closed_position_rows()`, which returns `[]` on every LIVE tick because the account runs EndOfDay netting. So the open-verdict summary always sums `0.0` and the safety check always compares `0.0` against the limit.

If it holds, the rail never fires on LIVE. It needs its own ticket and its own measurement against the running daemon.

**Do not repair it from this store.** This store is up to two days stale and an intraday rail needs intraday truth. A stale number feeding a money-path rail is a worse bug than a dead rail, because a dead rail is at least honest.
