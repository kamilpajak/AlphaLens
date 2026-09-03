# Insider cluster buying — retrospective pre-registration (draft for adversarial review)

**Status:** DRAFT (frozen for review 2026-09-03; becomes LOCKED only after zen + Perplexity adversarial review and a `docs/research/preregistration/params_insider_cluster_retro_2026_09.json` lock; NO run before LOCKED).
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

**Inference window 2013-2023: 4,661 events store-wide, 1,004 inside the PIT yaml universe, 1,891 distinct arrival sessions, 1,936 tickers.** USD-floor sensitivity (in-universe): 100k -> 1,004; 250k -> 633; 500k -> 370. Insider-count mix (in-universe): 2 = 577, 3 = 201, 4 = 93, >= 5 = 133. The >= 300 floor is cleared at every candidate USD floor, so §10 Q1 is a power-vs-strength choice, not a feasibility one. Note for the universe decision (§10 Q3): only ~22% of store events fall inside the PIT yaml — the Form-4 store universe is ~4x broader (also survivor-based); one 2024 yaml snapshot is degenerate (1 ticker) and must be excluded or repaired before any 2024 descriptive read.

## 8. Abort and deviation rules

Abort uncharged only for outcome-blind defects (row counts, join integrity, universe gaps, price-cache holes). Any feature-vs-outcome number emitted = the look is spent. Deviations are logged in the results section; the executable spec must not change after lock.

## 9. What this retrospective cannot show

Execution frictions (fills inside the 7-session TTL, slippage), the news gate on our own news store, and the returns of delisted names. The forward stage (parent memo §6 stage 2) covers the first two; the third needs delisted price history (paid vendor) — an owner decision, recorded as open.

## 10. Open questions for the owner (before lock)

1. Cluster USD floor 100k vs 250k (fewer, stronger events)?
2. Buy delisted price history (Polygon paid tier, one month) to remove the survivor caveat, or accept the disclosed upward bias for the discovery stage?
3. Universe: the PIT yaml (current-IWM reconstruction) vs the whole Form-4 store universe (broader, also survivor-based)? Default: PIT yaml, for comparability with #11.
