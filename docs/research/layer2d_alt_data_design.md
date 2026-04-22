# Layer 2d — Alt-data screener design doc

**Status:** DESIGN LOCKED (2026-04-22) — universe, cadence, signal spec, factor model, cost model structure all locked per Perplexity R3/R4/R5. Cost model `k` coefficient requires empirical calibration in Phase 1/2 (known open item). Exit criteria thresholds locked.

**Chosen after:** Layer 2b closeout (#18, 2026-04-22). Perplexity R3 ranked Option A (alt-data) +20% nad alternatives; R4 locked Russell 2000 universe; R5 finalized signal spec / factor model / cost model.

**Design principle:** Every decision here is justified against a specific Layer 2b post-mortem lesson. If it can't be, it shouldn't be locked in yet.

---

## 1. Hypothesis (pre-registered)

**H1 (primary):** Russell 2000 insider cluster buys (Form 4, open-market purchases) generate net-of-cost OOS Carhart-4F α t-stat > 2, Bonferroni-adjusted for the multi-test plan below, with 60-180d holding period and monthly rebalance.

**H2 (secondary, counts toward Bonferroni budget):** Same as H1 plus sector-neutral portfolio construction.

**Literature anchor:** Kelley & Tetlock (2017) — insider cluster buys +180bps/6mo w small-cap universes. Lakonishok & Lee (2001) + Cohen et al. (2012) — insider α decays sharply outside small-cap. Jagolinzer (2009) — biotech-specific insider signal real but microcap execution eats it.

**Total hypotheses in this research program:** 2 (H1, H2). Bonferroni α_adj = 0.05/2 = 0.025 (t > 2.24 two-tailed). **No additional hypotheses allowed without re-running Bonferroni correction from scratch.**

## 2. Universe (LOCKED)

**Russell 2000 constituents** (~2000 tickers).

- **Why not biotech-only (Option A):** 8-10× fewer Form 4 filings per month (~40-60 biotech vs ~400-600 R2000), binary catalyst risk (FDA trial readouts ≠ risk-adjusted α), microcap biotech spreads 2× R2000 median → would repeat Layer 2b execution collapse.
- **Why not broad >$100M <$10B (Option C):** Lakonishok-Lee 2001 + Cohen 2012 show insider informativeness approaches zero at large-cap end. Adds ~1000 tickers of noise chasing statistical power.
- **Why R2000 specifically:** canonical small-cap universe matching Kelley-Tetlock empirical regime. Large enough dla Finnhub rate limits + spread tolerance; small enough aby insider info asymmetry retained.

**Universe refresh:** iShares IWM holdings or Russell official rebalance (June annually). **Pre-commit:** universe snapshot frozen per-quarter point-in-time. No retrospective sector additions — the Layer 2b 18-semis mid-audit mistake is explicit failure mode.

## 3. Data source (LOCKED)

**Finnhub `/stock/insider-transactions` free tier** (60 calls/min).

- **Backup/validation:** SEC EDGAR Form 4 direct (XBRL parse) jako audit trail — not production dependency.
- **PIT integrity:** use Form 4 `filingDate`, NOT `transactionDate`. Form 4 filings have ~2 business day SEC deadline post-transaction. Using `transactionDate` introduces look-ahead bias identical to Layer 2b SimFin Report-Date vs Publish-Date bug.

**Short interest:** status OPEN (see §6). Data source TBD — Finnhub `/stock/short-interest` is biweekly NYSE/NASDAQ publication.

## 4. Cadence (LOCKED)

| Task | Frequency | Finnhub calls |
|---|---|---|
| Full universe Form 4 scan | Quarterly | ~2000/quarter ≈ 166/month ≈ 5-6/day |
| Delta scan (portfolio + watchlist) | Monthly | ~150-200/month ≈ 5-7/day |
| Fundamentals / liquidity filter refresh | Quarterly | ~2000/quarter ≈ 5-6/day |
| **Total sustained** | — | **~15-20/day** |

Headroom under 60/min cap: ~99%. Daily Form 4 polling explicitly rejected as premature optimization — holding period is 60-180d, rebalance is monthly.

## 5. Execution model (LOCKED per Perplexity R5)

**Rebalance:** monthly (first trading day of month).

**Cost model — dual-track approach** (structure LOCKED; `k` empirical):

**Primary (simple, spread-dominated — retail R2000 empirical):**
```
one_way_cost_bps = (bid_ask_spread_bps / 2) + 5  # +5 bps buffer: adverse selection, limit slippage
round_trip_cost_bps = 2 × one_way_cost_bps
monthly_portfolio_drag_bps = round_trip_cost_bps × monthly_turnover_fraction
```
- Typical R2000 spread 5-10 bps → one-way 7.5-10 bps
- Expected monthly drag: 30-50 bps per round-trip (per R5 estimate)

**Secondary (Almgren-Chriss-style impact model — robustness check):**
```
cost_bps = 0.5 × spread_bps + k × sqrt(trade_size / ADV) × annual_vol × sqrt(horizon_days / 252)
```
- `k` coefficient: start at 0.05-0.10 (Perplexity R5 conservative prior). **Calibrate empirically w Phase 2** przez comparing strategy returns at mid-price fill vs. next-open fill + close exit.
- Report strategy net α under both cost models; use primary dla go/no-go; secondary jako sanity check.

**R2000 realistic baseline assumptions (R5):**
- Median bid-ask: 5-10 bps (higher than R4's 1-3 bps estimate; R5 more conservative, safer)
- Monthly turnover target: ≤25%
- Retail $100K-$1M portfolio, 20-30 positions
- Expected annualized drag: **80-150 bps/y** (more realistic than R4's 6-15 bps)
- Gross α K-T: ~360 bps/y → net α margin: **+210 to +280 bps/y** (still positive, but tighter niż R4 estimate — dobry sanity check)

**Fail conditions** (trigger before claiming α):
- Monthly turnover >25% sustained
- Realized slippage (empirical measurement) >2× model prediction
- Net α collapses >60% gross-to-net (fragile execution)

**Layer 2b lesson explicitly preventing regression:** Layer 2b `cost_model.py` used flat 100 bps/year for all scenarios, independent of turnover × spread. Layer 2d cost MUST scale z turnover × per-name spread × trade-size (validated with empirical slippage backtest w Phase 2).

## 6. Signal specification (LOCKED per Perplexity R5)

| Parameter | Value | Confidence | Rationale |
|---|---|:---:|---|
| Cluster size | ≥3 distinct insiders | Med | Kelley-Tetlock + cluster-buy literature consensus on joint signal |
| Cluster window | 30 days | Med | Aligned z monthly rebalance; 7-day (K-T original) tworzy timing misalignment z rebalance cycle |
| $ threshold | **count-only, NO $ floor** | Low (empirical decision) | Perplexity R5: fixed $ threshold arbitrary; scale-by-size adds degrees of freedom. Count-only cleanest. Reassess po Phase 3 jeśli signal dilution widoczne. |
| Transaction type | Code P only (open-market purchase) | Med | Excludes S (sales), M (option exercise), A (awards), G (gifts), D (div reinvest) |
| 10b5-1 filter | Exclude if plan adoption >90 days before execution | Med-High | R5: old 10b5-1 = mechanical execution, not real-time consensus |
| Insider role | Officers + directors only (exclude 10% holders) | Med-High | R5: joint executive+director clusters strongest; 10% holders (activists) trade na orthogonal info asymmetries |
| Weighting w clusterze | Equal-weight all qualifying buys | Med | R5: consensus signal derives from agreement count, nie seniority |

**Short interest: LOCKED (a) skip entirely for H1.**

R5: no empirical insider × SI interaction literature for small-cap. Disciplined hypothesis testing favors independent primary signal. Add jako H3 only if H1 fails AND formal Bonferroni budget expansion documented. Caveat: dla H3 należy zweryfikować że Finnhub free tier zwraca actionable SI data (ne >monthly stale) — do zrobienia tylko jeśli H3 reached.

## 7. Validation protocol (LOCKED structure, values TBD)

**Train/test split:** 70/30 time-based. Hold-out = last 30% of backtest window, strictly no-peek.

**Backtest window:** TBD (likely 2014-2026 to match Layer 2b window + Finnhub coverage availability).

**OOS walk-forward:** after in-sample tuning of H1/H2, single clean OOS run. If OOS α t < 2.24 (Bonferroni-adjusted), close strategy — no "re-tune and try again".

**Factor model (LOCKED per Perplexity R5):**
- **Primary:** Carhart-4F (Mkt-RF, SMB, HML, UMD) + HAC Newey-West SEs — publication standard dla monthly small-cap strategies
- **Robustness #1:** Fama-French 5F + UMD (6-factor) — adds RMW + CMA; flag α attenuation jako profitability/investment loading signal
- **Robustness #2:** Hou-Xue-Zhang q-factor (Q4: Mkt, ME, I/A, ROE) — alternative factor construction
- **Report all three.** Do NOT switch primary mid-validation. If Carhart α passes ale FF5+UMD α t drops >30% → document as profitability loading, count it against the strategy.
- **STR (short-term reversal):** skip unless primary Carhart shows meaningful STR loading.
- **Ken French vintage:** use historical data as-of each rebalance date (avoid Layer 2b current-vintage look-ahead bias).

**Delisted universe:** point-in-time Russell 2000 with delisted tickers classified M&A vs bankruptcy (lesson z `project_survivorship_probe`). Universe reconstruction or documented provenance PRZED backtestem.

## 8. Exit criteria (LOCKED)

**Capital deploy** if ALL of:
- OOS Carhart-4F α t > 2.24 (Bonferroni-adjusted for 2 hypotheses)
- OOS FF5+UMD α t > 2.0 (robustness — if Carhart passes ale FF5 attenuates >30%, document profitability loading, re-evaluate)
- OOS Q4 (Hou-Xue-Zhang) α directionally consistent with Carhart
- Net-of-cost α > 0 using primary cost model (spread-dominated, §5)
- Net α remains positive under secondary Almgren-Chriss cost model z `k` ∈ [0.05, 0.15] sensitivity range
- Bootstrap 10k-iter 95% CI excludes zero on net α
- OOS Sharpe net > 1.0
- No regime-dependent collapse (α t > 1.5 in each of bull/bear/flat sub-periods)

**Close strategy** if ANY of:
- OOS α t < 2.24 Bonferroni
- Net α negative at realistic cost
- Gross-to-net α drop >80% (signals execution fragility à la Layer 2b)

**Ambiguous zone** (α t 1.5-2.24): paper-track 6-12mo forward OOS data before final decision.

## 9. Multi-test discipline (LOCKED)

**Hypothesis count frozen at 2 (H1, H2).** Any new hypothesis = formal doc amendment + re-run Bonferroni from scratch.

**Forbidden mid-audit additions:**
- Sub-sector slicing (Layer 2b 18-semis failure mode)
- Market cap sub-buckets below/above arbitrary threshold
- Liquidity tier re-slicing
- Profitability overlays (unless pre-registered)
- "Combining" H1 + H2 into H3 post-hoc

## 10. Implementation plan

### Phase 1 — Data pipeline (est. 1 session, ~4h)
- Finnhub API client w `alphalens/alt_data/finnhub_client.py` with rate limiting
- Form 4 parse + cluster detection w `alphalens/screeners/insider/`
- Russell 2000 universe snapshot loader
- PIT filing-date filtering

### Phase 2 — Backtest harness integration (est. 1 session)
- New scorer `insider_cluster_scorer` conforming do `alphalens.backtest.engine.Scorer` signature
- Adapter w `alphalens/screeners/insider/backtest_adapter.py`
- Realistic cost model w `alphalens/backtest/cost_model.py` (upgrade from flat bps — Layer 2b lesson)

### Phase 3 — Single-shot validation (est. 1-2 sessions)
- In-sample tune (70% window) — MINIMAL iteration, prefer defaults from §6
- OOS run (30% hold-out)
- Carhart-4F HAC + Bonferroni
- Regime decomposition

### Phase 4 — Decision (est. 0.5 session)
- Apply §8 exit criteria
- If PASS: write production pipeline w `alphalens/screeners/insider/pipeline.py`, register w `alphalens.registry.SCREENERS["insider"]`, priority 12 (between watchdog=0 and themed=10)
- If FAIL: close, write post-mortem, return do pivot decision

## 11. What this doc is NOT

- **Not a promise of alpha.** Kelley-Tetlock is ~20-year-old evidence; decay since publication likely. Bonferroni may kill H1.
- **Not a replacement for Layer 1 watchdog.** SEC EDGAR watchdog is orthogonal, stays live.
- **Not locked on signal spec yet.** §6 defaults are strong priors, but user decision required before backtest.

## 12. Next action

**Design locked.** Proceed to Phase 1: Finnhub API client + Form 4 pipeline + Russell 2000 PIT universe snapshot. Single remaining user-side decision: Layer 2b launchd plist (disable vs paper-track) — orthogonal do Layer 2d implementation, can be resolved anytime.

## 13. Perplexity consultation trail

| Round | Date | Outcome |
|---|---|---|
| R1 | 2026-04-21 | Flagged regime bias + multiple testing in Layer 2b → triggered audit |
| R2 | 2026-04-22 | Multiple-testing + Bonferroni + delisted + universe PIT methodology |
| R3 | 2026-04-22 | CLOSE Layer 2b; ranked alt-data pivot +20% nad alternatives |
| R4 | 2026-04-22 | Universe = Russell 2000 (rejected biotech-only i broad); monthly rebalance; cadence quarterly + delta |
| R5 | 2026-04-22 | Signal spec (count-only, ≥3-in-30, officers+directors, code P, 10b5-1 >90d exclude); skip SI for H1; Carhart primary + FF5/Q4 robustness; dual cost model z empirical `k` |
