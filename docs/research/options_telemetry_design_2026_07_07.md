# Options telemetry on thematic candidates — design memo

**Status:** LOCKED (design approved in-session 2026-07-07; amended same day per
Perplexity adversarial review — snapshot-window rule, skew added, ivp30 dropped;
implementation pending)
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
  ~10 tickers × 2-3 expiries per run and MUST be best-effort: a missing or
  failed chain stamps `options_chain_quality=NONE` + NaN features and never
  fails the stage.

### 3.1 Snapshot-window rule (Perplexity review, FATAL finding)

Yahoo option-chain fields are **session-mechanical**: `volume` is a
day-to-date counter that resets at the session open; `openInterest` updates
once per day around the open; bid/ask and the vendor `impliedVolatility`
are frozen or distorted outside regular trading hours (stale option mids
against an after-hours-moved underlying). Four of the six thematic-build
slots fall outside US regular hours, and — because briefs are dated T-1 —
the 12:30/16:30/20:30 UTC runs on day N would see day-N *intraday* volume
and misattribute it to asof N-1.

Rule: **options are stamped only by runs falling between the XNYS close for
the asof date and the next session open** (in practice the 00:30 / 04:30 /
08:30 UTC slots), using the existing exchange-parametrized calendar helper
(DST-safe). The first successful stamp per (asof, ticker) **freezes** the
row; later runs never restamp. Runs outside the window leave the columns
NaN with `options_snapshot_utc` null. Consequences: `volume` is the final
daily total (the only form valid for Pan-Poteshman-style abnormal-volume
work), OI is the day's cleared value, and quotes/IV are the at-close state
of the asof session. If all in-window runs fail (DNS storm), that asof
simply has no options row — best-effort, never a stage failure.

Fetch-failure vs no-chain semantics: a **failed fetch** (network error,
yfinance error) leaves the row unstamped (no `options_snapshot_utc`) so
that later in-window slots can retry. Only a fetch-OK ticker with no listed
chain stamps `options_chain_quality=NONE` and freezes — refetching a ticker
with no chain is pointless. A missing spot price (fetch OK but spot=None)
is treated as transient vendor state and also leaves the row unstamped for
retry.

## 4. Feature set (~16 `options_*` columns)

Mirror the **validated v9D stack** (`alphalens_research/screeners/
options_implied/features.py`) where computable from a snapshot, plus the raw
ingredients of the validated abnormal-P/C construction, plus the XZZ skew
(added on Perplexity review), plus audit columns:

| Column | Definition | Snapshot-computable? |
|---|---|---|
| `options_ivx30` | ~30d ATM implied vol (interpolated across the two expiries bracketing 30d; nearest-strike-to-spot midpoint IV) | yes |
| `options_term_slope` | ~180d ATM IV − ~30d ATM IV (v9D `ivx180_minus_ivx30`) | yes |
| `options_vrp_ratio` | `options_ivx30` / 20d realized vol (v9D `ivx30_over_hv20`; HV from the split-adjusted grouped-daily store, window ending at the newest ON-DISK session <= asof — the store lags its vendor by ~2 days, and a one-day-lagged RV window is literature-standard: staggered windows in Bollerslev-Tauchen-Zhou, backward HV in Goyal-Saretto; ~1-2 vol pts sampling noise, zero systematic bias) | yes |
| `options_skew_xzz` | Xing-Zhang-Zhao volatility smirk: OTM-put IV (moneyness closest to 0.95 within [0.80, 0.95]) − ATM-call IV (moneyness in [0.95, 1.05]), near expiry. Moneyness-based, no Greeks needed | yes |
| `options_put_vol`, `options_call_vol` | total put / call contract volume summed across the two bracketing expiries (near+far; single leg in the degenerate case), final daily totals per the §3.1 snapshot-window rule | yes |
| `options_put_oi`, `options_call_oi` | total put / call open interest summed across the two bracketing expiries (near+far; single leg in the degenerate case) | yes |
| `options_spread_pct_atm` | relative bid/ask spread of the ATM contract — tradability / data-quality measure (at-close quotes per §3.1) | yes |
| `options_chain_quality` | `NONE` / `THIN` / `OK` — see criteria below | yes |
| `options_asof_expiry_near` | expiry date actually used for the 30d leg (debuggability of interpolation) | yes |
| `options_atm_strike`, `options_atm_mid`, `options_spot` | audit columns: strike, option mid-price, and underlying spot used for the ATM point — Yahoo's `impliedVolatility` field has documented bugs, so IV must be recomputable from logged ingredients at analysis time | yes |
| `options_snapshot_utc` | UTC timestamp of the stamping snapshot (null when no in-window stamp succeeded) | yes |
| `options_config_version` | poolability key, per the `insider_signal_version` / `novelty_config_version` doctrine | constant |

### `options_chain_quality` criteria (pinned dimensions)

- `NONE` — no listed chain, or no expiry pair bracketing ~30d.
- `THIN` — chain exists but fails any of: ATM open interest below a minimum
  threshold, zero ATM volume on the asof session, ATM relative spread above
  a maximum, or only a single usable expiry (interpolation degenerate).
- `OK` — two expiries bracketing 30d with a near-ATM strike passing the OI /
  volume / spread floors on both legs.

Exact numeric thresholds are fixed in the implementation plan; the
dimensions above are locked here so the flag cannot degenerate into "any
contracts exist". **The flag measures IV-trustworthiness, NOT tradability**
(v2 clarification): its purpose is "is the midpoint IV a reliable telemetry
value", and the spread cap is 30% at the close — mid-cap ATM closing
spreads run 10-90% (median ~25% in the first live sample) under documented
end-of-day quote fading, OptionMetrics-based studies use no relative-spread
cap at all, and vega math bounds the worst-case midpoint-IV error at a 30%
spread to ~2-4 vol pts. The original 10% cap (an intraday large-cap
tradability number) rejected 12/12 real chains on day one. Tradability is a
different question the telemetry does not gate on; the continuous
`options_spread_pct_atm` column lets analysis slice by spread regardless of
the flag.

### Deliberate corrections vs the naive proposal

- **No raw put/call *ratio* column.** The validated signal is *abnormal* P/C
  (per-ticker log-ratio minus 60d rolling mean, residualized on equity
  controls); Pan-Poteshman showed the raw public P/C level is a null.
  Logging a raw ratio would be logging a known zero and inviting a
  false-negative verdict on the class. Instead we log the four raw
  volume/OI ingredients: candidates persist in briefs across many
  consecutive days, so forward logging itself builds the per-ticker series
  needed to compute abnormal-P/C at analysis time (min 30 obs — reachable
  for long-lived candidates). Caveat (Perplexity): raw totals without
  buyer/seller classification are a simplification of Pan-Poteshman's
  signed open-buy volume; day-over-day OI changes (derivable from the
  logged OI columns) partially recover direction.
- **`options_ivp30` (1y IV percentile) DROPPED** — Perplexity review, second
  FATAL-class finding. We log IV only on candidate days, which are
  catalyst-conditioned and systematically high-IV; a rolling percentile
  over that censored sample is a pseudo-percentile biased high by
  construction. The raw `options_ivx30` history accumulates anyway, so a
  *conditional* percentile remains computable at analysis time if wanted —
  no column needed, no false "IV rank" implied.
- **Vendor-IV sanity filter.** Per-contract `impliedVolatility` values that
  are near-zero or absurd (documented Yahoo API bugs, stale/zero-bid
  inversions) are excluded from ATM/skew selection before interpolation;
  a row whose ATM legs all fail the sanity filter degrades to `THIN`.

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
- Config version string: `options-telemetry-v1-yf-snapshot` (day one, 2026-07-08 stamps) -> `options-telemetry-v2-yf-snapshot` (30% spread cap + anchored lagged-RV window; v1 rows never pool with v2 per the poolability doctrine).

## 6. Success criteria / exit conditions

- **Ship criterion:** columns appear on new candidate parquets, thematic
  build stays inside its 75min budget, zero selection change (byte-identical
  candidate lists and ordering vs a control run modulo the new columns).
- **First-look criterion (~2026-09+):** at N≥30 matured outcomes with
  `chain_quality=OK`, run the same signal-attribution harness used for
  ATR/ma50: any options covariate must separate market-excess outcomes
  **after** the ATR partial (the ROIC/quality lesson) and survive the
  program-level multiplicity count.
- **Measurement-first skepticism (Perplexity review):** an early strong
  correlation is treated first as a suspected measurement artifact
  (chain-quality mix, earnings-in-window IV contamination, regime
  clustering of the first N observations), not as signal. Analysis must
  control for an earnings-within-30d indicator (derivable at analysis time
  from the AV earnings cache) because pre-earnings IV ramp + post-earnings
  crush structurally dominate 30d IV and term slope in a catalyst-selected
  sample.
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

## 8. Adversarial review record

Perplexity deep-research adversarial review ran 2026-07-07 (20 sources).
Accepted findings (applied above): §3.1 snapshot-window rule (session
mechanics of volume/OI/quotes; T-1 misattribution risk), `options_skew_xzz`
added (XZZ smirk is the strongest documented options predictor and is
specifically linked to future fundamental news — omitting it in a
catalyst-conditioned setting was a mistake), `options_ivp30` dropped
(censored pseudo-percentile), chain-quality criteria pinned, audit columns
+ vendor-IV sanity filter, earnings-window control in first-look analysis.

Rejected findings (with reasons): paid data (Cboe/CME) — contradicts the
"not before first evidence" sourcing decision; variance-swap / delta-based
interpolation — needs reliable full-smile IVs, overkill for a telemetry
tier (audit columns are the mitigation); "N=30 too small" — consistent
with existing doctrine, N≥30 is a first look, never a verdict;
selection-endogeneity — weaker here than the reviewer assumed, since the
EDGE replay is mechanical over the whole plannable population (no
discretionary trade filtering on options liquidity). The mandatory zen
pre-merge codereview still applies to the implementation PR.

**Second Perplexity review (2026-07-08, day-one live-data findings):** two
production findings were consulted after the first real stamp. (1) VRP was
structurally null: the grouped store's vendor serves session D only at
D+2 on the current plan, so the strict "window ends at asof" RV could never
complete. Verdict: anchor the RV window at the newest on-disk session <=
asof — one-day-lagged RV is standard practice (Bollerslev-Tauchen-Zhou use
deliberately staggered windows; Goyal-Saretto HV is backward-looking), no
systematic bias, ~1-2 vol pts noise; the fresh-but-split-UNADJUSTED
alternative (yfinance raw history) was rejected as strictly worse (a 1:10
reverse split fakes a ~230% daily return). (2) The 10% spread cap
misclassified all 12 day-one chains as THIN despite thousands of contracts
of OI: closing mid-cap ATM spreads of 20-30% are normal (end-of-day quote
fading), the literature uses no relative-spread caps, and midpoint IV stays
usable to ~30% (2-4 vol pts worst-case error via vega). Both fixes shipped
together as config v2.

## 9. Out of scope

- Any selection, ordering, gate, or exit change.
- Historical backfill or vendor history purchase.
- Card/SPA surfacing of the new columns.
- Reviving the L2 cross-sectional options screeners.
