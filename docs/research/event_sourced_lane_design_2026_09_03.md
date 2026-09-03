# Event-sourced candidate lane — initial Schedule 13D + insider cluster buying

**Status:** DRAFT (design frozen for review 2026-09-03; NOT yet adversarially reviewed; no code ships from this memo).
**Decision class:** new candidate SOURCE beside the thematic pipeline (second lane, own cohorts), evaluated through the existing brief -> ladder -> EDGE machinery.
**Owner decision trail:** this session (2026-09-03) — thematic source alone cannot lift the retained cohort above ~0 car_10 after subtraction (`edge_in_sample_probes_2026_09_03.md` §4); informed-flow signals live upstream (same memo §3); the owner asked for event sources whose drift fits the live execution windows (7-session entry TTL, 42-session maximum hold) and can be GATED by the news engine. Classes chosen by the owner: **initial 13D** and **insider cluster**. Guidance raises and cash-merger targets are recorded here as deferred.
**External review of the literature:** Perplexity `research` + `reason`, 2026-09-03 (citations in §9). No internal adversarial design review yet (§10).

---

## 1. Problem

Every Bonferroni-clear EDGE separator on the thematic list is an AVOID signal (ATR, MA50-extension, press-gate); the list's baseline car_10 is negative and the best in-sample avoid-filter lifts the retained cohort only to ~+1% (CI straddles 0). The only signals in the programme's history that approached the bar are informed-trader flows measured on a universe (Cohen-Malloy opportunistic insiders, alpha_t +2.71/+2.69), and those are structurally too sparse as a downstream attribute (8/202 episodes). The alpha, if any, has to enter at the SOURCE.

The thematic engine's failure mode is attention: candidates are selected because the press is already writing about them, so entries land on extended names that fade. A source whose trigger is a **structured, dateable filing** inverts that: the document is the trigger, and the news engine's job shrinks to verifying facts about it — the one task where our LLM instrument is reliable (fact extraction kappa ~0.68 vs judgment kappa ~0.21, `reference_llm_verification_literature_2026_08_05`).

## 2. Goal

Stand up a second candidate lane whose events are detected point-in-time from EDGAR, gated by fact verification, and pushed through the SAME scoring/brief/ladder/EDGE path as thematic candidates, under their own cohort keys, so that after a pre-registered N the programme can answer:

1. Does the event-sourced basket earn a positive market-adjusted return over the hold window (car_20 primary, car_40 secondary), consistent with the literature prior?
2. Does the fact-verification gate ADD value (gate-pass vs gate-fail arms, both logged), or does it only reduce N?

Non-goals: no change to thematic selection or ordering; no live capital (the `capital_deploy_clause` stays); no LLM judgment of "will it rise"; no short side (Saxo does not short cash equities).

## 3. Why these two classes (and what the literature actually supports)

Return convention throughout: **capturable drift = from day +2**, i.e. excluding day-0/+1 price discovery a follower cannot earn. Headline event-study numbers are NOT the prior.

| Class | Trigger (PIT) | Capturable drift prior (literature) | Frequency in $0.5-10B | Hold | Detectability today |
|---|---|---|---|---|---|
| **Initial Schedule 13D** (activist) | SC 13D (not 13D/A, not 13G) with Item 4 stating operational / strategic / board / capital-allocation / sale intent; since 2024-02 due within 5 business days of crossing 5% | Brav-Jiang-Partnoy-Thomas 2008: +7.2% over [-20,+20] but ~2.6% pre-filing, ~2.2% on days 0/+1, **~1.2% from +2 to +20**; Greenwood-Schor 2009: post-filing returns of targets that stay independent ~0, gains concentrate in eventual takeovers; sale/strategic-alternatives demands ~+1 to +3%, governance-only ~0 to +1%. **Prior: +1% (0 to +1.5%) over 20-40 sessions.** | ~1,288 initial 13Ds/yr SEC-wide -> ~20-40/month after activist Item 4 + listing + cap filter | 20-40 sessions | `edgar_detector` knows the 13D/13G form types (after #1263), but it polls the per-company Atom feed of the WATCHLIST tickers only — a universe-wide 13D source needs the EDGAR `getcurrent` feed by form type or the daily index (new code) |
| **Insider cluster buy** | >= 2 distinct officers/directors with open-market purchases (code P) within 2 trading days, or >= 3 within 5; excludes grants, exercises, conversions, 10b5-1 plans, amendments | JFR 2019 (1986-2014): clustered purchases ~+2.1% next-month AR vs ~1.2% solitary; post-SOX study: +0.52% over 20 days relative to non-cluster; 2025 FRL: filing-return effect shrinks or vanishes under tradable-dollar limits, concentrated in illiquid names. **Prior: +0.5 to +1.5% over 20-40 sessions for >= $500M names.** | 50-150/month before dollar/seniority filters; 20-60 after | 20-40 sessions | Form-4 store refreshed daily (universe 8,005 CIKs); cluster rule needs NO 3-year insider history, so it is not sparse like Cohen-Malloy |

Rejected for this lane (literature, 2010+): PEAD (dead for >= $500M since ~2006), index inclusion (effect gone, post-inclusion ~0/negative), OTC uplisting (weeks 4-6 negative), spin-off completion (payoff at 6-36 months), FDA approval (post-event "bio-run-down", ~-2% over 20 days), buyback authorizations (long-horizon, last decade ~0), analyst upgrades (-0.18% to +21), government contracts (announcement-day only). Deferred, not rejected: **standalone positive guidance raise** (needs a PIT consensus snapshot at detection; prior +0.75-1.5% over 15-30 sessions with mandatory exit before the next report) and **cash-merger target spread** (a separate risk sleeve, ~+0.5-1.5% carry with -20-50% tails).

## 4. Detection and PIT conventions

- **Event timestamp** = EDGAR acceptance time of the filing (first public dissemination). After-close filings map to the NEXT session; intraday filings to the same session with entry no earlier than the post-filing price.
- **Arrival session** = first session at which a follower could trade after the timestamp. **Anchor price** = first realistically obtainable price (next-session open or post-filing VWAP), never the close of day 0. This differs from the thematic `previous_trading_day(arrival)` anchor and must be a separate outcome column (`car_20_event`, `car_40_event`) so the two lanes are never pooled.
- **Entry TTL** = the live 7-session rule (`window_end` in the entry-watch journal); **maximum hold** = 42 sessions; the ladder's tranche/stop mechanics apply unchanged (the lane changes WHICH names arrive, never how the broker manages them — symmetric separation, `feedback_execution_tooling_no_selection_filter_2026_08_03`).
- **13D:** issuer CIK -> ticker via the detector's company-tickers map; filer identity from the reporting-person block; Item 4 text extracted from the primary document.
- **Insider cluster:** from the Form-4 store, `transaction_code == "P"`, `acquired_disposed == "A"`, `is_officer or is_director`, `is_amendment == False`, `filed_date` as the PIT clock (the store's `filter_records` uses `transaction_date`; the lane must key on `filed_date`).

## 5. The gate — fact verification only, logged in BOTH arms

The gate never decides "bullish". It answers checkable questions and stamps the answers; gate-fail events are still tracked (shadow arm) so the gate's incremental value is measurable at verdict time, exactly as `proposal_shadow` does for cluster 21.

**13D gate fields:** `is_initial` (SC 13D vs 13D/A vs 13G), `filer_matches_issuer_block`, `pct_owned`, `item4_demand_type` in {sale_or_strategic_alternatives, operational, board_or_governance, capital_allocation, passive_or_investment_only, unclear}, `prior_deal_announced` (a definitive agreement already public -> exclude from the drift arm), `activist_known` (filer on a frozen list of repeat activists, `activist_list_version`), `press_coverage_24h` (count, covariate only).

**Insider-cluster gate fields:** `n_distinct_insiders`, `n_senior` (CEO/CFO/Chair), `cluster_usd`, `cluster_span_sessions`, `all_open_market` (no grants/exercises/plans), `post_drawdown_20d` (insiders buy dips — a covariate, not a filter), `press_coverage_24h`.

**Common exclusions (hard, pre-registered):** market cap outside $500M-$10B at detection; ADRs/funds/SPACs; a same-ticker event within the prior 5 sessions (episode dedup at the source); `next_earnings_date` inside the first 10 sessions of the hold (earnings-window confound; the PIT column exists on the brief).

**Anti-patterns pinned:** no LLM score of "conviction", "credibility" or "likelihood of success"; no thresholds tuned after seeing outcomes; no use of the thematic press-gate as a positive filter (press is a covariate here — for 13D, more coverage means MORE immediate repricing and LESS left for a follower).

## 6. Outcomes, cohorts and the pre-registered look

- **Primary outcome:** `car_20_event` (event anchor, beta = 1 vs SPY, split guard 0.55-1.8). **Secondary:** `car_40_event`. Ladder `realized_r` is descriptive (fill-dependent).
- **Unit:** ticker-episode; **clusters:** arrival sessions; inference by restricted wild cluster bootstrap (the house standard).
- **Cohort keys:** `source` in {`13d_initial`, `insider_cluster`}, `event_gate_version`, `activist_list_version`, plus the existing `scorer_config_version` / `ladder_config_version`. Never pooled with thematic rows or with each other.
- **Family:** two classes x one primary test = 2 tests; bar 0.05/2. One programme charge per class (these are new POPULATIONS, registered like paradigm audits, not rows in the EDGE §3 covariate ledger — but the look must be appended to the §4 looks-log so the programme count is visible).
- **Pre-registered priors and verdict language:** H1(13D): mean `car_20_event` of gate-pass basket > 0 with equivalence bound |0.5%|; H1(cluster): same. Three-way conclusion as in `experts_last_look_2026_09.md` (cleared / inconclusive-retired-operationally / evidence against actionable effect).
- **Gate value test (secondary, descriptive unless pre-registered at build time):** gate-pass minus gate-fail difference in `car_20_event`, same clusters.

**Power (honest) and the two-stage design (amended 2026-09-03, owner decision):** sd(car_20)
~ 0.20 -> at N = 150 episodes per class the se of the basket mean is ~1.6 pp, detecting
>= ~4 pp at 80% one-sided power; at N = 300, ~2.9 pp. Against priors of +1% (13D) and
+0.5-1.5% (cluster), forward accrual alone would need >= 300 gate-pass episodes per class
(13D: 8-12 months). The design is therefore **two-stage**:

1. **Retrospective (discovery + OOS), pre-registered, weeks not months.** Both classes are
   reconstructable point-in-time from filings already on disk or in the EDGAR full index
   (§12). The retrospective estimates the drift on OUR universe and windows, replacing the
   literature prior with a measured one, and answers "does the class have any drift" in
   days (cluster) to ~2 weeks (13D). Each retrospective is its own pre-registration
   (`docs/research/preregistration/`), reviewed adversarially before the run, with the
   PIT-universe rule for > 100 tickers and the programme-level Bonferroni count.
   Cluster: `insider_cluster_retro_design_2026_09_03.md`. 13D: to follow (blocked on
   #1263 for the forward arm, not for the retrospective).
2. **Forward accrual = final lock**, at N ~ 100-150 gate-pass episodes per class (not 300),
   because the prior is then measured, not borrowed. Forward is the only stage that sees
   our execution frictions (7-session TTL fills, Saxo fees, slippage) and the news gate on
   our own news store (which starts 2026-05).

The verdict remains on the BASKET (equal-weight, all gate-pass names), never on the names a
human chose from it; human cherry-picking stays an augmentation overlay, outside the test.
The equivalence-bound three-way language applies to both stages.

## 7. Build sketch (for the implementation plan, after review)

1. **Event drain:** a NEW universe-wide source — EDGAR `browse-edgar?action=getcurrent&type=SCHEDULE+13D` Atom feed (or the daily `form.idx`) for initial 13D, and the Form-4 store for clusters; the existing detector is watchlist-scoped (per-company feeds) and stays the alerting layer and emits `event_candidates/<date>.parquet` with source + gate fields (PR, TDD, zen).
2. **Gate:** Item 4 extraction + demand-type classification through the canonical OpenRouter client (fact-shaped prompt, enumerated answers, `event_gate_version` = prompt SHA + schema); cluster rule as pure Python over the Form-4 store.
3. **Merge into the daily pipeline** as an additional candidate source behind the existing `score`/`brief` stages (source stamped through to `thematic_briefs`), with `car_*_event` outcome columns added to the population monitor keyed on the event anchor.
4. **EDGE/Django/SPA:** partition by `source` everywhere `scorer_config_version` is partitioned today; a source chip on the card (display-only).
5. **Pre-registration commit** (frozen gate versions, exclusions, N floors, priors) BEFORE the first VPS image rebuild that starts accrual.

Estimated 4-6 PRs. Nothing here touches selection or ordering of the thematic lane.

## 8. Risks

- **Announcement-return illusion:** measuring from day-0 close would turn price discovery into "drift". Pinned by the event anchor (§4) and a test that rejects day-0 anchors.
- **Takeover selection (13D):** returns concentrate in eventual sales; the drift arm must EXCLUDE already-announced deals and stamp `item4_demand_type` so the sale-demand subset is a pre-registered secondary, not a post-hoc rescue.
- **Illiquidity (cluster):** modern evidence says the effect lives in small illiquid names; the $500M floor and Saxo's fee floor (SIM $15 min) will haircut it. Report net of the fee model (`reference_saxo_fees_sim_vs_live_2026_07_29`).
- **Regime confounding:** both classes cluster in time (activism waves, post-earnings insider windows). Arrival-session clusters + LOBO worst-case in the battery.
- **Gate drift:** any prompt or list edit bumps `event_gate_version` / `activist_list_version`; cohorts are never pooled across versions (ADR 0013 R3 spirit).
- **Multiplicity creep:** two classes now; guidance/merger-arb later each add a charge. No "just one more class" mid-accrual.

## 9. Literature (via Perplexity, 2026-09-03; verify before citing in a verdict)

13D: Brav, Jiang, Partnoy, Thomas (2008, JF); Greenwood & Schor (2009, JFE); Bebchuk, Brav, Jiang (2015, Columbia LR); Becht, Franks, Grant, Wagner (2017, RFS); Polk et al. (2023, SSRN 4596959) on the 10->5 business-day deadline; SEC Release 33-11253 (2023). Insider clusters: Journal of Financial Research 42(2) 2019 (1986-2014 clusters); Finance Research Letters 72 (2025) on tradable-dollar limits; NBER w6913 (Lakonishok-Lee). Guidance (deferred): Ng, Tuna, Verdi (2013, RAST); Kothari, Shu, Wysocki (2009, JAR); Rogers, Skinner, Van Buskirk (2009, JAE). Announcement-day vs drift convention: all of the above; see also `paradigm_failures_postmortem.md` PASS_MARGINAL entry for the Cohen-Malloy audit design this lane deliberately does NOT reuse (monthly portfolio, 21-day hold, universe-wide).

## 10. Next steps (in order)

1. Adversarial design review (zen `deepseek/deepseek-v4-pro` thinking=high + Perplexity `reason`), per the ">1h compute" doctrine — this lane accrues for months, so the review is mandatory before the first PR.
2. Resolve §11, then flip Status to LOCKED in a follow-up doc PR with the frozen gate schema and N floors.
3. Implementation plan (writing-plans) -> PRs per §7.

## 11. Open questions for the owner

- Universe for the 13D arm: the $500M-$10B thematic bracket (consistency) or R2000 constituents (fidelity to the activism literature)? Default in this draft: bracket.
- Cluster definition: "2 in 2 sessions" (more events, weaker) vs "3 in 5" (fewer, stronger)? Default: 2-in-2 as primary, 3-in-5 stamped as a covariate, decided BEFORE accrual.
- Whether the SIM broker runs the gate-pass basket mechanically (equal-weight, all names) alongside the human overlay, so the basket verdict has a realized_r shadow. Default: yes, SIM only.

## 12. Data readiness — measured 2026-09-03 (VPS + Mac), not assumed

| Asset | Where | Coverage | Verdict for the retrospectives |
|---|---|---|---|
| Form-4 store `~/.alphalens/form4_parquet/` | VPS (SoT, daily incremental); Mac copy stale (2026-05-08) | 2008-2026; `filed_date` 100% populated; officer/director open-market `P` legs 4k-20k/yr on 500-1,900 tickers/yr; price missing < 1.1% | usable as the PIT clock (`filed_date`), **but survivor-biased**: the 8,005-CIK backfill universe holds only 202 of 1,727 issuers delisted 2007-2018 (12% present as issuers 2008-2018) |
| "R2000 PIT" `~/.alphalens/pit_universe/*.yaml` | Mac + VPS (207 monthly snapshots 2009-01..2026-03) | built by `build_pit_universe.py` from **current IWM holdings** back-filled by listing date: 4 names in 2009, ~430 in 2012, ~600 in 2015, ~750 in 2018, ~1,000 in 2023; **2024-01..2024-06 snapshots hold 1 ticker each (degenerate)** | a survivor reconstruction, not a true PIT roster; usable from 2013 on, with the control-cohort design below; every earlier "R2000 PIT" audit shares this property |
| S&P 1500 PIT `apps/alphalens-pipeline/data/sp{500,400,600}_pit/` | repo | 4 snapshots (2018, 2020, 2022, 2024), each labelled "FALLBACK proxy (iShares current membership — SURVIVORSHIP BIAS caveat)" | not a true PIT roster either |
| Delisting lists `~/.alphalens/survivorship/` | **Mac only** | `delisted_2021_2026.parquet` covers 2004-2026 (6,237 names, date + reason, no CIK); `delisted_2007_2018.parquet` 2,051 names with CIK | roster of the missing names exists; their PRICES do not |
| Price cache `~/.alphalens/prices/` (yfinance, per-ticker parquet OHLCV) | **Mac only** | 2,804 tickers, 100% of the PIT-yaml union, SPY/IWM/QQQ present, ends 2026-04/05; **0 of 2,051 and 1 of 6,230 delisted tickers present** | survivors only; delisted price history would need a paid vendor (Polygon paid tiers) — owner decision |
| Fama-French / momentum factors `~/.alphalens/factors/` | **Mac only** | daily through 2026-02-27 | fine (needs a refresh for 2026-03+) |
| Grouped daily (Polygon) `~/.alphalens/grouped_daily_history/` | VPS | 495 sessions (~2 years, free-tier cliff) | fine for the forward stage, too short for history |
| EDGAR full index `full-index/<Y>/<Q>/form.idx` | live, via `get_default_sec_client().get_text` | initial 13D per quarter: 854 (2015 Q1), 608 (2024 Q1), 572 (2026 Q2); form type renamed `SC 13D` -> `SCHEDULE 13D` from 2024 Q4 | 13D retrospective feasible; documents must be fetched and Item 4 classified |
| Live detector 13D feed | VPS `edgar-detect` | digest: 299 events, all Form 4 / 8-K, **zero 13D/13G ever**; the detector polls per-company Atom feeds for the WATCHLIST tickers, not the whole market | **bug #1263** (fixed in #1270): legacy form names only. Even fixed, the detector cannot feed a universe-wide 13D arm — that needs the `getcurrent`/daily-index source (§7 step 1) |

Consequences frozen into the retrospective designs: (a) inference window 2013-2023 (universe >= 450 names); (b) 2024-01 -> 2026-03 is BURNT for Form-4 features (paradigm #11 final lock + the earlier cluster screener) and is reported descriptively only; (c) survivor bias is handled by a **universe-matched control cohort** (non-event firm-days from the same survivor universe, same month, same size tercile) so the first-order survivor premium cancels in the treated-minus-control difference — the residual bias (distress-related insider buying is missing) is UPWARD and is disclosed; the forward stage is the unbiased check; (d) compute runs where the data are: Form-4 from the VPS, prices/factors/survivorship from the Mac — rsync to one host (runpod or the VPS) before the run.

## 13. Owner decisions (2026-09-03)

| # | Question (§11 / cluster memo §10) | Decision |
|---|---|---|
| 1 | Cluster USD floor | **100k primary**, 250k reported alongside as a strength check (pre-flight: 1,004 vs 633 events) |
| 2 | Delisted price history | **Not purchased for the discovery stage**; the upward survivor bias is disclosed in every table; revisit if the retrospective clears |
| 3 | Retrospective universe | **PIT yaml primary** (comparability with paradigm #11); full Form-4 store universe as a secondary descriptive read |
| 4 | 13D arm universe | **Russell 2000 (as in the activism literature)**, not the $0.5-10B bracket; the bracket becomes a covariate |
| 5 | Cluster definition | **2 distinct insiders within 2 sessions primary**; 3-in-5 stamped as a covariate |
| 6 | SIM basket alongside the human overlay | **Deferred** to the forward stage decision; the retrospective answers "is there drift" without it. The SIM basket exists only to observe OUR execution frictions (7-session TTL fills, Saxo fees, slippage) and the news gate on our own news store — things no historical test can measure. Decide after the retrospective result. |
| 7 | Detector bug #1263 | **Fix now** (small PR: map `SCHEDULE 13D/13G(/A)` in `FormType.from_sec_string` + tests + live probe) |
| 8 | Adversarial design review | **Yes** — zen `deepseek/deepseek-v4-pro` (thinking high) + Perplexity `reason` on both memos before DRAFT -> LOCKED |
| 9 | 13D retrospective | **Wait for the cluster result**, then decide |
| 10 | Cluster 21 (mechanical rule vs LLM) | **Read on the live v4 cohort at >= 30 brief-dates**, with `sector_excess_return` pre-registered as the secondary outcome (issue) |
| 11 | Short-interest telemetry | **Yes, small forward-only PR this month** (issue); first look at N >= 30 ~2026-11/12 |

## 14. Adversarial review amendments (2026-09-03) — lane-level

From the same review round as the cluster memo §12 (Perplexity `reason` + zen `deepseek/deepseek-v4-pro` high):

1. **Two-stage design clarified:** the retrospective is an ESTIMATION stage (no verdict, no programme-bar test); the forward accrual is the single confirmatory test per class. This removes the double-look on one hypothesis that the original §6 implied.
2. **`item4_demand_type` is a judgment call, not a fact.** The primary gate field becomes a **keyword rule** (regex over Item 4 for "sale of the company", "strategic alternatives", "board representation", "capital allocation", "passive"/"investment purposes only"), versioned as `event_gate_version`. An LLM classification of the same text may be stamped as a SEPARATE, explicitly-labelled judgment field with its own version and reliability metric; it never gates.
3. **13D regime split:** any 13D retrospective must separate pre-2024-02 (10-day deadline) from post-2024-02 (5-business-day deadline) and pre-register that ONLY the post-2024 estimate informs the forward stage. The 2024-2026 burn applies to Form-4 features, not to 13D.
4. **Pre-filing run-up as a covariate** for 13D (return from -20 sessions to the filing date), so reversal of the activist's accumulation is not read as post-filing drift.
5. **Retail-book simulation** (3- and 5-name caps) is a required descriptive output of every stage; the equal-weight basket remains the estimand, the simulation shows what a concentrated book would have seen.

## 15. Stage-1 result — insider cluster (2026-09-03)

Estimation stage complete (`insider_cluster_retro_design_2026_09_03.md` §13): treated − matched control car_20 = +0.76 pp, 90% CI [+0.21, +1.31], N = 3,134 / 1,441 arrival sessions; dose-response in the insider count (≥ 3 insiders +1.35 pp). **Planning rule: BUILD (marginal: net lower bound −0.45 pp vs −0.50 pp).** Retail 3-name simulation: mean net +0.5 pp, median −0.5 pp, 54% losing trades. Stage 2 (forward accrual under the live windows and the fee-bearing SIM basket) is the confirmatory test; the owner decides whether to start the build (§7) given the thin net margin.
