# NO_FILL root-cause + metric-rethink — design spec

**Status:** DRAFT (awaiting user review)
**Date:** 2026-06-15
**Owner:** Kamil
**Origin:** EDGE-tuning data-readiness audit (workflow `wf_1df6d581-73e`) surfaced that 7 of 8 matured EDGE outcomes are `NO_FILL`. User chose to investigate this first (N-independent lever), at depth "root-cause + rethink the metric".

---

## 1. Problem

The population-ladder monitor classifies most matured EDGE outcomes as `NO_FILL`: the entry order never executed. At maturity the split was `{NO_FILL: 7, TP_FULL: 1}` (N=8). Because the R-space targets (`realized_r`, `capture_gap`) only exist for *filled* positions, `NO_FILL` dominance starves every ladder-management metric (`realized_r` non-null 1/65) and is the binding constraint on EDGE-driven tuning today.

### Working hypothesis (from code, not yet from data)

The entry ladder is a **dip-buy / pullback model** applied to a **momentum / catalyst-driven book** — a structural mismatch, not a bug:

- Entry tiers are placed **strictly below** the signal-time close (`thematic/trade_setup/builder.py:69-112`, `ladder.py:18-55`): E1 ≈ `close − 0.5·ATR`, E3 ≈ `close − 2.0·ATR`.
- A tier fills only when `low ≤ limit` (price must *dip* to it) within a **7-trading-session** entry TTL (`paper/constants.py` `DEFAULT_ORDER_TTL_DAYS=7`; cutoff at/after `entry_expiry_ms` blocks fills — `feedback/ladder_replay.py:483`).
- The anchor is the signal close, **static** — it does not float up with the market.

So a name that gets a catalyst and rises never dips back to the pullback tier → `NO_FILL`. Contributing factors to test: short TTL (touched but after expiry), gap-up between signal date and arrival session, and minute-bar data gaps (artifact).

### Why it may not break selection feedback

`market_excess_return` is anchored to `reference_close` (arrival VWAP) and is computed **regardless of fill** (`edge/models.py:70-72`; set even on NO_FILL). So NO_FILL corrupts the *ladder* feedback but not the *selection* feedback. The investigation must quantify this and turn it into a metric decision.

---

## 2. Goals / non-goals

**Goals**
- Confirm, on the **real** outcome population (not just the 8 matured), *why* outcomes are `NO_FILL`, with a per-outcome cause classification.
- Produce the cross-tab `NO_FILL × sign(market_excess_return)` — the lynchpin for the metric decision.
- Lay out the metric-rethink decision: is ladder-based `realized_r` the right selection lens for a momentum book, or should `market_excess_return` be the primary selection feedback with entry-model treated as a separate question?

**Non-goals**
- No change to the production pipeline / ladder / monitor in this effort.
- No entry-model remediation here. If the evidence calls for it, that is a **separate** spec.
- No ML. This is descriptive diagnosis.

---

## 3. Data source (verified) — research-side, parquet-native

Everything is read **directly from the parquet stores on the VPS filesystem** (`~/.alphalens/`), not from Postgres. The edge ingest maps parquet columns 1:1 onto model field names (`edge/ingest/parquet.py` `row.get(field.name)`), so Postgres is only a copy of the stores. The investigation lives on the **research side** (`apps/alphalens-research/`), which is allowed to import `alphalens_pipeline` — so it reuses the existing pipeline decoders/readers instead of re-implementing them and never touches Django.

**Why not Django/Postgres** (rejected): the slim Django image does not install `alphalens_pipeline`, so a Django command could not reuse `parse_ladder` / `rs_history` / `paper.calendar` and would need re-implementation + a `~/.alphalens` mount inside the container. The research side has all of this for free.

| Source (on VPS, read-only) | Provides | Reader |
|---|---|---|
| `~/.alphalens/population_ladders/<date>.parquet` | per-outcome columns: `ticker`, `ladder_classification`, `terminal`, `reference_close`, `market_excess_return`, `forward_return`, `realized_r`, `chart_payload_json`, `ladder_config_version`, `theme` | `pandas.read_parquet` (columns = `LadderOutcome` field names) |
| `~/.alphalens/thematic_briefs/<date>.parquet` | `brief_trade_setup` (JSON) → full entry tiers E1..E3 + `disaster_stop` + `atr` | `alphalens_pipeline.paper.brief_loader._coerce_trade_setup` (handles str-or-dict) |
| `~/.alphalens/grouped_daily_history/<date>.parquet` | whole-market split-adjusted daily OHLC per session → in-window `[low, high]` per ticker | `alphalens_pipeline.data.rs_history.read_grouped_day(root, date)` → `{TICKER: {o,h,l,c,v}}`; root `rs_history.DEFAULT_RS_HISTORY_ROOT` |
| `alphalens_pipeline.paper.calendar` | window sessions: `session_on_or_after(brief_date, exchange)`, `advance_trading_sessions(session, n, exchange)` | direct import |

Trade-setup keys (authoritative, from `ladder_replay.parse_ladder`): `status=="OK"`, `disaster_stop`, `entry_tiers=[{limit, alloc_pct}]`, `tp_tranches=[{target}]`, `atr`. Tiers E1..E3 = `entry_tiers[i]["limit"]`.

**Note — chart payload is NOT the window source.** For the untouched NO_FILL rows (the ones the investigation most cares about) the monitor fetched no minute bars, so `chart_payload_json.bars` carries only the lead-in/trailing *context* window, never the in-trade `[arrival..entry_expiry]` path (`ladder_chart.build_chart_payload:520-535`, plan-preview branch). The grouped-daily store is therefore the **primary** in-window `[low, high]` source, not a fallback. `chart_payload_json` is used only opportunistically (it confirms E1 + status). No Polygon fetches anywhere; minute escalation only for ≤2 `AMBIGUOUS` cases (manual).

**Split-adjustment caveat:** grouped-daily is `adjusted=true` (adjusted near each session's own fetch date); brief-time tiers are adjusted as-of the brief date. A split between brief date and the window makes the two scales disagree. Flag any ticker with a split inside its window (rare at ~65 rows / ~2 months) and exclude it from `MOMENTUM_RAN`, counting it as `DATA_GAP`.

---

## 4. Per-outcome reconstruction

One row per outcome (all ~65 rows; NO_FILL classification does not need maturity). Derived columns:

- `reference_close`, `e1`, `e2`, `e3`, `disaster_stop` (tiers + stop from `price_lines`; `reference_close` from the model). `signal_close` is captured *only if present* in `brief_trade_setup`; nothing below depends on it.
- `min_low_in_window` over `[arrival_session, entry_expiry_session)` (7 sessions)
- `touched_e1/e2/e3` = `min_low_in_window ≤ tier` (touch eps `0.0025` to match `_TOUCH_EPS`)
- `gap_to_e1` = `(min_low_in_window − e1) / e1` (how far price stayed above the shallowest tier)
- `days_to_first_touch` (nearest tier) vs TTL = 7 sessions → "touched but after TTL"
- `arrival_drift` = `(first_session_open − reference_close) / reference_close` → gap-up vs the arrival anchor (self-contained; no `signal_close` needed)
- `window_bars_present` = data-coverage flag

## 5. Cause taxonomy (per NO_FILL)

1. `MOMENTUM_RAN` — `min_low_in_window > e1` within window; the dip-buy never triggered. (the hypothesis)
2. `TOUCHED_AFTER_TTL` — a tier price was reached, but first touch is after `entry_expiry_session`.
3. `GAP_UP_ARRIVAL` — first-session open already materially above E1 (`first_session_open > e1` by a margin), so the dip-buy starts out of reach at the arrival anchor.
4. `DATA_GAP` — bars missing/sparse over the window; artifact, not a real miss.
5. `AMBIGUOUS` — near-miss at daily resolution; escalate ≤2 to minute precision.

Precedence (first match wins): `DATA_GAP` → `GAP_UP_ARRIVAL` → `MOMENTUM_RAN` → `TOUCHED_AFTER_TTL` → `AMBIGUOUS`.

## 6. Population aggregation

- Classification mix over all ~65 (NO_FILL / filled-something / ongoing), grouped by `ladder_config_version`.
- Cause distribution among NO_FILL.
- **Cross-tab `NO_FILL × sign(market_excess_return)`** (matured only, since market_excess needs the forward window). If NO_FILL names skew positive market_excess → the ladder systematically discards the selection signal on winners.
- Count: how many matured outcomes are usable measuring selection via `market_excess_return` vs `realized_r` (8 vs 1 expected — quantify exactly).

## 7. Metric-rethink (the decision)

Frame the numbers into an explicit choice, with the evidence attached:

- **(i) market_excess as primary selection feedback.** If NO_FILL is dominated by `MOMENTUM_RAN` with positive market_excess, treat `market_excess_return` (fill-independent, already the EDGE headline) as the primary *selection* metric; demote ladder `realized_r` to a separate *execution / entry-model* question. Consistent with existing doctrine.
- **(ii) entry-model is mis-specified for a momentum book.** Scope (do not build) a future research question "how to enter": arrival/market fill tier, longer TTL, momentum-aware ladder. Separate spec.

The recommendation falls out of the cause distribution + cross-tab.

## 8. Deliverables (research side)

- `apps/alphalens-research/alphalens_research/diagnostics/nofill.py` — **pure** reconstruction + classification functions. No I/O: they take plain inputs (tier prices, `reference_close`, a per-session `{session: (low, high)}` map, the window session list, `entry_ttl_sessions`) and return the derived columns + the cause label. Sits beside the existing `diagnostics/` occupants (`survivorship_pit`, cyclicality).
- `apps/alphalens-research/scripts/diagnose_nofill.py` — thin driver: read the three parquet stores (`population_ladders`, `thematic_briefs`, `grouped_daily_history`) via the §3 readers, build the window with `paper.calendar`, call the pure functions, write a tidy per-outcome table (parquet/CSV under `~/.alphalens/diagnostics/`) and print the population aggregates (§6). Runs on the VPS or against rsync'd stores.
- `apps/alphalens-research/tests/test_nofill_diagnostics.py` — TDD unit tests on synthetic inputs (one per cause: `MOMENTUM_RAN`, `TOUCHED_AFTER_TTL`, `GAP_UP_ARRIVAL`, `DATA_GAP`, plus a touched-and-filled control). Run via `unittest discover` (repo convention).
- `docs/research/nofill_rootcause_metric_rethink_2026_06_15.md` — memo: per-outcome table, cause distribution, the NO_FILL × market_excess cross-tab, and the metric-rethink decision + recommendation.

## 9. Testing

TDD per repo convention. Synthetic `chart_payload_json` fixtures drive the pure functions:
- a `MOMENTUM_RAN` payload (all bar lows above E1),
- a `TOUCHED_AFTER_TTL` payload (low dips to E1 only on session 8),
- a `DATA_GAP` payload (empty/sparse bars),
- a touched-and-filled control (low crosses E1 inside the window).
Assert the derived columns and the cause label. The command stays thin (I/O only), so the logic that can be wrong is covered.

## 10. Risks / open points

- **In-window price path source** — the grouped-daily store is the primary (not fallback) source for the `[arrival..entry_expiry]` `[low, high]` (see §3 note). If a session's snapshot is absent (`read_grouped_day` returns `None`) or the ticker is missing from a present snapshot for a session inside the window, that row is `DATA_GAP` (data coverage, not a real miss).
- **Where entry tiers live** — `thematic_briefs/<date>.parquet` `brief_trade_setup` (decoded with `brief_loader._coerce_trade_setup`). Rows with no parseable setup (`status != "OK"`, no `entry_tiers`, or no matching brief row) are excluded and counted as `DATA_GAP`.
- **Split-adjustment scale mismatch** — see §3 caveat; split-in-window tickers → `DATA_GAP`.
- **Small N for the cross-tab** — market_excess sign-skew is descriptive at N≈8; report as a direction, not a test. The cause distribution runs on all NO_FILL rows (~tens) and is more robust.
- **Store freshness on the run host** — grouped-daily seed/topup is live since 2026-06-14; confirm the store spans the oldest outcome window before trusting `DATA_GAP` counts (an un-backfilled early session reads as `DATA_GAP`, not a real miss). Read-only throughout; no Polygon, no production-path writes.

## 11. Out of scope (explicit)

Entry-ladder redesign, TTL changes, market/arrival fill tiers, any production-path edit, the design-matrix builder, the proposal-frame ledger, and any ML. Each is its own future spec.
