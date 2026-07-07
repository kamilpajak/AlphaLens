# Options telemetry on thematic candidates — design memo

**Status:** LOCKED (design approved in-session 2026-07-07; implementation pending)
**Date:** 2026-07-07
**Author:** research session (brainstorming → design flow)

## 1. Problem / motivation

The thematic candidate pipeline stamps expert numerics (buffett, oneil,
panel) as forward-only telemetry and correlates them with EDGE outcomes at
N≥30. Options-market data is absent from that telemetry, yet the
options-implied class holds the **strongest prior in the entire research
program**: of 15 paradigm-class failures, the only signals that ever
approached the bar are

- Cohen-Malloy opportunistic insider Form-4 — pooled αt +2.71, PASS_MARGINAL;
- v9D options-implied retrospective (2009-2017 OOS) — pooled αt +2.45,
  INCONCLUSIVE, bounds-lower CI +2.15 excludes zero;
- pc_abnormal_volume retrospective (same window) — pooled αt +2.65,
  INCONCLUSIVE, bounds-lower CI +1.98.

Both positive classes are informed-trader flow. Logging options features on
candidates is therefore the best-motivated telemetry the project can add.

A second motivation: the EDGE signal-attribution July re-run kept ATR as a
Bonferroni-clear separator while ROIC/quality died as ATR proxies. The v9D
stack residualized on `rv_30d` (realized vol) as an equity control and still
scored ~+2.45 — evidence that options features carry information **beyond**
realized vol, i.e. they are not doomed to the ATR-proxy grave by
construction.

## 2. Goal

Stamp a small set of options-derived columns on the candidate parquet at the
thematic `score` stage, **display-only / telemetry-only**:

- zero change to selection, ordering, or the brief sort;
- forward-only (no historical backfill, no vendor history purchase);
- correlated with EDGE outcomes via the existing signal-attribution
  apparatus once N≥30 matured outcomes with non-empty chains accumulate
  (expected ~2026-09+, same cadence as buffett/oneil × EDGE).

This is a **transfer test**, not a replication: the ~2.2-2.45 αt class
ceiling was measured cross-sectionally (top decile of ~2000 optionable
names, 5d rebalance, 2009-2017). Whether the same features separate EDGE
outcomes among ~10 catalyst-conditioned mid-caps in 2026 is exactly the open
question the telemetry answers cheaply.

## 3. Data source decision

**yfinance option-chain snapshot at `score` time**, via the canonical
`alphalens_pipeline/data/alt_data/yfinance_client.py` (raw-HTTP ban enforced
by `test_no_raw_yfinance_http.py`).

- Zero cost, zero new keys. `Ticker.option_chain(expiry)` yields per-contract
  IV, OI, volume, bid/ask — sufficient for every column below.
- Forward-only snapshot is **PIT by construction** — the data-vendor PIT
  validation gate (mandatory for new *historical* sources feeding
  pre-registration) does not apply; no history is bought or trusted.
- Rejected alternatives:
  - **iVolatility reactivation** ($399/mo metered, key currently expired):
    clean pre-computed IV surfaces, but not before the telemetry shows a
    first-look signal. Becomes relevant only if a transfer signal appears
    and we want retrospective validation depth.
  - **Polygon options tier**: separate subscription from equities; same
    "not before first evidence" verdict.
- Known operational risk: recurring yfinance DNS storms on the VPS already
  stretched thematic-build (TimeoutStartSec 45→75min, #582). The fetch is
  ~10 tickers × 2 expiries per run and MUST be best-effort: a missing or
  failed chain stamps `options_chain_quality=NONE` + NaN features and never
  fails the stage.

## 4. Feature set (~12 `options_*` columns)

Mirror the **validated v9D stack** (`alphalens_research/screeners/
options_implied/features.py`) where computable from a snapshot, plus the raw
ingredients of the validated abnormal-P/C construction:

| Column | Definition | Snapshot-computable? |
|---|---|---|
| `options_ivx30` | ~30d ATM implied vol (interpolated across the two expiries bracketing 30d; nearest-strike-to-spot midpoint IV) | yes |
| `options_term_slope` | ~180d ATM IV − ~30d ATM IV (v9D `ivx180_minus_ivx30`) | yes |
| `options_vrp_ratio` | `options_ivx30` / 20d realized vol (v9D `ivx30_over_hv20`; HV from the split-adjusted grouped-daily store already read at `score`) | yes |
| `options_ivp30` | 1y rolling percentile of `options_ivx30` (v9D's PIT-validated normalization) | **no** — needs IV history; column exists from day one, stays NaN until per-ticker forward accumulation reaches a minimum window |
| `options_put_vol`, `options_call_vol` | total put / call contract volume across the near chain | yes |
| `options_put_oi`, `options_call_oi` | total put / call open interest across the near chain | yes |
| `options_spread_pct_atm` | relative bid/ask spread of the ATM contract — tradability / data-quality measure | yes |
| `options_chain_quality` | `NONE` / `THIN` / `OK` (no chain listed / OI below threshold / normal) | yes |
| `options_asof_expiry_near` | expiry date actually used for the 30d leg (debuggability of interpolation) | yes |
| `options_config_version` | poolability key, per the `insider_signal_version` / `novelty_config_version` doctrine | constant |

### Deliberate corrections vs the naive proposal

- **No raw put/call *ratio* column.** The validated signal is *abnormal* P/C
  (per-ticker log-ratio minus 60d rolling mean, residualized on equity
  controls); Pan-Poteshman showed the raw public P/C level is a null.
  Logging a raw ratio would be logging a known zero and inviting a
  false-negative verdict on the class. Instead we log the four raw
  volume/OI ingredients: candidates persist in briefs across many
  consecutive days, so forward logging itself builds the per-ticker series
  needed to compute abnormal-P/C at analysis time (min 30 obs — reachable
  for long-lived candidates).
- **`options_ivp30` ships as an accumulating column, not a launch blocker.**
  The percentile was v9D's strongest normalization but requires 1y of IV
  history; it fills in as the telemetry ages.

### NaN discipline

`options_chain_quality` is mandatory-stamped (never NaN). All numeric
columns are NaN when the chain is `NONE` or the specific leg is missing.
The EDGE-attribution analysis at N≥30 must condition on
`chain_quality=OK` — expected consequence: the effective N for options
covariates lags the headline matured-N because part of the $500M-$10B
bracket has no usable chain. This is accepted and is itself information
(the tradability of the class on our universe).

## 5. Integration points (implementation-plan level detail deferred)

- Stamp site: thematic `score` stage, alongside the buffett/oneil numeric
  stamping (same parquet, same run).
- New parquet columns need `LEGACY_CONTRACT_COLUMNS` registration
  (established gotcha from the insider-signal-v2 work).
- Sort-lock tests: `options_*` columns are display-only and must land in the
  non-sort allowlist path (`_NON_EXPERT_SORT_ALLOWLIST` generalization from
  PR-6 #559) — the brief sort must not see them.
- Django: no migration expected — columns ride the existing
  parquet→`rebuild_briefs_cache` path; card surfacing (if any) is a
  separate, later decision. Telemetry does not require display.
- All yfinance calls through `yfinance_client.py`; no new vendor client.
- Config version string proposal: `options-telemetry-v1-yf-snapshot`.

## 6. Success criteria / exit conditions

- **Ship criterion:** columns appear on new candidate parquets, thematic
  build stays inside its 75min budget, zero selection change (byte-identical
  candidate lists and ordering vs a control run modulo the new columns).
- **First-look criterion (~2026-09+):** at N≥30 matured outcomes with
  `chain_quality=OK`, run the same signal-attribution harness used for
  ATR/ma50: any options covariate must separate market-excess outcomes
  **after** the ATR partial (the ROIC/quality lesson) and survive the
  program-level multiplicity count.
- **Kill criterion:** if at first look every options covariate is absorbed
  by ATR (partial correlation ~0) the class verdict for *this setting* is
  "ATR proxy — no incremental value"; telemetry may stay (cheap) but no
  selection/exit work is justified.

## 7. Relation to the orphaned pc_abnormal forward observation

The 2026-05-05 pc_abnormal INCONCLUSIVE verdict activated a 12-month
paper-trade forward observation; ADR 0012 decommissioned the paper-trade
chain without addressing that obligation. This telemetry is the only living
broker-free vehicle observing the options class forward, but it is **not**
a 1:1 successor (different universe, different construction). The gap is
tracked as its own GitHub issue (filed 2026-07-07 alongside this memo);
resolving it (redesign of the observation, or formal retirement of the
obligation) is out of scope here.

## 8. Out of scope

- Any selection, ordering, gate, or exit change.
- Historical backfill or vendor history purchase.
- Card/SPA surfacing of the new columns.
- Reviving the L2 cross-sectional options screeners.
