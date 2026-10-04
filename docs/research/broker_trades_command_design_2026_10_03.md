# Design: `alphalens broker trades`, a read-only record per pick

Status: DRAFT r3, 2026-10-03. r3 records the results of the §0 probes and adjusts §4.5, §4.7 and §5 to them. Code facts come from `origin/main` at `7b109e8e`. Broker facts come from read-only LIVE probes run on 2026-10-03, by the author and by two reviewers. Three facts were re-checked against `origin/main` for this revision: `_apply_env_option` (broker.py:569-640), `_guard_ambient_instance` / `env_ambiguous` (broker.py:655-672), and the `picks --limit` rule (broker.py:2055-2089). The revision also confirmed that `SaxoBroker._tick_size_for` (saxo/broker.py:1735) and `SaxoClient.get_instrument_details` (saxo/client.py:289) exist.

## 1. Goal

The command returns **one record per pick**. It builds the record from the keeper's journals and from the broker's own records. A record holds:

- the TradeIntent (the plan) and the pick's lifecycle: armed, refused or disarmed, with when and why;
- every entry tier: its window, its planned quantity and limit, when it was armed, when it filled, and the venue price and quantity of the fill;
- every exit: when, at what price, for what quantity, and **why**;
- the outcome: gross P&L, P&L as a % of the money spent, R with its denominator stated, fees, and holding time.

The main consumer is the TradeIntent replay (intent-replay, epic #1571). It compares its replay with real LIVE results. That gives three binding rules:

1. Every number carries its **source**.
2. An unknown value is `null` with a **reason** from a published vocabulary. It is never estimated. An allocation rule, such as pro-rata fees, is allowed only when it is published, and its output is marked `derived` with a warning.
3. The command never writes anything. It never places, amends or cancels an order. It applies no limit without saying so in the output.

The work fits in **one PR**, after the read-only probes in §0. The logic lives in a pipeline module. `broker.py` only parses options, resolves the env, calls the builder and renders the result (§8).

## 0. Read-only probes (run 2026-10-03, results)

Each rule below depended on one of three probes. All three ran on 2026-10-03 against the LIVE account, GET-only, with the LIVE recipe and `ALPHALENS_BROKER_ALLOW_ORDERS=0`. The snippet built the broker with `_cli_broker()` and read through its `SaxoClient`. Reads made:

| Read | Window | Rows |
|---|---|---|
| `/cs/v1/audit/orderactivities?EntryType=All` | `FromDateTime=2026-09-07T00:00:00Z`, no end | 138 (25 `FinalFill`) |
| same | `2026-08-01T00:00:00Z` to `2026-09-07T00:00:00Z` (`ToDateTime` accepted) | 111 (16 `FinalFill`) |
| `/cs/v1/reports/trades/{ClientKey}` | `2026-09-07`..`2026-10-03`; `10-01`..`10-01`; `10-02`..`10-02`; `10-03`..`10-03` | 25; 2; 0; 0 |
| `/cs/v1/reports/bookings/{ClientKey}` | the same four windows | 62; 7; 0; 0 |
| `/port/v1/closedpositions` | | HTTP 400 `ClosedPositionNotAccessibleInEndOfDayNettingMode` |

Every report response fit in one page. The `__next` paging fix (§4.7) was therefore not exercised by these reads.

Side observation: building the LIVE broker prints `SAXO LIVE ORDER RAIL UNLOCKED ... real-money orders are now possible in THIS process` even with `ALPHALENS_BROKER_ALLOW_ORDERS=0`. The command's read-only guarantee therefore rests on what it calls (§7, §8 AST gate), not on that banner.

### P1. Do closing fills link to the opening fill? **Only through `RelatedPositionId`, and only on platform-placed manual closes.**

- **`PositionId` never links.** Each of the 25 `FinalFill` rows since 2026-09-07 carries its own `PositionId`; no two are equal. A closing fill gets a fresh id. UBER: opens 7737731002 (t1, 2 @73.08), 7737738525 (t0, 2 @73.13) and 7755617875 (manual Buy 4 @69.55, no `ExternalReference`); the stop 5440923753 sold 8 @67.99 under a new `PositionId` 7759458353. LULU g2: open 7741085955 (3 @96.23); the manual close 5442083130 sold 3 @98.19 under a new id 7742423895.
- **`RelatedPositionId` links correctly when present.** It is set on an order placed against a position, and it equals the opening fill's `PositionId`:
  - LULU g2: the first manual Sell 5442081345 carries `RelatedPositionId` 7741085955, the LULU open. That order was `Rejected`. The Sell that then filled (5442083130) carries **no** `RelatedPositionId`.
  - QUBT: the manual Sell 5444634251 (33 @8.515, 2026-09-17) carries `RelatedPositionId` 7732832861 on its `FinalFill` row. That is the `PositionId` of the QUBT entry fill 5439823189 (33 @7.88, 2026-09-03, ref `c11e00e5-...`).
- **Coverage.** Of the 41 `FinalFill` rows from 2026-08-01 to 2026-10-03, exactly one carries `RelatedPositionId` (QUBT). No keeper-placed stop or TP fill carries it. The UBER stop fill has none.
- **Netting mode.** `/port/v1/closedpositions` refuses with `ClosedPositionNotAccessibleInEndOfDayNettingMode`. The account nets positions at end of day, which is why the venue keeps no open-to-close pairing.
- **Bookings do not link either.** On every bookings row tied to a trade, `RelatedPositionId` equals `RelatedTradeId`.

Decision: §4.5 keeps the position-link step, keyed only on a closing fill's `RelatedPositionId`. It never uses `PositionId` equality. On today's data it decides QUBT only, which a reference does not own (manual close). Every keeper-placed exit is owned by its reference, and the UBER surplus still goes through the remainder rule.

### P2. Report date semantics. **Inclusive single-day windows; trades filter on `TradeDate`, bookings on `Date`; UTC vs local not decidable; same-day availability not observable.**

- **`ToDate` is inclusive.** `FromDate=ToDate=2026-10-01` returned both 2026-10-01 trades (VST sell 6885451891, NESR buy 6885722629) and their 7 bookings rows.
- **The filter field is the trade date, not the value date.** The VST exit has `TradeDate` 2026-10-01 and `ValueDate` 2026-10-02. The `2026-10-02` window returned 0 trades and 0 bookings. Bookings filter on their `Date`, which equalled the trade's `TradeDate` on all 60 trade-tied rows.
- **UTC vs account-local: not decidable from this data.** On all 25 trades, `TradeDate` = `AdjustedTradeDate` = the UTC date of `TradeExecutionTime`. But every execution falls between 13:30 and 19:59 UTC, where the UTC, New York and Warsaw dates are the same. A row that could tell them apart (a fill before 04:00 or after 22:00 UTC) does not exist on a US-listed account. `GET /port/v1/users/me` reports `TimeZoneId` 26 and `Culture` pl-PL; the mapping of id 26 was not looked up.
- **Same-day availability: not observable.** 2026-10-03 is a Saturday and 2026-10-02 had no fills (the audit has none either). `ToDate` = today was accepted (HTTP 200, 0 rows). The 2026-10-01 rows were present when read on 2026-10-03, so the lag is at most two days; a shorter bound was not measured.
- **Audit and report agree on fills.** The set of `OrderId`s with an audit `FinalFill` equals the set in the trades report (25 = 25). Every order had exactly one trade.

Decision: §4.7 keeps the one-day padding on both ends. When an audit fill has no report row, fees and executions are null with `report_row_missing` and the record carries the warning `report_lags_audit`; the audit still gives the fill.

### P3. FX on bookings. **Yes: a realized rate and the account-currency amount are on every row. The FX cost is a field, not a `BkAmountType`.**

- Bookings rows carry `Currency` (USD), `AccountCurrency` (PLN), `Amount`, `AmountAccountCurrency`, `AmountClientCurrency`, `AmountUSD`, `ConversionRate`, `ConversionRateAccountCurrency`, `ConversionRateClientCurrency`, `ConversionRateUSD`, `CostClass`, `CostSubClass`, `Date`, `ValueDate`, `RelatedTradeId`.
- **`ConversionRate` is the realized rate per trade.** `AmountAccountCurrency = round(Amount × ConversionRate, 2)` held on all 62 rows. The rate differs per trade: VST entry 3.85078295, VST exit 3.86282872.
- **`BkAmountType` values seen:** `Share Amount` (25), `Commission` (25), `Exchange Fee` (10), `Corporate Actions - Withholding Tax` (1), `Corporate Actions - Cash Dividends` (1). There is **no** FX type.
- **The FX cost is `ConversionRateAccountCurrency` on the `Share Amount` row.** It is negative on all 25 rows, and `|cost| / (|AmountAccountCurrency| + |cost|)` lies between 0.2469 % and 0.2533 % on all 25, which is a 0.25 % conversion charge rounded to the grosz. The cost is already inside `ConversionRate` and `AmountAccountCurrency`; it is not booked as a separate row. This reading of the field comes from that arithmetic; Saxo's documentation was not consulted. On `Commission` rows the same field is ±0.01 (rounding), and on all other rows it is 0.0.
- **`Exchange Fee` appears only on sells:** 10 of 10 sell trades, 0 of 15 buys.
- **The trades report agrees.** `BookedAmountAccountCurrency` equals the sum of the trade's bookings `AmountAccountCurrency` on all 25 trades.

Decision: §5 fills `pnl_cash_acct` and `notional_spent_acct` from `Share Amount` `AmountAccountCurrency`, and reports the FX charge as `fees.fx_conversion` (account currency). `fx_conversion_cost` leaves `fees_not_included`.

## 2. Command surface

```
alphalens broker trades [--env sim|live] [--format human|json]
                        [--state closed|open|never_filled|unresolved|all]   (default: all)
                        [--since YYYY-MM-DD]   (pick trade_date >= this)
                        [--ticker TICKER] [--pick PICK_KEY]
                        [--limit N]            (default 200, must be >= 1; newest trade_date first)
                        [--all]                (no row limit; cannot be combined with --limit)
                        [--offline]            (journals only; venue-derived fields null, reason "offline")
```

- `--env` and `--format` reuse `_ENV_OPTION` and `_FORMAT_OPTION`. There is no `--json` alias, because `JsonAliasTest` allows the alias only on `reconcile` and `reconcile-fills`.
- The default reads the broker. `--offline` follows the `status --offline` precedent: `_apply_env_option(env, required=not offline)`.
- `--limit` follows the `picks` precedent: a value below 1 is `usage`, and the default is 200. To get every row, pass `--all` explicitly. The envelope carries `truncated`. When rows are cut, one yellow warning goes to stderr.
- `--state` defaults to **`all`**. The main consumer should get every pick unless it asks for less.
- The envelope carries `counts.all`, taken **before** the `--state` filter, and `counts.selected`, taken **after** it. Both are computed before `--limit` cuts the list.
- A bad `--since`, `--state`, `--limit`, `--pick`, or `--limit` combined with `--all`, gives `usage` (exit 2) through `_fail_with("usage", ...)`. The `picks` command uses `_fail` (unclassified) for a bad `--state`; that is not copied.

Example for the replay consumer (it also goes in the README):

```
alphalens broker trades --env live --state all --all --format json
alphalens broker trades --env live --pick VST:2026-09-21 --format json
alphalens broker trades --env sim --offline --format json
```

## 3. Record schema

The envelope schema id is **`alphalens.broker.trades/v1`** (constant `_TRADES_SCHEMA`). One strict line goes out through `_emit_json(_envelope(_TRADES_SCHEMA, resolved_env, ...))`. A JSON Schema for v1 is published next to the README tables. The CLI tests validate the output against it.

### 3.0 Versioning rule

- Within v1, new **optional fields** may be added. New **enum values** may also be added to `reason`, `null_reason`, `warnings[].code`, `source` and `state`. A consumer must treat an unknown enum value as `unknown`.
- The published JSON Schema lists the enum values of its release. Each addition is a README change in the same PR.
- Renaming or removing a field or a value, or changing a type or a unit, needs `v2`.

### 3.1 Time format

Every timestamp in the output is RFC 3339 UTC with milliseconds and a `Z`. The journals mix formats: epoch floats in `standalone_stops`, ISO strings in `entry_trails`/`picks`, and `touch_ts` on `touched` lines. All are converted.

### 3.2 Envelope body

| Field | Type | Meaning |
|---|---|---|
| `schema`, `env` | str | the first two keys (#1379) |
| `generated_at` | time | time of the read |
| `mode` | `"broker"` \| `"offline"` | which sources were read |
| `sources` | object | one entry per source: `{status: read\|skipped\|offline, reason, window: {from,to}\|null, rows}`. The keys are the role names in §3.3. |
| `snapshot_horizon` | time \| null | the earliest compaction-snapshot stamp across the compacted journals. Lines older than this may have been dropped by a compaction that took no snapshot. Null when no snapshot exists. |
| `counts` | object | `all` and `selected`, each `{closed, open, never_filled, unresolved, total}`. Also `malformed`: lines dropped, per journal, counted once per distinct raw line. Also `unattributed_fills`. |
| `truncated` | bool | `--limit` cut the list |
| `trades` | list[TradeRecord] | §3.4 |
| `unattributed_fills` | list[Fill + `reason`] | venue fills on a pick's `uic` that no pick owns. Each one carries `reason` (§4.4, including `manual_open`). Listed, never dropped. |

### 3.3 `Measured`, the shape of every price, quantity, time and money value

```json
{"value": -1.0, "unit": "USD", "source": "venue.bookings", "ref": "trade:6885451891", "null_reason": null}
```

- `unit` is a currency code for money, `"shares"` for quantities, `"s"` for durations, `"price:<ccy>"` for prices, and `null` for times.
- `source` names a **role**, not a file, so that the keeper repo split (`docs/research/bracket_keeper_repo_split_stage1_design_2026_08_02.md`) does not force a v2. The values are:

| Role | Read from today |
|---|---|
| `plan` | the intent JSON |
| `keeper.pick_queue` | `picks.jsonl` |
| `keeper.entry_watch` | `entry_trails.jsonl` |
| `keeper.stop_journal` | `standalone_stops.jsonl` |
| `keeper.submissions` | `submissions.jsonl` |
| `venue.audit` | `/cs/v1/audit/orderactivities` |
| `venue.trades_report` | `/cs/v1/reports/trades` |
| `venue.bookings` | `/cs/v1/reports/bookings` |
| `venue.instrument` | `/ref/v1/instruments/details` |
| `derived` | computed by the builder |

  The `journal` value that §5 of r1 used is gone.
- `ref` names the row, for example `order:<id>`, `trade:<id>`, or `line:<journal>:<kind>`.
- `null_reason` is set exactly when `value` is null. The vocabulary:

| `null_reason` | Meaning |
|---|---|
| `offline` | the broker was not read |
| `not_journaled` | the keeper never writes this fact |
| `compacted_before_snapshots` | the fact sits behind `snapshot_horizon` and is not in any snapshot |
| `no_audit_row` | the order id is known, but the audit window has no row for it |
| `sim_reports_unusable` | `/cs/v1/reports/*` on SIM return canned data from other accounts |
| `not_closed` | the position is still open |
| `never_filled` | no entry tier filled |
| `non_positive_risk` | the entry is at or beyond the denominator stop, so R is undefined |
| `ambiguous_attribution` | the fill cannot be given to one pick by the rules in §4.5 |
| `legacy_plan_shape` | the value needs a size, and the plan is percent-sized |
| `fx_rate_not_realized` | no bookings row with a `ConversionRate` was read for this trade: offline, SIM, or the report has no row yet (§5, P3) |
| `report_row_missing` | the audit has the fill, but the trades or bookings report has no row for its order yet (§4.7, P2) |
| `non_finite` | a computed value was NaN or ±inf |
| `stop_amend_history_unavailable` | offline, and the stop's moves cannot be known (§4.4) |

  No value of `null_reason`, `reason` or a warning code matches `^(place_|amend_)` (§8).

### 3.4 TradeRecord

| Field | Type | Source | Null rule |
|---|---|---|---|
| `pick_key` | str `TICKER:date[-gN]` | `keeper.pick_queue` | never null |
| `ticker`, `trade_date`, `generation` | str, str, int | `keeper.pick_queue` | never null; generation is 1 when omitted |
| `pick_status` | `armed` \| `refused` \| `disarmed` | latest line of the key (fold) | never null |
| `pick_status_at`, `pick_status_reason` | Measured, str | `armed_ts` / `disarmed_ts`+`note` / `refused_ts`+`reason` | reason null only for `armed` |
| `state` | `closed` \| `open` \| `never_filled` \| `unresolved` | derived (§4.6) | never null |
| `state_reason` | str \| null | §4.6 | set for `never_filled` and `unresolved` |
| `plan` | object | `plan`: the **last `armed` line that carries an `intent`**, verbatim | null only if no such line survives; then `plan_null_reason` |
| `plan_armed_at` | Measured | that line's `armed_ts` | |
| `plan_schema_version` | str \| null | `meta.schema_version`, copied verbatim | null when absent (that means the current version, per the door) |
| `plan_size_shape` | `by_amount` \| `by_percent` | `spec.size` vs `suggested_size_pct` (schema versions 1 and 2) | never null |
| `plan_source` | `manual` \| `brief` \| `absent` | `meta.source` | never null |
| `plan_disaster_stop` | Measured | `plan` `spec.disaster_stop` | null `legacy_plan_shape` only when absent; then falls back to the `planned.stop_price` line, with that source named |
| `placed_stop` | Measured | first stop order's audit `Placed` price; offline: `stop_placed.stop_price` (#1621) | null with reason |
| `instrument` | `{uic, exchange_mic, instrument_currency, sizing_currency}` | `watch_open` / submissions | members null with reason |
| `sizing_fx` | `{rate, bid, ask, asof, source}`, each Measured | `watch_open.fx_rate` / submissions `fx_rate*` | null `not_journaled` |
| `side` | `long` \| `short` | `plan` | the builder refuses outcome math when it cannot resolve the side; the outcome fields are then null `non_finite` and a warning is added |
| `entries` | list[EntryTier] | §4.3 | |
| `exits` | list[Exit] | §4.4 | |
| `outcome` | Outcome | §5 | |
| `warnings` | list[`{code, detail}`] | derived | |

**EntryTier**

| Field | Type | Source | Null rule |
|---|---|---|---|
| `tier_index` | int | `watch_open.tier_index` / bracket index / audit ref `-entry-t<k>` | never null |
| `path` | `trail_watch` \| `now_bracket` | | never null |
| `crid` | str | watch crid / bracket `client_request_id` | never null |
| `planned_limit`, `planned_qty` | Measured | `watch_open.limit`/`qty`, or the bracket qty | null `not_journaled` |
| `window_end` | Measured | `watch_open.window_end` (the tier deadline) | null `not_journaled` |
| `touched_at`, `touch_price` | Measured | `touched.touch_ts` (keeper detection) | usually null `not_journaled`; the venue has no touch event |
| `trigger_order_id` | str \| null | `trail_armed.order_id` / `fired.order_id` / bracket `entry_order_id` / audit ref | null when the tier never armed |
| `armed_at` | Measured | audit `Placed` row of that order | |
| `terminal` | `filled` \| `expired` \| `cancelled` \| `suspended` \| `open` | audit first, then entry-watch fold | never null |
| `terminal_at`, `terminal_at_source` | Measured | audit `Cancelled`/`Expired`/`FinalFill` row; picks `disarmed_ts` for an operator disarm; otherwise null `not_journaled` | |
| `fill` | Fill \| null | §4.3 | null when not filled |

**Fill** (used for entries, exits and `unattributed_fills`)

| Field | Type | Source |
|---|---|---|
| `order_id` | str | audit / journal |
| `external_reference` | str \| null | audit `ExternalReference` |
| `venue_time` | Measured | audit `FinalFill` `ActivityTime`. Fallbacks: `fired.venue_activity_time` (#1668), then offline detection time with source `keeper.*` |
| `price` | Measured | audit `FinalFill` **`AveragePrice`** (the cumulative average). `ExecutionPrice` is used only when `AveragePrice` is absent and there is exactly one execution. |
| `qty` | Measured | audit `FilledAmount` (cumulative) |
| `executions` | list[`{trade_id, time, price, qty}`] | `venue.trades_report`: every TradeId of the order. Empty on SIM. |
| `detected_at` | Measured | journal `ts`, converted to UTC. Kept separate; never used as the venue time. |
| `position_id`, `related_position_id` | str \| null | audit |
| `fees` | `{commission, exchange_fee, fx_conversion}`, each Measured | `venue.bookings`, summed over **every** TradeId of the order. `commission` and `exchange_fee` are read from `Amount` in `Currency`. `fx_conversion` is `ConversionRateAccountCurrency` of the `Share Amount` row, in the account currency (P3). |
| `realized_fx` | `{conversion_rate, share_amount_acct}`, each Measured | `venue.bookings` `Share Amount` row(s) of the order: `ConversionRate` and `AmountAccountCurrency` (signed as sent). Null `fx_rate_not_realized` when no such row was read. |

**Exit** is a Fill plus:

| Field | Type | Meaning |
|---|---|---|
| `reason` | enum (§4.4) | why it exited |
| `reason_evidence` | list[str] | what decided the reason, for example `audit:Changed 138.82 @2026-10-01T13:40:17.000Z`, `keeper:trailed 138.824`, `no_external_reference` |
| `stop_level_at_fill` | Measured | the last price-changing audit `Changed` price of the stop order, or its `Placed` price; null for non-stop exits. Offline it is always null `stop_amend_history_unavailable` for a stop exit: a journaled placement price may have moved since (VST placed 125.85, filled at a level of 138.82). |
| `tp_label` | str \| null | the `n` from `_TP_REF_RE` |
| `attributed_qty` | Measured | this pick's share of the fill (§4.5) |
| `attribution` | `journal_tie` \| `external_reference` \| `position_link` \| `fifo_fallback` | how ownership was decided |

## 4. How a pick is rebuilt

Module: `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/trades.py`. It exposes `build_trades(env, *, broker | None, filters, now) -> TradesReport`. `TradesReport` is a frozen dataclass that renders itself to a dict. This is the same split as `status_snapshot.build_snapshot`.

### 4.1 Reading the journals

1. `journal_snapshots.iter_journal_history(journal, *, malformed: list[str] | None = None)` gains an optional collector, in the style of `submission_log.iter_submission_records`. The collector **deduplicates raw malformed lines by their bytes**, so a bad line that sits in both a snapshot and the current file counts once. The default behaviour for existing callers does not change.
2. All four journals are read through `iter_journal_history`. Paths come from `state_paths.{picks,entry_trails,standalone_stops,submissions}_path(env=resolved_env)`, always with the explicit env. Picks and submissions are not compacted, so for them this read just returns the file.
3. `entry_trails.fold_entry_trail_lines` takes strings, while the history yields dicts. The module adds `fold_entry_trail_records(records)`, a dict-based fold. The string fold delegates to it. The two must not drift apart, so the existing fold tests run on both entry points.
4. `touched` lines and every `fired` line are collected in a separate pass, because the fold keeps only the latest record and drops `touched`.
5. **Dedup limits.** History dedup is keyed on canonical JSON. Most entry-watch lines carry no `ts`. A re-arm `watch_open`, or a second `cancelled` identical to an earlier one, therefore collapses into one record, and the fold sees the wrong order. This can change `terminal`, `planned_*` and `window_end` of a re-armed crid. Two consequences:
   - In broker mode, `terminal` and `terminal_at` come from the audit, which is not affected.
   - Offline, a crid whose history holds a collapsed duplicate gets the warning `fold_order_uncertain`.
6. Order never comes from file position or from a missing `ts`. Broker mode orders events by audit venue times. Offline mode uses the generation order from `stop_journal._apply_generation_reset`.

### 4.2 Picks, lifecycle and plan

- Picks come from the history of `picks.jsonl`. **Status** comes from the fold (`read_pick_fold`): the latest line per key gives `pick_status`, `pick_status_at` and `pick_status_reason`.
- The **plan** comes from a separate raw pass: the last `armed` line **that carries an `intent`** for that key. When an idempotent replace left two armed lines, the later one in file order wins, as in the drain. A disarmed or refused line has no intent, so the fold's latest line is never used as the plan.
- The raw pass also decodes percent-sized documents (schema versions 1 and 2) that `iter_picks` skips (`LEGACY(size_pct_v2)`). On LIVE that is 9 of the 13 closed picks (GME, RHI, QUBT, AMBA, UBER, LULU and others), not only LULU and QUBT. Size-derived fields are null with `legacy_plan_shape`.
- Placement is the join with `submissions.jsonl` through `picks._submission_join_key`.

### 4.3 Entry tiers

1. **Trail-watch tiers:** every `watch_open` whose crid matches `<TICKER>-<trade_date>[-gN]-entry-t<k>`, or whose `pick_key` matches.
2. **Now-bracket tiers:** `submissions.jsonl` `brackets[i]` (`entry_order_id`, `client_request_id`, qty). QUBT is the precedent.
3. **Audit-found tiers (broker mode):** an audit fill whose ref is `<crid>-entry-t<k>-fire`, or a submissions bracket UUID, and that has no journal tier, is added. It gets the warning `entry_not_in_journal`. No such case exists on LIVE today: all 19 `fired` lines plus QUBT 5439823189 are covered. The rule makes sure a lost line cannot hide an entry.
4. **Fill values:** taken from the trigger order's `FinalFill` row, as in the Fill table. With ≥2 executions, `executions` comes from `venue.trades_report`. If Σ execution qty ≠ `FilledAmount`, or the qty-weighted execution price differs from `AveragePrice` by more than one tick, the warning is `audit_report_disagree`. If only partial `Fill` rows exist, the last row's cumulative `FilledAmount`/`AveragePrice` is used, with `partial_fill_shape_unverified`. LIVE has never produced such a fill.
5. **Journal cross-check:** the journal `fired` values are kept. If they differ from the audit by more than one tick, or by any quantity, the warning is `journal_audit_disagree`. Offline, the journal values are the values, and their source says so.
6. **Time cross-check:** audit and report times differ by about 1 ms (VST: 13:36:35.916Z vs .917Z). Cross-source time comparisons use a tolerance of **1 s** (constant `_CROSS_SOURCE_TIME_TOLERANCE_S`). The venue time is always the audit `ActivityTime`.

**Tick tolerance.** Every "within one tick" comparison uses the instrument tick from `SaxoBroker._tick_size_for(price, details)`, with the details coming from `get_instrument_details(uic)`. That is one GET per uic, recorded in `sources["venue.instrument"]`. Offline mode makes no tick comparison (§4.4).

### 4.4 Exits and their reasons

**Who owns an exit order (broker mode).** The audit `ExternalReference` decides. Journal lines are cross-checks only. Each predicate can return "no match"; none falls back to a default.

| Reference shape | Owner |
|---|---|
| `<crid>-entry-t<k>[-fire]-stop-<n>` | `stop_journal._pick_key_from_stop_ref` |
| `<uuid>-stop-<n>`, or a bracket child in submissions `brackets[].exit_order_ids` | the submissions bracket with that UUID or order id |
| `<request_id>-stop` / `<request_id>-tp` (OCO legs, saxo/broker.py:1018) | the pick whose crid or submission request id equals `request_id`. Otherwise the uic rule. |
| `u<uic>-tp<n>-sell` (`live_exit_engine.py:433`), matched with `_TP_REF_RE` (`-tp(\d+)(?:-\|$)`) **and** with the leading `u<uic>` equal to the fill's uic | the pick tied by a surviving `tranche_fired.telemetry.sell_order_id`. Otherwise the uic rule: the single pick with an open lot on that uic in the window. If two picks have open lots there, `ambiguous_attribution`. |
| no reference | §4.5, with the evidence string `no_external_reference` |

`labels.tp_label_from_tag` is **not** used as a test. It never returns "no match", because it falls back to `tag.upper()`. It is used only to render the label after `_TP_REF_RE` has matched.

**Which fills are candidates.** Every audit fill row on the pick's uic in `[first entry fill, now]` on the closing side. Offline: `stop_filled` lines, joined to `stop_placed` by `order_id`, plus `tranche_fired` lines.

**Price-changing row.** A `Changed` row is "price-changing" when its `Price` differs from the previous row **of the same order id**, starting from that order's own `Placed` price. A quantity-only amend is not a stop move. UBER's stop went 2→4→8 shares, all at 68.00.

**Reasons (broker mode), checked in this order:**

| `reason` | Rule | Replay event it maps to |
|---|---|---|
| `take_profit` | owned via a TP or OCO-tp reference | `tp_fired` with tranche index n - 1 for the label TPn; on the last tranche also the zero-unit marker `position_closed(tp_complete)` |
| `disaster_stop` | a stop order with **no** price-changing row. The fill price is not compared: StopIfTraded turns into a Market order and may slip. | `position_closed(stop)` |
| `trailed_stop` | price-changing rows exist, and a `trailed` line on the same uic, with `ts` between this order's `Placed` and `FinalFill` times, has a `level` within one tick of the last changed price | `stop_moved(trail)` then `position_closed(stop)` |
| `reanchored_stop` | the same, matched against `reanchored.stop_price` | `stop_moved(reanchor-on-fill)` then `position_closed(stop)` |
| `stop_moved_kind_unknown` | price-changing rows exist and no in-window marker matches (lost before the snapshot horizon). EWTX, ASTS. | `stop_moved` with an unknown reason, then `position_closed(stop)` |
| `manual_close` | a closing fill with no `ExternalReference`. LULU g2, QUBT. Rejected manual attempts go into `reason_evidence`. | none; the replay has no equivalent and should exclude it from comparison |
| `manual_open` (in `unattributed_fills` only) | an opening fill with no `ExternalReference` that no pick owns (UBER Buy 4 @69.55) | none |
| `unknown` | anything else, with its evidence | none |

These are published in the contract README as a mapping table. The table also maps them to `trade_alerts.ExitReason` (`PLAN_STOP`↔`disaster_stop`, `TRAILED_STOP`↔`trailed_stop`, `REANCHORED_STOP`↔`reanchored_stop`). A test checks that every `ExitReason` member has a row, so the two vocabularies cannot drift apart. The replay's `time_stop` has no row, because LIVE has no time stop by design.

**Offline reasons.** These come from journal markers only, never from a fill price:

- a `tranche_fired` line gives `take_profit`;
- a `trailed` line in the stop's window gives `trailed_stop`;
- a `reanchored` line in the stop's window gives `reanchored_stop`;
- a stop fill with no marker gives `reason: null` with `null_reason: stop_amend_history_unavailable`.

The builder never uses `trade_alerts.stop_reason`. That function maps "no marker" to `PLAN_STOP`, which is wrong for EWTX and ASTS.

**`manual_close` is a heuristic.** LIVE has machine-placed StopIfTraded orders with no reference: the 2026-08-10 research probes on uic 486, orders 5432611809 and 5432644973. Both were cancelled, so nothing is misclassified today. The `no_external_reference` evidence string makes the basis visible.

### 4.5 Attribution when picks share a `uic`

A uic is not a pick identity. Rules, in order:

1. **Owned fills.** A fill owned through its reference (§4.4) or through a journal tie goes to that pick, up to the pick's open quantity. `attribution` is `external_reference` or `journal_tie`.
2. **Position link.** An unowned closing fill whose audit **`RelatedPositionId`** equals the `PositionId` of an opening fill of exactly one pick goes to that pick (`position_link`). P1 showed that `PositionId` itself never links: on this end-of-day-netting account every fill, opening or closing, gets a fresh `PositionId`, so two `PositionId`s are never compared. `RelatedPositionId` is set only on orders placed against a position from the platform (manual closes). On LIVE it decides QUBT (5444634251 → 7732832861) and nothing else; no keeper-placed exit carries it. A `RelatedPositionId` that matches no pick's opening fill leaves the fill to the next steps, with the evidence string `related_position_unmatched`.
3. **FIFO fallback.** The remainder is allocated to open lots, oldest venue time first, with the warning `attribution_fifo_assumed` (`fifo_fallback`). FIFO is an assumption about EOD netting, not a measured fact. It is never used silently.
4. **Remainder.** Quantity left after every pick lot is closed goes to `unattributed_fills`. The pick that owned the order gets the warning `exit_qty_exceeds_pick`. UBER: the stop sold 8; 4 shares go to the pick; `unattributed_fills` holds the 4 surplus shares (`reason: disaster_stop`, from the owning stop) and the manual Buy 4 @69.55 (`manual_open`).
5. **No order known.** If the order of lots cannot be settled (offline, no venue times), the split fields are null with `ambiguous_attribution`.

P&L is computed on `attributed_qty`, never on `stop_filled.qty`.

LIVE has no real case of two picks with **overlapping** fills on one uic. LULU g1, ALB g1 and ENPH g1 never filled, and the BE picks have no audit rows. Rule 3 is therefore tested on synthetic fixtures, labelled as such, and on the real UBER shape. Rule 2 has a real case (QUBT) and a synthetic two-pick case.

Note on LULU g2 (P1): the rejected manual Sell carries the link, the filled one does not. The filled Sell is still `manual_close` with `no_external_reference`; it reaches the pick through rule 3 (the only open lot on uic 29957), not through rule 2. The rejected order's `RelatedPositionId` goes into `reason_evidence`.

### 4.6 State

| `state` | Rule |
|---|---|
| `never_filled` | Σ entry fill qty = 0. `state_reason` is one of `refused`, `disarmed`, `expired`, `cancelled`, or `pending` (armed and not yet placed). It comes from `pick_status` and the tier terminals. |
| `closed` | attributed exit qty = entry qty |
| `open` | entry qty > exit qty, and the record is complete enough to say so. In broker mode the audit covers the window. Offline, the pick has no lines behind `snapshot_horizon`. |
| `unresolved` | offline only, never in broker mode. Three cases, each with its `state_reason`: (1) entry qty > journal exit qty, and the pick's `plan_armed_at` is older than `snapshot_horizon` (or no snapshot exists): lines may have been lost, so the builder does not guess, `compacted_before_snapshots`; (2) the pick has a now-bracket tier, whose fill no journal holds, so neither `closed` nor `never_filled` can be said: `not_journaled` (QUBT, LAC, MP on LIVE; SIM RHI:2026-09-03, where a `closed` would have hidden 701 shares); (3) a fill on its uic could not be given to one pick: `ambiguous_attribution`. Outcome fields are null with the same reason. |

Offline, a pick at `open` whose stop has no fill also gets the neutral warning `exit_not_in_journal`. It does not suggest a manual close: on LIVE, 5 of the 7 picks in that shape were closed by the keeper (GME, AMBA, ENPH g2, SMMT, and the ALB g2 stop-0).

RHI:2026-09-02 is `open`: 3 one-share tiers and a working stop 5439449105 for 3 @30.39. `broker positions` shows 3 legs.

### 4.7 Broker reads and their bounds

This is a new capability Protocol in `alphalens_pipeline/brokers/fill_history.py`. Following #1122, the adapter reports and the contract does not decide.

```python
@runtime_checkable
class SupportsFillHistory(Protocol):
    def list_fill_history(self, since: datetime, until: datetime) -> FillHistory: ...
    def tick_size(self, uic: str, price: float) -> float | None: ...
```

`FillHistory` holds typed tuples:

- `OrderActivity`: order_id, external_reference, uic, buy_sell, order_type, status, sub_status, price, activity_time, filled_amount, execution_price, average_price, position_id, related_position_id;
- `Execution`: trade_id, order_id, execution_time, price, signed amount, to_open_or_close;
- `CostBooking`: related_trade_id, `bk_amount_type` (the raw `BkAmountType` string), amount (signed, as the venue sends it), currency, `amount_account_currency`, `account_currency`, `conversion_rate`, `conversion_cost_account_currency` (the raw `ConversionRateAccountCurrency`), booking `date`.

The `SaxoBroker` implementation reads:

1. `/cs/v1/audit/orderactivities?EntryType=All&FromDateTime&ToDateTime`. `get_order_activities` gains `to_datetime`. The endpoint ignores `Uic`, so filtering happens on our side.
2. `/cs/v1/reports/trades/{ClientKey}?FromDate&ToDate&AccountKey`, via the new `get_trades_report`.
3. `/cs/v1/reports/bookings/{ClientKey}?FromDate&ToDate&AccountKey`, via the new `get_bookings_report`. `BkAmountType` values are mapped explicitly: `Commission` → commission; `Exchange Fee` → exchange_fee; `Share Amount` → the trade's cash leg (`realized_fx`, and `fx_conversion` from its `ConversionRateAccountCurrency`). P3 found no FX `BkAmountType`: the conversion charge is a field on the `Share Amount` row. The ±0.01 `ConversionRateAccountCurrency` on `Commission` rows is rounding and is not summed. Other types (`Cash Amount`, `Corporate Actions - Cash Dividends`, `Corporate Actions - Withholding Tax`, and any type not listed here) are not summed. They are listed in `fees_not_included`. An unknown type gets the warning `booking_type_unmapped` with the raw string.
4. `/ref/v1/instruments/details` once per uic, through the existing `get_instrument_details`, for the tick size.

On **SIM**, reads 2 and 3 are skipped with `status: skipped, reason: sim_reports_unusable`. Fees are then null and `executions` is empty.

**Windows.**

- The audit window is `[min(plan_armed_at of the selected picks) − 1 day, now]`, in datetimes.
- The report window is padded by **one day on both ends**, in dates. Rows are then filtered on our side by `TradeExecutionTime`. P2 showed that `ToDate` is inclusive and that trades filter on `TradeDate` and bookings on `Date` (not `ValueDate`). It could not show whether those dates are UTC or account-local: every LIVE execution falls between 13:30 and 19:59 UTC, where the dates agree. The padding stays, because it costs nothing and covers both readings.
- An audit fill with no trades-report row (P2 could not measure same-day availability; the lag is at most two days) keeps its audit values. Its `executions` is empty, its `fees` and `realized_fx` are null with `report_row_missing`, and the record carries the warning `report_lags_audit`. The command never treats a missing report row as a missing fill.
- No `$top` is sent, and every `__next` page is followed. `__count` is the page length, not a total, so it is never read as a total.
- A `since` older than the venue's stated 2-year retention is still requested as asked, and `sources[...].window` records it.

**Paging prerequisite, in the same PR.** `SaxoClient._normalize_next_url` strips only `/sim/openapi`. A LIVE `__next` link (`https://gateway.saxobank.com:443/openapi/...`) therefore raises in `_join_url`; a reviewer reproduced this with `$top=5`. The fix: parse the URL; require the host to equal the configured base host, ignoring an explicit `:443`; strip the configured base path, whether `/sim/openapi` or `/openapi`. A foreign host still fails loudly.

**Errors.** Broker errors go through `except BrokerError: raise _fail_from_broker_error(exc, "broker trades failed")` and are never partial. Requests keep the client's spacing of at least 0.5 s. A 429 gives `broker_rate_limited` (exit 7). The rate limits of the report endpoints were not measured.

## 5. Outcome math

All Outcome fields are `Measured` with `source: "derived"`, and `ref` names the inputs. Each one is null with a reason when an input is null. Several names are the replay's, but a shared name does NOT mean the same quantity or unit: `notional_spent` and `pnl_cash` are in the instrument currency here and in the account currency in the replay summary, and the replay's `r_multiple.denominator.value` is `risk_per_share` here. The contract README carries the field-by-field mapping, with units.

| Field | Definition | Unit |
|---|---|---|
| `entry_qty` | Σ entry fill qty | shares |
| `avg_entry_price` | qty-weighted mean of entry fill prices | instrument ccy |
| `exit_qty` | Σ `attributed_qty` | shares |
| `avg_exit_price` | qty-weighted mean over `attributed_qty` | instrument ccy |
| `notional_spent` | Σ entry qty × price | instrument ccy |
| `pnl_cash` | `side_sign × Σ attributed_qty × (exit_price − avg_entry_price)`, gross | instrument ccy |
| `pnl_pct_of_spent` | `pnl_cash / (avg_entry_price × exit_qty) × 100` | % |
| `denominator_stop` | the stop LEVEL the R uses: `plan_disaster_stop`, per replay spec §5.1 (`spec.disaster_stop`, even when `exit.initial_levels` is supplied). `ref` says so. | instrument ccy |
| `risk_per_share` | `side_sign × (avg_entry_price − denominator_stop)`. This is the replay's `r_multiple.denominator.value`. Null `non_positive_risk` if ≤ 0. | instrument ccy |
| `r_multiple` | `pnl_cash / (exit_qty × risk_per_share)` | R |
| `holding_seconds` | last exit venue time − first entry venue time. Offline it uses detection times, and the source names `keeper.*`. | s |
| `fees` | `{commission, exchange_fee, fx_conversion}`, signed as the venue sends them (negative = cost). `commission` and `exchange_fee` are in the booking currency (`Currency`, USD on LIVE). `fx_conversion` is Σ `ConversionRateAccountCurrency` of the `Share Amount` rows, in the account currency (P3: about 0.25 % of the traded value per conversion, already inside the realized rate). For a fill shared between owners, the pick's share of every booking amount is `amount × attributed_qty / FilledAmount`, with source `derived`, the TradeIds in `ref`, and the warning `booking_prorated`. | per member |
| `notional_spent_acct` | `−Σ` entry `Share Amount` `AmountAccountCurrency` (prorated as above). Null `fx_rate_not_realized` when any entry fill has no such row. | account ccy |
| `pnl_cash_acct` | `Σ` signed `Share Amount` `AmountAccountCurrency` over the entry fills and the attributed exit fills (prorated). It is gross of commission and exchange fee but **includes** the FX conversion charge, because the venue books the cash leg at the realized rate. `fees.fx_conversion` states that charge, so a consumer can add it back. Null `fx_rate_not_realized` when any contributing fill has no row. The journaled `sizing_fx` is reported (§3.4) but never used here, because it is the rate at sizing time, not the rate realized on the trade. | account ccy |
| `mfe_lower_bound` | `max(trailed.peak)` in the stop's window minus `avg_entry_price`. It is a lower bound, because the keeper only journals peaks while trailing. Null `not_journaled` when no `trailed` line exists. | instrument ccy |
| `fees_not_included` | constant list: `financing`, `dividends`, `withholding_tax`. P3 mapped the FX charge, so it is no longer in the list. Dividends and withholding are booked with `RelatedTradeId` 0 (ALB, 2026-10-01), so they cannot be tied to a fill. | |

Rules:

- The exit-dependent fields are computed only when `state == closed`. For `open`, the entry side is filled and the rest is null with `not_closed`.
- **Gross only.** `PnLUSD` from `closedPositions` is net of commission, fee and FX (VST: 12.87 vs gross 21.30), and v1 does not read it. No net figure is derived.
- Every value is checked with `math.isfinite` before rendering. A non-finite value becomes null with `non_finite`. `_render_json` uses `allow_nan=False`, so a NaN that got through would become an `unclassified` refusal.

## 6. Failure codes

**No new code.** Every refusal reuses a registered code:

| Situation | Code | Exit |
|---|---|---|
| bad `--state` / `--since` / `--limit` / `--pick` / `--format`, or `--all` with `--limit` | `usage` | 2 |
| wrong state-directory layout (`_guard_state_layout`) | `state_layout` | 1 |
| `--env live` and the unit environment cannot be composed (`_apply_env_option(required=True)` → `_fail`) | `unclassified` (existing shared behaviour of every LIVE read command; see below) | 1 |
| the LIVE broker cannot be built (`_cli_broker`) | `live_refused` | 1 |
| the broker lacks `SupportsFillHistory` and `--offline` was not given | `broker_unsupported` (contract code; reconcile-fills' `_fail` is not copied) | 1 |
| broker read errors | `broker_transient` / `broker_rate_limited` (7), `broker_auth`, `broker_failed` (1), via `_fail_from_broker_error` | as classified |

Two notes on this table:

- `env_ambiguous` is **not** reachable. Only `_guard_ambient_instance` raises it, and that guard serves the queue-writing commands.
- The composition failure is a real refusal mode. It is shared by `status`, `positions` and every other LIVE read, so it is classified in its own small PR, not bundled here. That PR graduates the code out of `unclassified`, which is not a breaking change.

Everything uncertain about a trade is **content with exit 0**: unknown reasons, lost markers, attribution conflicts, a missing audit row. On a refusal, stdout stays empty: the builder finishes every read before it renders. Every `_fail_with` uses a literal registered code, so the AST gate in `test_broker_failure_contract_cli.py` stays green.

## 7. What it must NOT do

- **No orders.** The command reaches only `list_fill_history`, `tick_size` and the account-key lookup. `ALPHALENS_BROKER_ALLOW_ORDERS=0` is forced on `--env live`.
- **No writes.** No journal append, no compaction, no parquet, and no token-store write beyond what `_cli_broker` construction already does.
- **No silent caps.**
  - `--limit` is announced, and `--all` exists.
  - Paging is followed to the end.
  - Malformed lines are counted.
  - Unattributed fills are listed.
  - Skipped sources and sources behind `snapshot_horizon` carry reasons.
- **No invented numbers.**
  - No detection time is labelled a venue time.
  - No `ExecutionPrice` is paired with a cumulative quantity.
  - No `stop_filled.qty` is used as the pick's size.
  - No `PnLUSD` is used as gross.
  - No sizing FX rate is used as a realized rate.
  - No stop move is inferred from a fill price.
  - No `open` is reported where lines may be lost.
- **No `__nextPoll`.** The client already refuses to follow it.

## 8. Placement and contract gates

- **Classification lives in `alphalens_pipeline`.** The no-orders AST gate flags string constants in `alphalens_cli` that match `^(place_\w+|amend_\w+)$`. Comparing a line with `"amend_ok"` inside `broker.py` turns the gate red. `broker.py` only renders a finished `TradesReport`.
- **JSON contract (#1379).** Add `("trades", ["trades", "--offline"], True, ("mode", "sources", "snapshot_horizon", "counts", "truncated", "trades", "unattributed_fills"))` to `_JSON_COMMANDS`. Call order: `_resolve_format`, then `_apply_env_option`, then `state_paths.broker_environment()`, then `_guard_state_layout()`, then `_cli_broker()` only when not offline. Imports are lazy, inside the command body.
- **Human renderer.** One block per pick: a header with status and state, an entries table and an exits table, with `-` for absent cells. It carries the same facts as the JSON. Warnings go to stderr in yellow.
- **Ownership after the split.** The schema belongs to the bracket-keeper side (`area:bracket-keeper`). The replay (`area:contract`) consumes it. Role-named sources keep v1 valid when the keeper moves to its own repo.
- **Docs in this PR:**
  - the `broker.py` module docstring;
  - the JSON-contract note in `deploy/systemd/README.md`;
  - a "broker trades" section in `apps/alphalens-broker-contract/README.md`, with the `reason` / `null_reason` / warning / `source` vocabularies, the versioning rule, the fee sign convention, the replay mapping table and the consumer example;
  - the JSON Schema file.

  The `CLAUDE.md` change ("13 JSON-emitting broker commands" becomes 14) goes in its own PR.

## 9. Tests (TDD, `unittest.TestCase`, written red first)

Files: `apps/alphalens-research/tests/brokers/automanager/test_broker_trades.py` (builder) and `test_broker_trades_cli.py` (CLI). Client tests sit next to the existing Saxo client tests. Fixtures live under `tests/brokers/automanager/fixtures/trades/`. Each fixture is a set of journal files (current file plus `compaction_snapshots/`) and a `FakeFillHistory` broker. Fixtures built from LIVE data are redacted of AccountId, ClientId, UserId and CorrelationKey. Synthetic fixtures are named `synthetic_*`.

**Foundation**

1. `iter_journal_history(..., malformed=lst)`: same records as before; a bad line in both a snapshot and the current file counts once.
2. `fold_entry_trail_records` equals `fold_entry_trail_lines` on the existing fold fixtures.
3. `_normalize_next_url` follows a LIVE `:443/openapi` `__next` link and a SIM link, and refuses a foreign host. `__count` is never read as a total.
4. `get_order_activities(to_datetime=...)` sends `ToDateTime`. The report methods build the documented paths and follow pages.
5. `list_fill_history` maps raw rows, `BkAmountType` included. On SIM it does not call the report endpoints.

**Ownership and reasons**

6. `take_profit`, AMBA shape: no `tranche_fired` line, only the `u267154-tp1-sell` audit row. It is owned by uic and time window.
7. A ref like `foo-bar` that `tp_label_from_tag` would only uppercase is **not** a TP.
8. OCO TP leg and OCO stop leg (`<request_id>-tp` / `-stop`); a bracket child stop via `exit_order_ids`.
9. `disaster_stop`: no price-changing row, filled **3 ticks below** the stop. The reason is still `disaster_stop`.
10. GME shape: the journal `stop_placed` has no id or ref. Ownership still comes from `GME-2026-08-27-entry-t0-fire-stop-0`, and the reason is not `unknown`.
11. `trailed_stop`; `reanchored_stop`. A `trailed` line on the same uic from **another stop generation** (synthetic) must not match.
12. `stop_moved_kind_unknown`, EWTX shape. The test asserts the reason is not `disaster_stop`.
13. RHI shape: several stop orders with one ref, and the journal points at the cancelled order. Ownership follows the audit, and the state is `open`.
14. `manual_close`, LULU g2 shape: a rejected Sell with `RelatedPositionId` appears in the evidence; `no_external_reference` is present. QUBT shape: a manual Sell that was not rejected.
15. `unknown`: a closing fill whose reference no pick claims.
16. Every `trade_alerts.ExitReason` member appears in the published mapping table.

**Structure**

17. Two-tier entry (ALB g2 shape, two stop orders): qty-weighted average; a qty-only amend is not a move.
18. Now-bracket entry (QUBT): `plan_size_shape = by_percent`, `plan_schema_version = "2"`. GME: `"1"`, `plan_source = absent`.
19. UBER shape (real): 4 shares attributed; `unattributed_fills` holds the 4 surplus shares and the `manual_open` Buy 4 @69.55; exit bookings (commission, exchange fee, `Share Amount`, `fx_conversion`) prorated 4/8 with `booking_prorated`.
20. `synthetic_two_picks_one_uic`: a `RelatedPositionId` link decides. Without the link: FIFO with `attribution_fifo_assumed`. Offline: null with `ambiguous_attribution`. Two fills with equal-looking but different `PositionId`s are never linked by `PositionId`.
20a. QUBT shape (real, P1): the manual close's `RelatedPositionId` equals the entry fill's `PositionId`, so `attribution = position_link`. LULU g2 shape (real, P1): the link sits only on the rejected Sell; the filled Sell is not `position_link`, and the rejected order's `RelatedPositionId` is in `reason_evidence`.
20b. P2: an audit fill with no trades-report row keeps its audit price and qty; `fees` and `realized_fx` are null `report_row_missing`; warning `report_lags_audit`.
20c. P3: `pnl_cash_acct` and `notional_spent_acct` come from `Share Amount` `AmountAccountCurrency`; `fx_conversion` sums only `Share Amount` rows (a `Commission` row's ±0.01 is not summed); an unknown `BkAmountType` gives `booking_type_unmapped`.
21. Multi-execution fill (synthetic): `ExecutionPrice` ≠ `AveragePrice`. The price is `AveragePrice`, fees are summed over both TradeIds, and `executions` lists both.
22. Lifecycle: a disarmed pick that filled keeps its plan from the last armed line; refused / disarmed / expired / pending each give the right `never_filled` `state_reason`; with two armed lines, the later one wins.
23. A re-armed crid with a collapsed duplicate: offline gives `fold_order_uncertain`; broker mode takes `terminal` from the audit.
24. Offline, AMBA/GME shape older than `snapshot_horizon`: `state = unresolved`, outcome null `compacted_before_snapshots`; a newer pick with a missing stop fill gives `open` with `exit_not_in_journal`.
25. `--offline`: venue fields null `offline`; a stop fill with no marker gives `reason` null with `stop_amend_history_unavailable`.
26. Mixed `ts` formats all render as RFC 3339 `Z`; `touch_ts` is read.
27. A time cross-check within 1 ms passes, and one beyond 1 s warns.
28. Non-finite input gives null `non_finite` and still renders strictly.
29. `journal_audit_disagree`; `entry_not_in_journal`.
30. `counts.all` vs `counts.selected` under `--state`; `--limit` sets `truncated`; `--all` together with `--limit` gives `usage`; `--limit 0` gives `usage`.

**CLI contract**

31. The `_JSON_COMMANDS` row; output validates against the published JSON Schema.
32. Broker mode with the fake capability: exit 0, one JSON line.
33. No capability and no `--offline`: `broker_unsupported`, empty stdout.
34. `BrokerTransientError` gives exit 7. A bad `--since` gives `usage`.
35. The AST no-orders gate stays green.

**Golden case: VST:2026-09-21** (real LIVE lines: picks 44; entry_trails snapshot 97, 120, 121, 122; standalone_stops snapshot 9, 20, 48, 55-69; submissions 26. Audit rows for orders 5448021994 and 5448023092. Report and booking rows for TradeIds 6883674691 and 6885451891. Instrument tick for uic 7300542.)

| Field | Expected |
|---|---|
| entry | 6 @ 135.11, `2026-09-30T13:36:35.916Z` |
| `placed_stop` | 125.85 (audit `Placed`) |
| exit | 6 @ 138.66, `2026-10-01T13:59:21.248Z`, `trailed_stop`, `stop_level_at_fill` 138.82 |
| `pnl_cash` | 21.30 USD |
| `pnl_pct_of_spent` | 2.6275 % |
| `denominator_stop` / `risk_per_share` | 125.85 / 9.26 |
| `r_multiple` | 0.3834 |
| `holding_seconds` | 87765.332 |
| fees | commission −1.00 and −1.00; exchange fee −0.02 (`Amount`, in `Currency`, USD); `fx_conversion` −15.84 PLN (−7.79 entry, −8.05 exit) |
| `realized_fx` | entry 3.85078295 / −3121.68 PLN; exit 3.86282872 / 3213.72 PLN |
| `notional_spent_acct` | 3121.68 PLN |
| `pnl_cash_acct` | 92.04 PLN (3213.72 − 3121.68; FX charge inside) |
| `mfe_lower_bound` | from the max `trailed.peak` |

**Discrimination check** (repeat whenever the reason predicate changes):

- Remove the `trailed` lines. The result must be `stop_moved_kind_unknown`, never `disaster_stop`.
- Also remove the audit price-changing `Changed` rows. The result must be `disaster_stop`, whatever the fill price.
- Run offline with the `trailed` lines removed. The result must be `reason` null with `stop_amend_history_unavailable`.

## 10. Known limits

- **Lines lost before the first snapshot.** Compactions before the first boot that took snapshots (the earliest snapshot is stamped `20261001T201003Z`) dropped lines of several kinds:
  - `trailed` and `reanchored`;
  - `stop_filled` (for example ALB g2 stop-0, 3 @115.0);
  - `tranche_fired` (AMBA, ENPH, SMMT);
  - legacy `stop_placed` lines without `order_id`/`ref` (GME, SMG, OLN).

  Not every older pick lost lines: VST closed earlier that day and kept them. The cut is a compaction event, not a clock time. Broker mode recovers fills and stop levels from the audit, but not the kind of a stop move: EWTX and ASTS stay `stop_moved_kind_unknown`. Offline mode reports such picks as `unresolved`.
- **Touch times.** The venue has no touch event. `touched_at` is almost always null; there is 1 `touched` line on LIVE.
- **Entry times without the broker.** 0 of the 19 LIVE `fired` lines carry `venue_activity_time`. Offline entry times are detection times.
- **Manual-close detection** depends on manual orders having no reference. A machine order with no reference would be misclassified. Such orders exist on LIVE (uic 486), but all were cancelled.
- **Partial and multi-execution fills** have never happened on LIVE. Their handling rests on synthetic fixtures and `partial_fill_shape_unverified`.
- **Fees** cover commission, exchange fee and the FX conversion charge. Financing, dividends and withholding are not tied to a fill and are not included. The FX charge is read from `ConversionRateAccountCurrency`; that this field is the conversion charge was inferred from arithmetic on 25 rows (§0 P3), not from Saxo's documentation.
- **SIM** has no fees, no executions and no report cross-check.
- **Attribution on a shared uic.** The account nets at end of day, so the venue does not pair a close with an open. The position link (`RelatedPositionId`) exists only on platform-placed manual closes (1 of 41 LIVE fills since 2026-08-01). Everything else that a reference does not own goes through FIFO, with a warning. No real overlapping case exists on LIVE.
- **Identical no-`ts` lines** collapse in history dedup, which can change offline `terminal` / `planned_*` / `window_end` of a re-armed crid (`fold_order_uncertain`).
- **Report endpoints:** rate limits not measured. Whether the report dates are UTC or account-local is not known (§0 P2), and same-day availability was not measured; the padding and `report_row_missing` cover both.
- **SIM journals** were not probed. Only LIVE was.
- **`closedPositions` is not available on this account.** `/port/v1/closedpositions` answers HTTP 400 `ClosedPositionNotAccessibleInEndOfDayNettingMode` on LIVE, so it cannot be a v2 cross-check source here.

---

## Review record

R1 = reviewer 1 (LIVE data probe). R2 = reviewer 2 (code and consumer review).

| Finding | Source | Verdict | Change |
|---|---|---|---|
| The TP rule cannot work: the ref is `u<uic>-tpN-sell`, `tp_label_from_tag` never says no, and `tranche_fired` was lost for AMBA/ENPH/SMMT | R1 high | Adopted | §4.4 ownership table: `_TP_REF_RE` plus a uic-prefix check; tie through `tranche_fired`, otherwise the uic rule or `ambiguous_attribution`; tests 6 and 7 |
| `tp_label_from_tag` cannot test; OCO legs and bracket children are missing | R2 high | Adopted | §4.4: OCO `-stop`/`-tp` rows and `exit_order_ids`; label function used for rendering only; test 8 |
| Compaction lost `stop_filled`/`tranche_fired`/legacy `stop_placed`; offline would report 7 of 13 as `open` and hint at manual closes; §10 contradicted VST | R1 high | Adopted with a change: a `state` value `unresolved` instead of a null `state`, because `state` is the filter key and must not be null | §3.2 `snapshot_horizon`; §4.6 `unresolved`; warning renamed `exit_not_in_journal`; §10 rewritten; test 24 |
| No pick lifecycle; reading through the fold loses the plan of disarmed and refused picks | R2 high | Adopted. `read_pick_fold` latest-wins was confirmed in the R2 evidence. | §3.4 `pick_status*`; §4.2 plan from the last armed line with an intent; `never_filled.state_reason`; test 22 |
| No FX, no account-currency amounts, no planned qty | R2 high | Adopted. The account-currency P&L is null until a realized rate is read; the sizing rate is reported but never used as a realized rate (rule 2). | §3.4 `sizing_fx`, `instrument.*_currency`; EntryTier `planned_qty`; §5 `*_acct` fields; probe P3 |
| `ExecutionPrice` with a cumulative qty is wrong for multi-execution fills; fees assume one TradeId per order | R2 high | Adopted. No LIVE multi-execution fill exists, but the rule is wrong by construction. | Fill uses `AveragePrice` and `executions`; fees summed over all TradeIds; test 21 |
| Ownership must come from the audit reference, not from journal lines (GME, RHI, UBER/ALB multi-stop) | R1 medium | Adopted | §4.4 ownership table; "price-changing" judged per order id; tests 10 and 13 |
| The `disaster_stop` equality fails on tick rounding; the tick has no source | R1 medium | Adopted. Tick source verified: `_tick_size_for` + `get_instrument_details` exist on main. | Broker mode decides from rows only; `placed_stop` field; tick from `venue.instrument`; R stays on the plan stop |
| Fee split for a partly attributed trade; bookings shape (`BkAmountType`, signs, `AmountUSD`) | R1 medium | Adopted. Pro rata published as a rule, `derived` with `fee_prorated`. | §4.7 type mapping; §5 fees, signed; golden case expects negative values; test 19 |
| The offline fallback infers a moved stop from the fill price | R1 medium | Adopted | §4.4 offline: `reason` null with `stop_amend_history_unavailable`; test 9 (3-tick slip, broker mode) and test 25 |
| FIFO is assumed without evidence; the position link is unused | R2 medium | Adopted | §4.5 order: position link (if P1 confirms) before FIFO with `attribution_fifo_assumed`; probe P1 |
| The vocabulary and fields do not line up with the replay | R2 medium | Adopted | Outcome renamed to the replay's names, with `denominator` (spec §5.1), `notional_spent`, `mfe_lower_bound`; mapping table to replay events and to `trade_alerts.ExitReason`; test 16 |
| Tier windows and terminal times are missing; `ts` formats are mixed; `touch_ts` | R2 medium | Adopted | EntryTier `window_end`, `terminal_at`, `terminal_at_source`; §3.1 RFC 3339; `touch_ts`; test 26 |
| No versioning rule; `journal` is not in the source list; no unit or currency; no JSON Schema | R2 medium | Adopted | §3.0 rule; `Measured.unit`; `journal` removed; JSON Schema published and tested (test 31) |
| Fold takes strings; malformed lines counted twice; dedup collapses re-arms | R2 medium | Adopted | §4.1 items 1, 3 and 5; `fold_order_uncertain`; tests 1, 2 and 23 |
| Report date semantics are not verified; `--since` and the window use different fields | R2 medium | Adopted | Probe P2; report window padded one day each side and filtered by execution time |
| `trailed`/`amend_ok` lines carry only uic and ts, so matching can hit another position | R1 low | Adopted | §4.4: a marker must fall between the stop order's `Placed` and `FinalFill` on the same uic; test 11 |
| The `plan_shape` enum misses schema v1; 9 of 13 picks are percent-sized | R1 low | Adopted | `plan_size_shape`, `plan_source`, `plan_schema_version` copied verbatim; §4.2 wording fixed; test 18 |
| `__count` is the page length; the LIVE `__next` link fails | R1 low | Adopted (confirms the r1 fix) | §4.7: never read `__count` as a total; test 3 |
| Audit vs report time differ by about 1 ms | R1 low | Adopted | Audit time is canonical; 1 s cross-source tolerance constant; test 27 |
| No real overlapping case on a shared uic; UBER's manual Buy should be listed on its own | R1 low | Adopted | Test 20 labelled synthetic; real UBER fixture with `manual_open` (test 19) |
| Machine orders without a reference exist; entries are found only from the journal; mixed `ts` types | R1 low | Adopted | Evidence string `no_external_reference`; audit-found tiers with `entry_not_in_journal`; UTC conversion; test 29 |
| Failure table wrong: `env_ambiguous` unreachable; composition failure is `unclassified`, not `live_refused`; `--limit 0` breaks the precedent | R2 low | Adopted. Verified on origin/main: broker.py:619-621 uses `_fail`; broker.py:665-667 is the only `env_ambiguous` site; broker.py:2089 refuses `limit < 1`. Classifying the shared composition failure is a separate PR, because it changes every LIVE read command. | §6 table corrected; §2 `--limit >= 1`, default 200, explicit `--all` |
| Defaults work against the consumer; are counts taken before or after `--state`? | R2 low | Adopted | Default `--state all`; `counts.all` and `counts.selected`; consumer example in README |
| The disaster-stop test should use the placed stop, not the plan stop | R2 low | Adopted | `placed_stop` (audit `Placed`, falling back to `stop_placed.stop_price`); the R denominator stays `spec.disaster_stop` per replay §5.1 |
| File-named sources tie the schema to the keeper's internals before the repo split | R2 low | Adopted | §3.3 role names; ownership after the split stated in §8 |
| VST, EWTX, ASTS, UBER, ALB g2, RHI, LULU, QUBT values and contract plumbing confirmed | R1, R2 confirmed | No change needed | RHI open question closed (§4.6); golden values kept, with fee signs updated |
| r3: P1 — `PositionId` never links; `RelatedPositionId` does, on manual closes only (QUBT; LULU's rejected Sell) | LIVE probe 2026-10-03 | Applied | §4.5 rule 2 keyed on `RelatedPositionId` only; tests 20, 20a |
| r3: P2 — inclusive `ToDate`; filter on `TradeDate` / booking `Date`; UTC vs local and same-day lag not measurable | LIVE probe 2026-10-03 | Applied | §4.7 padding kept; `report_row_missing` + `report_lags_audit`; test 20b |
| r3: P3 — `ConversionRate` and `AmountAccountCurrency` on every row; FX charge is `ConversionRateAccountCurrency` on `Share Amount`, no FX `BkAmountType` | LIVE probe 2026-10-03 | Applied | §3.4 `realized_fx`, `fees.fx_conversion`; §5 `*_acct` filled; `fee_prorated` renamed `booking_prorated`; golden case PLN values; test 20c |
