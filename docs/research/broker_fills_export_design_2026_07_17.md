# Broker-fills export contract (betlejem → AlphaLens) — broker-fills-v1

**Status:** LOCKED (2026-07-17)
**AL-side implementation:** `apps/alphalens-pipeline/alphalens_pipeline/feedback/broker_fills.py` (loader + contract validator ONLY — no A/B statistics by design, see §6)
**Betlejem-side implementation:** stdlib-only `alphalens_export.py` in `betlejem-mcp/` (sibling task, C-number ≥ C1615): pure functions + injected I/O, salt in `local/alphalens.env`, cron wrapper cloned from `cron_llm_cycle.sh`, read-only over journals, no `server.py` change.
**Pre-registration:** Cluster #22 in `edge_hypothesis_budget_2026_07.md` §3 (registered 2026-07-17, BEFORE any data landed). Collection registered in `shadow_collections_inventory_2026_07_16.md` §A + §C.

## 1. What this is

The betlejem paper-trading engine (IBKR paper, a friend's WSL box) exports its
closed-trade journal as a privacy-scrubbed, scale-free snapshot so AlphaLens can
(a) run the pre-registered broker-truth selection A/B (Cluster #22) and
(b) eventually calibrate `feedback/execution_cost.py` haircuts — whose docstring
admits "no real fills exist post-ADR-0012".

Source join (exporter-side): `thesis-{tradeId}.jsonl` (provenance, strategy,
targetNotionalUsd, commission, EXIT record) LEFT-joined by `llm-outcomes.jsonl`
(the DRIVING stream — covers ALL closes and carries quantity/stopLossPct/
takeProfitPct/timestamps) on `tradeId`. Only CLOSED trades export (an
llm-outcomes line exists). Divergence caveat: betlejem L636(f) admin corrections
fix H2 but NOT already-written jsonl lines — the export can disagree with
corrected H2 rows; jsonl is nonetheless the chosen source (append-only,
retention-free) vs H2 (live-locked, 90-day CLOSED purge). The export never reads
H2 at all (privacy: H2 carries account fields).

## 2. Parquet schema — `broker-fills-v1` (28 columns)

`schema_version` is `"broker-fills-v1"` on every row. Bumped only on column
add/remove or semantic change of an existing column, never on value drift.

| Column | Type | Null | Semantics |
|---|---|---|---|
| `schema_version` | string | non-null | Literal `broker-fills-v1` on every row. |
| `fills_source_version` | string | non-null | Poolability token per the `ladder_config.py` canonical-sorted-keys-JSON pattern: `{"broker":"ibkr-paper","schema":1,"source":"thesis-jsonl+llm-outcomes-jsonl"}`. Rows under different tokens never pool (ADR 0013 R3). Registered in shadow-collections inventory §C. |
| `export_run_ts_utc` | timestamp[us, UTC] | non-null | When the exporter ran; identical for all rows in one file. Distinguishes snapshot generations if stale files linger. |
| `trade_id_hash` | string | non-null | Hex `sha256(BETLEJEM_EXPORT_SALT + ":" + tradeId)`. THE dedup/join key — one row per closed trade, uniqueness enforced at export (fail loud on collision). Salted (not plain sha256) because betlejem tradeIds are plausibly sequential/enumerable, so unsalted hashes are invertible by enumeration; salt lives only in betlejem `local/alphalens.env`, is never transmitted, and is STABLE across runs so re-exports of the same trade hash identically. |
| `ticker` | string | non-null | Cleartext symbol (analytical payload; public datum). |
| `market` | string | non-null | Betlejem market profile wire value (e.g. US/GPW/futures), from llm-outcomes record. |
| `side` | string | non-null | Wire value from llm-outcomes (BUY/SELL). Cleartext. |
| `strategy` | string | nullable | ENTRY thesis `strategy`. Null when no ENTRY thesis file exists (autonomous, non-Pilot trades — llm-outcomes covers ALL closes, thesis only strategic). |
| `scanner_sources` | list\<string\> | nullable | ENTRY thesis `scannerSources` (ordered de-duped union of registry-claim scanner names, C1612+). NULL means the key is absent from the ENTRY line (pre-C1612 record or no ENTRY record); EMPTY LIST means post-C1612 with genuinely no sources. The null-vs-empty distinction is load-bearing for the cohort split — the converter must not coerce null to `[]`. |
| `source_claims` | list\<string\> | nullable | ENTRY thesis `sourceClaims` SourceType wire names (e.g. `ALPHALENS_FILTERED`, `PILOT_MANUAL`). Same null-vs-empty semantics as `scanner_sources`. |
| `provenance_cohort` | string | non-null | Derived enum: `POST_C1612` (ENTRY exists and carries the provenance keys), `PRE_C1612` (ENTRY exists, keys absent from the JSON line), `NO_ENTRY_RECORD` (llm-outcomes close with no thesis file). Makes the pre/post cohort split a one-column filter. |
| `fill_ts_utc` | timestamp[us, UTC] | nullable | llm-outcomes `fillTimestamp` normalized to UTC. AlphaLens derives arrival session → `brief_date` join key from this; the exporter does not pre-compute session dates (exchange-calendar logic stays AL-side in the exchange-parametrized calendar helper). |
| `close_ts_utc` | timestamp[us, UTC] | nullable | llm-outcomes `closeTimestamp` normalized to UTC. |
| `holding_seconds` | int64 | nullable | From llm-outcomes (nullable Long upstream). |
| `close_reason` | string | non-null | Wire value (STOP_LOSS, TAKE_PROFIT, etc.) from llm-outcomes; cross-checked equal to thesis EXIT `closeReason` when both streams joined (mismatch → `record_error`, llm-outcomes value wins). |
| `entry_price` | float64 | nullable | Per-share entry price from llm-outcomes (preferred over thesis EXIT `entryPrice`; if both present and diverge >1bp, `record_error` is set and the llm-outcomes value is kept). CAVEAT: this is broker avgCost only when the L309b backfill ran, else the decision spot — the journal carries NO flag distinguishing the two; treat as fill-price-of-unknown-basis. Per-share prices are public market data and carry no position-size information. |
| `close_price` | float64 | nullable | Per-share close price. CAVEAT (B197): for STOP_LOSS/TAKE_PROFIT closes this is the pre-computed lifecycle trigger price, NOT the broker fill — exit slippage is structurally unobservable in this export. |
| `close_price_is_trigger` | bool | non-null | Derived: `close_reason ∈ {STOP_LOSS, TAKE_PROFIT}`. Consumers must exclude trigger-basis rows from any exit-fill-quality computation. |
| `stop_loss_pct` | float64 | nullable | The per-symbol calibration stop pct the engine ACTUALLY used (llm-outcomes) — the authoritative risk denominator input for R. |
| `take_profit_pct` | float64 | nullable | Same source; defines designed reward geometry (designed R-max = `take_profit_pct / stop_loss_pct`). |
| `realized_r` | float64 | nullable | Net R multiple, computed AT SOURCE so no absolute PnL/qty ever leaves the exporter: `realizedPnl_net / (entryPrice × stopLossPct/100 × quantity)`. Null when underivable (see §5 fallbacks). Scale-free: quantity cancels between numerator and denominator, so it leaks no position size. |
| `pnl_pct_of_notional` | float64 | nullable | `100 × realizedPnl_net / denominator`, denominator per `pnl_pct_basis`. Computed at source. This is the FROZEN pre-registration metric. |
| `pnl_pct_basis` | string | nullable | `FILL_NOTIONAL` (denominator = `entryPrice × quantity`, from llm-outcomes — preferred, exists for all closes with qty) or `TARGET_NOTIONAL` (denominator = thesis `targetNotionalUsd` — fallback, strategic trades only). Null when `pnl_pct_of_notional` is null. |
| `commission_pct_of_notional` | float64 | nullable | `100 × commission / (same denominator as pnl_pct_of_notional)`. CAVEAT: commission is betlejem's MODELED floored round-trip cost (oracleLossPct-based pct vs CommissionModel min-aware floor), NOT broker-reported — this column calibrates AlphaLens haircuts against betlejem's MODEL, not against IBKR. |
| `commission_is_modeled` | bool | non-null | Always true under broker-fills-v1; column exists so a future broker-reported-commission export can flip it without a schema break. |
| `entry_fill_vs_thesis_spot_bps` | float64 | nullable | Adverse-signed entry offset: `sign × 10000 × (entry_price − thesis characteristics PriceSnapshot spot) / spot`, sign = +1 for BUY, −1 for SELL (positive = adverse). Null when the ENTRY characteristics spot is absent. The only entry-side fill-vs-reference signal the journal can actually supply — no order ids, limit prices, or NBBO exist in either stream. CAVEAT: when the L309b backfill did NOT run, `entry_price == decision spot` by construction, so the exact-zero mass is uninformative (no flag exists to separate "no slippage" from "no backfill"); calibration consumers should analyze the nonzero support only. |
| `joined_streams` | string | non-null | `BOTH` / `THESIS_ONLY` / `OUTCOMES_ONLY`. Expected: BOTH for strategic trades, OUTCOMES_ONLY for autonomous; THESIS_ONLY (ENTRY+EXIT thesis without an llm-outcomes line) is anomalous and also sets `record_error`. |
| `record_error` | string | nullable | HonestSummaries surface: parse failures, entry-price divergence >1bp between streams, gross-invariant breach (thesis gross != realizedPnl + commission within rounding), closeReason mismatch, THESIS_ONLY join. Errors are surfaced, never papered over with synthetic values; a row is emitted only if a real close record exists. |

## 3. File layout + delivery

One parquet PER EXPORT RUN (full-history snapshot), not per date:
`~/.alphalens/broker_fills/broker-fills-<YYYYMMDDTHHMMSSZ>.parquet` (UTC run
timestamp in the name; dir env-overridable as `ALPHALENS_BROKER_FILLS_DIR`
mirroring `ALPHALENS_LADDER_OUTCOMES_DIR`).

Why per-run over the house per-date pattern:
1. Paper-ledger volume is tiny (tens-to-hundreds of closed trades), so a full
   snapshot is trivially loadable.
2. Delivery is a manual/cron rsync hop from the friend's WSL box — a single
   file written tmp-then-`os.replace` and synced atomically avoids
   partially-delivered multi-file states.
3. Idempotent full-history re-export needs zero incremental bookkeeping in a
   stdlib-only betlejem script.
4. Snapshot semantics self-heal: when the C1612 provenance branch merges or an
   exporter bug is fixed, the next run supersedes everything without per-date
   invalidation logic.

**Reader contract:** the lexically-latest `broker-fills-*.parquet` WINS; older
files are prunable garbage, never merged. Idempotency/dedup: within a file,
`trade_id_hash` is unique (exporter fails loud on collision — no silent
last-wins); re-running the exporter on unchanged journals produces
byte-equivalent rows modulo `export_run_ts_utc`.

**Delivery mechanism (ops-owned):** rsync from the friend's machine into
`~/.alphalens/broker_fills/` on the consuming host (reverse direction of the
usual VPS→Mac cache sync). No AlphaLens service pulls; delivery is push/manual.

**Transport note:** the betlejem side (stdlib-only; `mcp>=1.0.0` is its whole
requirements.txt) emits the same records as `broker-fills-<runts>.jsonl`; the
AL-side converter/validator (`feedback/broker_fills.py`) writes/reads the
parquet and enforces `REQUIRED_COLUMNS ⊇ {trade_id_hash, ticker, side,
close_reason, provenance_cohort, schema_version}`. This document pins the
parquet contract; the jsonl is the same schema field-for-field. Any AL-side
parquet write uses tmp-then-`os.replace`.

## 4. Privacy rules

- **No share counts:** `quantity` from llm-outcomes is consumed only as an
  intermediate inside the exporter (R denominator, FILL_NOTIONAL denominator)
  and NEVER exported.
- **No notional amounts:** `targetNotionalUsd`, `entryPrice × quantity`,
  `positionValue` are denominators only — no USD-absolute column exists in the
  schema.
- **No absolute PnL:** `realizedPnl` (net) and `commission` appear only as
  `realized_r`, `pnl_pct_of_notional`, `commission_pct_of_notional` — all
  scale-free ratios computed AT SOURCE, before any data leaves the friend's
  machine.
- **No account values:** `ledgerNlvBefore/After`, `accountCurrency`, and every
  other H2 TradeLifecycle account field are out of contract entirely (the
  export never reads H2 — jsonl streams only).
- **No broker identifiers:** orderIds, `stpOrderId`/`tpOrderId` are not
  exported; the only trade identifier is `trade_id_hash`.
- **`trade_id_hash` = SALTED sha256** (`sha256(BETLEJEM_EXPORT_SALT + ":" +
  tradeId)`, salt in betlejem `local/alphalens.env`, never shared, stable
  across runs). Salted chosen over plain sha256 because tradeIds are plausibly
  low-entropy/sequential and plain hashes would be enumerable.
- **Cleartext by design** (the analytical payload): ticker, market, side,
  strategy, scanner_sources, source_claims, close_reason, timestamps/dates,
  per-share prices (public market data, zero position-size information), and
  all pct/bps ratios.
- **Defense in depth AL-side:** the `broker_fills.py` validator REJECTS any
  delivered file containing forbidden column names (quantity, qty, notional,
  target_notional, realized_pnl, commission_usd, nlv, position_value, order_id,
  account, …) — a mis-built export fails loud instead of ingesting private
  data. It also rejects ANY column not in the pinned v1 list (a column add
  requires a schema bump), so even a private field the tripwire does not know
  by name cannot slip through.

## 5. R-unit definition + fallback chain

Primary R unit (computed exporter-side; inputs never exported):
`risk_usd = entryPrice × (stopLossPct / 100) × quantity`, with `entryPrice`,
`stopLossPct`, `quantity` ALL taken from the llm-outcomes.jsonl record for the
tradeId (the authoritative per-symbol calibration pcts the engine actually
used — the thesis jsonl alone carries neither quantity nor stop/TP).
`realized_r = realizedPnl_net / risk_usd`, where `realizedPnl_net` is the
llm-outcomes `realizedPnl` (net of modeled commission; invariant
`gross = realizedPnl + commission` is checked against the thesis EXIT record
when joined, breach → `record_error`).

Guard conditions: `realized_r` is emitted only when `stopLossPct > 0`,
`quantity > 0`, `entryPrice > 0`; otherwise null. `realized_r` is exactly
scale-free (quantity cancels), so it is privacy-safe by construction.

FALLBACK CHAIN when risk is not derivable:
- (a) `stopLossPct` missing/zero but `quantity` and `entryPrice` present →
  `realized_r = null`, `pnl_pct_of_notional = 100 × realizedPnl_net /
  (entryPrice × quantity)`, `pnl_pct_basis = FILL_NOTIONAL`.
- (b) No llm-outcomes join at all (`joined_streams = THESIS_ONLY` — anomalous
  but handled per HonestSummaries) and thesis `targetNotionalUsd` non-null →
  `realized_r = null`, `pnl_pct_of_notional = 100 × realizedPnl_net /
  targetNotionalUsd`, `pnl_pct_basis = TARGET_NOTIONAL` (strategic trades only;
  `targetNotionalUsd` is a nullable Double).
- (c) Neither derivable → `realized_r = null`, `pnl_pct_of_notional = null`,
  `pnl_pct_basis = null`, `record_error = "r_and_pct_underivable"`.

When the primary path succeeds, `pnl_pct_of_notional` (FILL_NOTIONAL basis) is
ALSO emitted — both metrics always ride together when derivable, because the
pre-registered A/B metric is pct-of-notional while R feeds
exit-geometry/ladder comparisons.

Semantics anchor: on a clean full-fill STOP_LOSS close, `realized_r ≈ −1` minus
commission drag (`closePrice` is the trigger, per B197), so deviation of
SL-close `realized_r` from −1 measures betlejem's modeled commission, not
slippage.

## 6. Paper-truth calibration scope (what this data CAN and CANNOT say)

This export CAN calibrate:
- (a) **Commission drag** as pct-of-notional vs AlphaLens's
  `execution_cost.py` haircut constants — but only against betlejem's MODELED
  commission (min-aware floored round-trip), not IBKR-reported.
- (b) **Entry-side fill-vs-decision offsets** via
  `entry_fill_vs_thesis_spot_bps`, restricted to its NONZERO support (the
  exact-zero mass is a no-backfill artifact, not "zero slippage").
- (c) **Realized bracket geometry** — the `realized_r` distribution vs the
  designed `−1 / +take_profit_pct÷stop_loss_pct` bracket; limit/trigger
  semantics of the paper engine.

It CANNOT calibrate:
- **Real market impact** — `_MCAP_BUCKETS` stay unvalidated; the paper engine
  has no impact.
- **Resting-limit fill probability / adverse selection / queue position** — the
  `RESTING_LIMIT_ARMS` zero-haircut claim remains untestable; paper fills at
  touch are optimistic.
- **Effective spreads** — no NBBO reference exists in either stream.
- **Exit slippage** — SL/TP `close_price` is the pre-computed trigger (B197).

IBKR paper fills are simulated end-to-end: the metric is engine-consistent
paper P&L, not broker-true P&L. Any execution-cost-calibration LOOK on this
data charges the hypothesis-budget §4.1 policy annex separately from the
Cluster #22 selection look (one dataset, two independently-budgeted looks, per
ADR 0013 R4).

## 7. Forward-only provenance note

Arm membership for the Cluster #22 A/B requires the betlejem
`alphalens-integration/provenance-at-source` branch (C1612–C1614) merged on his
side — `scanner_sources` / `source_claims` are stamped at ENTRY-thesis write
time and are NOT retroactively reconstructable. All trades closed before that
merge land in `PRE_C1612` / `NO_ENTRY_RECORD` cohorts and are EXCLUDED from the
A/B by pre-registration. The cohort split is therefore forward-only: Arm-A rows
accrue only after the provenance branch is live. The loader normalizes
provenance-absent rows into an explicit cohort (never coerces null provenance
keys to empty lists) so the exclusion is a one-column filter, not a heuristic.

## 8. Join to AlphaLens outcomes

`LadderOutcome` key is `(brief_date, ticker)`; AL derives the session-date side
of the join from `fill_ts_utc` via its exchange-parametrized calendar
(`paper/calendar.py::session_on_or_after`, arrival session), keeping calendar
logic out of the exporter — `broker_fills.calibration_join_keys()` is the
helper. Ticker symbology mismatches (GPW suffixes, etc.) are a validator/join
concern, not a schema concern.

## 9. Pre-registration cross-reference

- `edge_hypothesis_budget_2026_07.md` §3 **Cluster 22** — broker-truth
  selection A/B, frozen metric = median per-trade `pnl_pct_of_notional`
  (FILL_NOTIONAL basis), one two-sided Mann-Whitney U, HARD floor N ≥ 30 closed
  POST_C1612 trades PER ARM, discovery tier. Registered 2026-07-17, before any
  data landed. The A/B deliberately charges a §3 cluster (selection-source
  comparison); a future execution-cost-calibration look charges §4.1.
- `shadow_collections_inventory_2026_07_16.md` §A `broker_fills` track row +
  §C `fills_source_version` poolability key.
