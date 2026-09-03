# Insider cluster buying — retrospective pre-registration (draft for adversarial review)

**Status:** COMPLETE (run 2026-09-03 19:20-19:45 CEST; results §13; planning rule = BUILD, marginal). Spec LOCKED 2026-09-03 — spec frozen in `docs/research/preregistration/params_insider_cluster_retro_2026_09.json` (ledger id `insider_cluster_retro_2026_09`); owner decisions §12.3 resolved: universe = full Form-4 store (PIT yaml as secondary cut), ledger = exploratory estimation look without verdict. Results: §13 (placeholder until the run).
**Parent:** [`event_sourced_lane_design_2026_09_03.md`](event_sourced_lane_design_2026_09_03.md) §6 stage 1, §12 data readiness.
**Class:** informed-trader flow, Form-4 based — shares data with paradigm #11 (Cohen-Malloy opportunistic), so this is NOT a fresh class for multiplicity: programme-level Bonferroni applies (`feedback burnt-holdout multiplicity compounds`).
**Compute:** small (Form-4 store + ~2,800 price parquets); runs on one host after rsync (Form-4 from the VPS, prices/factors from the Mac). Wall time minutes, not hours; the ">1h compute" review doctrine is applied anyway because the result feeds a multi-month forward lane.

## 1. Hypothesis (one primary)

H1: a basket of stocks entered at the first obtainable price after an **insider purchase cluster** becomes public earns a positive market-adjusted return over the next 20 sessions, relative to a universe-matched control cohort.

Direction and horizon are literature-fixed (JFR 2019 clustered purchases ~+2.1%/month vs ~1.2% solitary; post-SOX cluster study +0.5% over 20 days relative to non-cluster; FRL 2025 shrinkage under tradable-dollar limits). Prior used for the equivalence bound: **+0.5% to +1.5%**; smallest actionable effect **+0.5%** (net of the Saxo fee model this is roughly break-even for a $1-2k ticket).

## 2. Event definition (frozen)

From `~/.alphalens/form4_parquet/` (VPS SoT):
- legs: `transaction_code == "P"`, `acquired_disposed == "A"`, `is_amendment == False`, `is_officer or is_director` (10% owners excluded unless also officer/director), `transaction_price_per_share` present (no imputation), `transaction_shares * price >= 10,000 USD` per leg;
- **cluster** = >= 2 **distinct** `reporting_owner_cik` with qualifying legs whose `filed_date`s fall within 2 trading sessions of each other (the PIT clock is `filed_date`, never `transaction_date`); secondary definition stamped as a covariate: >= 3 distinct insiders within 5 sessions;
- cluster USD = sum of qualifying legs; **floor 100,000 USD** (frozen);
- **event date** = the `filed_date` of the leg that completes the cluster (the 2nd distinct insider); **arrival** = next session if that filing landed after the close (filing acceptance time unavailable in the store -> conservative rule: arrival = `filed_date` + 1 session for ALL events); **anchor price** = arrival-session OPEN from the price cache;
- **episode dedup**: one event per ticker per 20 sessions (the first wins);
- **universe**: ticker in the PIT yaml of the event month (`~/.alphalens/pit_universe/YYYY-MM.yaml`);
- **exclusions**: no price row on the anchor session; split guard 0.55-1.8 on day-over-day closes inside the window.

## 3. Outcome

- `car_20_event` = stock buy-and-hold return from the anchor open to the close of arrival + 19 sessions, minus the same for SPY (beta = 1). Secondary: `car_40_event` (arrival + 39), and IWM as a secondary benchmark (descriptive).
- Net variant: minus the Saxo fee model (`reference_saxo_fees_sim_vs_live_2026_07_29`) for a fixed ticket — descriptive.

## 4. Control cohort (survivor-bias mitigation)

For each event, draw **5 control firm-days** from the same PIT-yaml universe in the same calendar month, same size tercile (market cap proxy = price x shares from the price cache; if unavailable, universe-only match), excluding tickers with any qualifying `P` leg in [-20, +20] sessions. Same anchor/outcome construction. **Primary statistic = mean(car_20_event treated) - mean(car_20_event control).** Rationale: the price cache and the universe are survivor-only (§12 of the parent memo); the survivor premium is common to treated and control and cancels to first order. The residual bias (insider buying in firms that later delisted is missing from the Form-4 store itself, 12% delisted-issuer presence) is UPWARD and is stated in the verdict.

## 5. Windows and looks

- **Inference window: 2013-01-01 -> 2023-12-31** (universe >= 450 names). Split for stability only (not a second test): 2013-2018 / 2019-2023.
- **2024-01-01 -> 2026-03-31: BURNT for Form-4 features** (paradigm #11 final lock; the earlier insider cluster screener); reported descriptively, no p-value.
- 2009-2012: universe too thin (4-450 names); excluded, printed as a count.
- One primary test. Bar = the programme-level naive Bonferroni threshold in force at lock time (ledger count; ~|t| >= 3.1 at n ~ 28-30 tests). Verdict three-way: cleared / inconclusive (CI includes +0.5%) / evidence against (CI within (-0.5%, +0.5%)).

## 6. Inference

- Unit = event (already deduped per ticker/20 sessions); **clusters = arrival sessions**; restricted wild cluster bootstrap (Rademacher, B = 9,999) for the treated-minus-control mean; CR2 t reported alongside.
- Verification battery for a clearing result (all must pass): exact reproduce; leave-one-year-out worst-case p < 0.05; ticker-collapse (first event per ticker) sign retained and >= 50% magnitude; both sub-windows same sign; control-cohort re-draw with a second seed within 0.25 pp.
- Covariates stamped, never used as filters: cluster USD, n insiders, `n_senior` (CEO/CFO/Chair from `reporting_owner_name` title if present, else null), 20-day pre-event return (insiders buy dips), IWM-relative car.

## 7. Outcome-blind pre-flight (runs BEFORE lock, counts only)

Event counts per year 2009-2026 under the frozen definition; universe coverage per year; share of events with an anchor price; control-pool depth per month. If the 2013-2023 event count is < 300 the design is re-scoped BEFORE lock (raise the window, never lower the floor after seeing outcomes). No outcome is joined in the pre-flight.

### 7.1 Pre-flight result (run 2026-09-03 on the VPS store, counts only — no price or outcome joined)

Frozen definition applied to `transaction_year=2009..2026` (74,250 qualifying legs):

| Year | events (all store tickers) | in PIT yaml | arrival sessions | median cluster USD |
|---|---|---|---|---|
| 2013 | 174 | 17 | 119 | 292k |
| 2014 | 259 | 44 | 153 | 350k |
| 2015 | 304 | 60 | 157 | 351k |
| 2016 | 277 | 52 | 143 | 417k |
| 2017 | 291 | 46 | 151 | 466k |
| 2018 | 446 | 103 | 180 | 393k |
| 2019 | 460 | 96 | 182 | 422k |
| 2020 | 676 | 147 | 192 | 391k |
| 2021 | 535 | 80 | 206 | 688k |
| 2022 | 649 | 175 | 214 | 440k |
| 2023 | 590 | 184 | 194 | 353k |

**Inference window 2013-2023: 4,661 events store-wide (1,891 distinct arrival sessions, 1,936 tickers); inside the PIT yaml universe 1,004 events on 686 distinct arrival sessions and 489 tickers (max 9 events per ticker; 191 sessions carry >= 2 events).** (Corrected 2026-09-03 after review: the first draft attached the store-wide session and ticker counts to the in-universe subset.) USD-floor sensitivity (in-universe): 100k -> 1,004; 250k -> 633; 500k -> 370. Insider-count mix (in-universe): 2 = 577, 3 = 201, 4 = 93, >= 5 = 133. The >= 300 floor is cleared at every candidate USD floor, so §10 Q1 is a power-vs-strength choice, not a feasibility one. Note for the universe decision (§10 Q3): only ~22% of store events fall inside the PIT yaml — the Form-4 store universe is ~4x broader (also survivor-based); **six 2024 yaml snapshots (2024-01..2024-06) are degenerate (1 ticker each)** — the 2024-2026 descriptive read must use the month-union or repaired snapshots, and any prior audit that read per-month membership over H1 2024 should be re-checked.

## 8. Abort and deviation rules

Abort uncharged only for outcome-blind defects (row counts, join integrity, universe gaps, price-cache holes). Any feature-vs-outcome number emitted = the look is spent. Deviations are logged in the results section; the executable spec must not change after lock.

## 9. What this retrospective cannot show

Execution frictions (fills inside the 7-session TTL, slippage), the news gate on our own news store, and the returns of delisted names. The forward stage (parent memo §6 stage 2) covers the first two; the third needs delisted price history (paid vendor) — an owner decision, recorded as open.

## 10. Open questions for the owner (before lock)

1. Cluster USD floor 100k vs 250k (fewer, stronger events)?
2. Buy delisted price history (Polygon paid tier, one month) to remove the survivor caveat, or accept the disclosed upward bias for the discovery stage?
3. Universe: the PIT yaml (current-IWM reconstruction) vs the whole Form-4 store universe (broader, also survivor-based)? Default: PIT yaml, for comparability with #11.

## 11. Owner decisions (2026-09-03) — frozen into the spec

1. USD floor **100,000** primary; 250,000 as a strength check (reported, not a second test).
2. Delisted prices **not purchased** for this stage; survivor bias disclosed as UPWARD in every result table; the control-cohort difference is the primary statistic for exactly this reason.
3. Universe **PIT yaml** primary; full store universe secondary descriptive.
5. Cluster **2 distinct insiders within 2 sessions** primary; 3-in-5 covariate.

Status stays DRAFT until the adversarial review (decision 8) is applied; the review may amend §2-§6 BEFORE lock, never after.

## 12. Adversarial review trail (2026-09-03) and amendments

Reviewers: Perplexity `reason` (16 findings) and zen `deepseek/deepseek-v4-pro` thinking=high (10 findings), both hostile-referee framing, no go/no-go. Each finding was adjudicated separately from its remedy (house rule). Amendments below OVERRIDE the corresponding text in §2-§6 and are frozen into the params file at lock.

### 12.1 Adopted

| # | Finding (both reviewers unless noted) | Amendment |
|---|---|---|
| A1 | **Power.** At N ~ 1,000 in-universe events, se ~ 0.63 pp; P(|t| >= 3.1) is ~1% for a true +0.5%, ~6% for +1.0%, ~23% for +1.5%; 80% power at +1% needs ~6,200 events (iid, before clustering). "Inconclusive" is the near-certain outcome of a hypothesis test. | The retrospective is **re-classified as an ESTIMATION stage**: it reports the treated-minus-control effect with cluster-bootstrap CIs and NO three-way verdict, NO p-value against the programme bar. It is recorded in the ledger as an exploratory look (precedent: `options_retro_pilot_2026_07`) with an explicit "no discovery claim". The forward stage is the ONLY confirmatory test. A pre-registered **planning rule** (not a verdict) decides whether the forward lane is built: build iff point estimate > 0 AND lower 90% CI bound > -0.5 pp (net of the fee model); otherwise do not build. |
| A2 | **Mean reversion masquerades as drift** (insiders buy dips; controls matched only on month x size are healthier). | Matching is **mandatory on**: 20-session pre-event return, 6-month pre-event return, 20-session realized volatility, 20-session average dollar volume, plus calendar month and size tercile (proxy from the price cache). Nearest-neighbour within calipers, 5 controls per event; standardized mean differences reported before/after; the treated-minus-control estimate is ALSO reported with a regression adjustment on the same variables as a co-primary sensitivity. |
| A3 | **Control exclusion uses future information** ([-20,+20] insider legs). | Exclusion window becomes **[-20, 0] only**; a later qualifying purchase inside a control's window is stamped (censoring flag), never used to delete the control. |
| A4 | **Survivor bias is not cancelled by survivor controls**; conditioning on "no insider buying" selects healthier controls; residual direction is not sign-guaranteed although upward is the likely case. | The claim "cancels to first order" is **withdrawn**. The estimand is redefined explicitly: *the post-cluster return of issuers that survive to 2026 in a survivor-reconstructed universe*, NOT the return available to a historical investor. Sensitivity: re-estimate on the 12% delisted-issuer subset present in the store to bound the magnitude. Purchase of delisted price history stays deferred (owner decision 2). |
| A5 | **Anchor** `filed_date + 1 OPEN` is leak-free but throws away same-session drift for pre-open / early filings. | Fetch the EDGAR **acceptance datetime** from each event's filing header (accession numbers are in the store; ~1,000 requests through the canonical SEC client). Arrival rule: accepted before 09:00 ET -> same-session OPEN; otherwise next-session OPEN. Sensitivity: same-day CLOSE anchor reported alongside. Late filings (> 10 business days after the trade) excluded; filing lag stamped. |
| A6 | **Dollar thresholds are not economically scaled**; **Cohen-Malloy routine trades dilute**; **cluster completion vs first filing**. | Covariates stamped (never filters): cluster USD / market cap, cluster USD / 20-session dollar volume, Cohen-Malloy label of each buyer (the classifier exists), first-leg filing date (car from the first leg reported descriptively). |
| A7 | **Dependence**: single-way clustering by arrival session may overstate the effective N (same ticker repeats; sessions with many events). | Primary clusters stay arrival sessions; **two-way (ticker x arrival session) reported as a sensitivity**; cluster-size distribution and effective N printed. |
| A8 | **Basket vs retail**: an equal-weight basket of ~1,000 events is not what a 2-5 name retail book experiences. | Descriptive **retail simulation**: 3- and 5-name caps, equal-dollar, fee model, first-come rule for simultaneous signals; full distribution (median, p10/p90, max drawdown) reported. This is descriptive here and becomes the SIM-basket question at the forward stage (parent memo decision 6). |
| A9 | **Equivalence bound** must be net of costs. | The planning-rule threshold (-0.5 pp) is defined NET of the Saxo fee model for a fixed ticket; the fee model is the same in the power simulation and the estimate. |
| A10 | **Benchmark**: SPY leaves size/value/momentum/liquidity exposure. | Primary statistic is the matched-control difference (characteristic-adjusted by construction after A2); a Carhart-4F residual on the treated basket is reported as robustness; SPY- and IWM-relative raw car are descriptive only. DGTW is NOT adopted (no PIT book-to-market). |

### 12.2 Not adopted (with reason)

- "Use CRSP/Compustat delisting returns" — not available to this programme; handled by A4's estimand redefinition and the deferred purchase decision.
- "Make the first filing the event" — the strategy's signal is by construction the first moment the cluster condition is observable (second distinct insider); the first-leg car is reported descriptively (A6).
- "DGTW as primary" — requires PIT book-to-market we do not have; A2 + A10 cover the intent.
- Perplexity finding 1 ("1,004 events but 1,891 arrival sessions is impossible") — a REPORTING error in the pre-flight table, not a data error: 1,891 sessions and 1,936 tickers referred to all 4,661 store-wide events; the in-universe subset is recounted in §7.1.

### 12.3 New owner decisions required before lock

1. **Universe for the estimation stage.** With power now the binding constraint, the reviewers recommend the **full Form-4 store universe as primary** (4,661 events, se ~0.29 pp) with the PIT yaml as a secondary cut — the reverse of decision 3. Recommendation: accept the reversal; comparability with paradigm #11 is a weaker good than a CI half as wide.
2. **Ledger accounting.** Record the estimation stage as an exploratory look with no verdict (precedent: options retro pilot, 2026-07), i.e. it is COUNTED in the programme tally but produces no discovery claim; the forward stage is the single confirmatory charge. Recommendation: yes.

## 13. Results (placeholder — filled by the results commit, which must not touch the executable spec)

- Pre-flight (outcome-blind, run 2026-09-03 after the lock, cache complete): cluster events 2009-2026 = 6,750; inference window 2013-2023 = 4,662, of which 3,836 have a price history (830 missing tickers fetched through the yfinance cache on 2026-09-03; the remainder are delisted names yfinance cannot serve), 1,008 inside the PIT yaml, 86 late filings (> 10 business days) excluded, 1,792 also satisfy the 3-in-5 definition. EDGAR acceptance time known for 3,782 / 3,782 candidate events (31 needed the reporter-CIK fallback); only 6% were accepted before 09:00 ET, so ~94% of arrivals are next-session opens. Universe (store ∩ priced) = 3,236 tickers. Per-year priced events: 2013 122, 2014 192, 2015 223, 2016 210, 2017 210, 2018 363, 2019 379, 2020 569, 2021 443, 2022 569, 2023 556.
- **Primary estimate (treated − matched control, car_20, 2013-2023):** N = 3,134 events on 1,441 arrival sessions and 1,307 tickers (2,998 events with all 5 controls). Mean d20 = **+0.76 pp**, 90% CI **[+0.21, +1.31]**, 95% CI [+0.10, +1.42], bootstrap sd 0.34 pp (arrival-session clusters); ticker-cluster CI [+0.22, +1.30]. Treated car_20 +1.10%, matched controls +0.35%. Balance after matching: |SMD| < 0.05 on all four variables (ret_20d −0.014, ret_6m −0.028, vol_20d +0.049, log_dv_20d +0.002); treated and controls both sit ~−7% over the prior 20 sessions, so the mean-reversion confound is matched away, not assumed away. Regression adjustment (pooled rows, arrival clusters): β = +0.86 pp, t_CR2 = 2.63.
- **Planning rule:** net mean = +0.76 − 0.66 = **+0.10 pp > 0**; net lower 90% bound = +0.21 − 0.66 = **−0.45 pp > −0.50 pp** → **BUILD**, by 0.05 pp. The rule is pre-committed and is honoured; the margin is disclosed as thin. This is an ESTIMATE, not a verdict.
- **Shape of the effect:** median d20 = 0.00 and 50.1% of events positive — the +0.76 pp lives in the right tail (p95 d20 = +26%, p5 = −23%), not in a shifted centre.
- **Descriptives (frozen list):**
  - Sub-windows: 2013-2018 +0.69 pp [−0.16, +1.50] (n 979); 2019-2023 +0.79 pp [+0.11, +1.50] (n 2,155). Same sign, later half carries the precision. Yearly means noisy: 2014 −1.98, 2020 −1.56, others +0.0 to +2.0.
  - USD floor 250k: +0.83 pp [+0.15, +1.56] (n 1,977).
  - Insider count (dose-response): 2 insiders +0.31 pp [−0.35, +0.98] (n 1,777); **≥ 3 insiders +1.35 pp [+0.37, +2.34]** (n 1,357). 3-in-5 flag +0.80 pp [−0.17, +1.86].
  - PIT-yaml secondary cut: +0.91 pp [+0.07, +1.70] (n 954). Ticker-collapsed: +0.95 pp [+0.06, +1.88] (n 1,307).
  - Cohen-Malloy label of the buyers: no separation (all_opportunistic +1.18 [−1.04, +3.43] n 113; mixed +0.60; all_routine +0.97; unclassified +0.83) — consistent with the July finding that the routine/opportunistic axis does not discriminate in this population.
  - Horizons/benchmarks: car_40 treated +0.58 pp (no further drift after 20 sessions); IWM-relative car_20 +1.51 pp; car_20 from the FIRST leg's filing +1.42 pp (descriptive — earlier entry, before the cluster is observable).
  - **Retail simulation** (3- and 5-name caps, equal-dollar, fee model): 378 / 615 trades; mean net +0.52 / +0.55 pp; **median net −0.48 / −0.53 pp; 54% of trades negative; p10 −13.7 pp, p90 +14.6 pp.** A concentrated book experiences a coin flip with fat tails; the basket mean is not what a 3-name book sees.
- **Caveats (frozen wording):** estimand = survivor-reconstructed universe (issuers that survive to 2026; 12% of delisted issuers present in the Form-4 store; price cache survivors only) — the true historical figure is lower by an unknown amount; gross open-to-close returns before slippage; ~94% of arrivals are next-session opens (only 6% of filings accepted before 09:00 ET).
- **Deviations log:** none from the frozen spec. Operational notes: (i) the acceptance-time fetch used six parallel processes against the same cache (SEC latency-bound, total ~1.4 req/s); (ii) the acceptance fetch fell back to the reporter-CIK Archives path for 31 filings; (iii) 830 price histories missing from the cache were fetched on 2026-09-03 before the run (survivors only).

## 14. Reading (owner-facing, not a verdict)

The measured prior lands inside the literature band (+0.5 to +1.5%) at +0.76 pp gross per 20 sessions, with balance on the pre-event confounds and a dose-response in the number of insiders. Net of the Saxo LIVE fee model it is thin (+0.10 pp). The pre-committed planning rule says BUILD; the forward stage (parent memo §6 stage 2) is the single confirmatory test and the only place where our execution frictions and the news gate are measured. If the lane is built, the ≥ 3-insider subset is the natural pre-registered secondary; the routine/opportunistic label is not.
