# O'Neil expert (numeric-only) — locked design

**Status:** LOCKED 2026-06-13 (epic #541 PR-7, issue #549). Built via design Workflow
(3 feasibility probes → synthesis → 3 adversarial lenses → revision).

The second expert beside Buffett: a CANSLIM-**reduced** momentum/technical lens.
Display-only (out of the brief sort — PR-6 allowlist enforces) until a per-expert
Expert×EDGE correlation is validated (N≥30, ~2026-09+).

## What v1 ships: N + L + C/A (NOT R)

| CANSLIM letter | v1 input | Source |
|---|---|---|
| **N** (new high) | `oneil_pct_off_52w_high` (proximity to 52w high) | REUSE existing `technical_pct_off_52w_high` off the score-stage frame |
| **L** (leader / trend) | `oneil_ma200_slope_pct_per_day` (+ `oneil_ma200_distance_pct` display-only) | REUSE existing `technical_ma200_*` columns |
| **C / A** (earnings) | `oneil_earnings_growth_yoy_pct` | `EdgarFundamentalsStore.annual_series_as_of` latest-FY net-income YoY |
| **R** (relative strength) | **DEFERRED — not in v1** | — |
| **S** (supply) | dropped (float <25M collides with the $500M–$10B mcap bracket) | — |
| **I** (institutional) | dropped (no 13F data) | — |

### Why R (RS-approx) is deferred — three independent fatal reasons
The locked epic memo proposed RS = percentile-rank of trailing return over the
population-monitor grouped-daily disk cache (`adjusted=false`). The feasibility
probe + adversarial pass killed it for v1:
1. **Broken split math** — applying the monitor's consecutive-day `_SPLIT_SCREEN_THRESHOLD=0.18`
   band to a ~252-session ratio nulls every stock with a legit 1-year return outside
   ±18% — exactly the high-momentum leaders RS is meant to surface.
2. **Structurally ~100% None** — the grouped cache is empty on disk now and is written
   FORWARD-ONLY over each brief's ~42-session monitor window, so a 252-session-lookback
   snapshot essentially never exists. No `n-sessions-before(asof)` calendar helper exists.
3. **Biased when present** — the reference universe would be the survivorship intersection
   of two cached snapshots, not a market RS.

Shipping a ~always-None, wrong-when-present, biased-when-present column manufactures
false precision and invites "is this broken?" reports. **Re-activation** (off-table now):
a periodic grouped-daily prefetch job + ≥1y history + Polygon budget + a robust
cross-sectional ratio-outlier split detector (MAD z-score on log-ratios, not a fixed band)
+ the missing calendar helper.

## The 8 stamped columns (`ONEIL_COLUMNS`)
`oneil_pct_off_52w_high`, `oneil_ma200_slope_pct_per_day`, `oneil_ma200_distance_pct`,
`oneil_earnings_growth_yoy_pct`, `oneil_earnings_growth_near_zero_base`,
`oneil_new_high_split_suspected`, `oneil_data_coverage`, `oneil_score`.

6 float (None→NaN) + 2 "bool-as-float" (0.0/1.0/NaN) — Django `coerce_optional_bool`
restores the None/True/False tri-state. Every column is ALWAYS added (empty-frame and
all-None degraded branches both create all 8) so the brief parquet schema stays stable.

## Score (`compute_oneil_score`) — gated on the mandatory N term
Module constants (hand-chosen, **unvalidated** — labelled in code): `_W_NEW_HIGH=0.40`,
`_W_TREND=0.30`, `_W_EARNINGS=0.30`; `_COVERAGE_BASE=0.5`; `_NH_FLOOR_PCT=-25.0`,
`_TREND_FULL_SLOPE=0.10`, `_EARN_FULL_PCT=50.0`; `_NEAR_ZERO_BASE_RATIO=0.05`;
`_SPLIT_JUMP_THRESHOLD=0.35`.

1. **Hard N gate** — if `pct_off_52w_high is None` OR `new_high_split_suspected is True`
   → `oneil_score = None`. A score without proximity-to-high is not an O'Neil score.
2. **Term credits** (each clipped [0,1]): `f_nh = clip((pct+25)/25,0,1)`;
   `f_trend = clip(slope/0.10,0,1)` (≤0 earns 0, still present); `f_earn = clip(growth/50,0,1)`.
3. **Renormalized weighted sum over PRESENT terms** — `raw = 100·Σ(w·f)/Σ(w)`; a missing
   optional term does not silently deflate the score toward zero (the coverage shrink
   carries the thin-data penalty).
4. **Coverage shrink** — `data_coverage` = fraction of the 2 OPTIONAL terms (trend, earnings)
   present (0/0.5/1.0); `shrink = 0.5 + 0.5·data_coverage`; `score = raw·shrink`.

### Missing-input rules
- N absent (None or split-suspected) → score None (the gate).
- trend None → excluded from renorm + coverage; trend ≤0 → present, 0 credit.
- earnings None (<2 FY / net_income missing / prior_ni ≤ 0 sign-flip / near-zero base)
  → excluded from renorm + coverage. Near-zero base (`abs(prior) < 0.05·abs(latest)`)
  is EXCLUDED (uninformative exploding ratio), not damped; `oneil_earnings_growth_near_zero_base`
  records it for audit.
- `ma200_distance_pct` is display-only — NOT a basket term, NOT in coverage.

## Split contamination of the N term
`technical_pct_off_52w_high` is computed from `auto_adjust=False` raw closes, so an
intra-252d-window split inflates the peak and reads a name as falsely deep off its high.
Since N carries 0.40 weight, O'Neil runs an intra-window split screen over the SAME cached
raw-close window (`cached_daily_ohlcv(ticker, asof=asof)` — warm from the score pass, no new
yfinance call): if any consecutive day-over-day `|close_ratio−1| > 0.35`, set
`oneil_new_high_split_suspected=True` and treat N as absent for scoring (→ score None),
while still stamping the raw value into `oneil_pct_off_52w_high` for display.

## Collider flag (blocker #2) — earnings ↔ magic_formula_rank
`oneil_earnings_growth_yoy_pct` uses the SEC `NET_INCOME` concept that `magic_formula_rank`
(an ACTIVE brief sort key) also consumes — via `valuation_pe` (`market_cap/net_income_ttm`)
AND `roe_pct` (`net_income_ttm/equity`), 2 of the 6 rank components. O'Neil reads ANNUAL FY
net-income; magic_formula reads net_income_TTM — same concept, different aggregation window
→ **correlated, not collinear**. `shared_with_sort_key=True` is recorded on the earnings
fields. The deferred Expert×EDGE study MUST partial out `magic_formula_rank` as a CONTINUOUS
covariate (not a binary collinearity assumption). `operating_income_ttm` is a second shared
concept to watch if O'Neil ever adds an EBIT-based term.

## No new network
`EdgarFundamentalsStore(with_prices=False)` (O'Neil has no owner-earnings/mcap term) +
`preload(candidate_tickers)` once → disk-cache hit (companyfacts already on disk from the
score pass + Buffett pass). The split-screen ohlcv read hits the score-pass disk cache. The
technical columns are read off the frame. Fail-soft: a wiring failure or single bad ticker
yields all-None O'Neil columns and never aborts the score batch.

## Scope boundary
PR-7 **stamps the 8 columns into the brief parquet only** — they sit present-but-unread.
**Django surfacing is deferred to PR-8** (UI), which must touch THREE coupled sites in
lockstep: (1) add `'oneil'` to `_EXPERT_COLUMNS` in `briefs/ingest/parquet.py`; (2) register
the 6 float cols in `_EXPERT_FLOAT_COLUMNS` and the 2 bool cols in `_EXPERT_BOOL_COLUMNS` in
`briefs/ingest/coerce.py` (else the bool/float cells persist as strings); (3) extend the
frozen pin `test_expert_columns_match_frozen_buffett_tuple`. PR-7 ships a brief-parquet
schema-stability test so the present-but-unread columns don't break `rebuild_briefs_cache`.

## Open risks
- All scoring constants are unvalidated module defaults; verify on real data before any
  future sort wiring.
- `_OHLCV_LOOKBACK_DAYS=400` ≈ 251–275 trading rows vs the ≥252 requirement — a thin margin;
  with the mandatory-N gate a window-short name yields `oneil_score=None` (honest). Do NOT
  widen the shared screener constant from O'Neil.
- `_SPLIT_JUMP_THRESHOLD=0.35` is a heuristic — a compounding sub-0.35 split or a legit
  >0.35 gap could mis-classify. Validate on real split events or move N to split-adjusted
  closes in a later PR.
- On earnings-absent days O'Neil adds little orthogonal signal beyond reweighted technicals;
  `data_coverage<1.0` surfaces this. The Expert×EDGE study should segment on earnings-present
  vs earnings-absent days.
