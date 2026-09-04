# Insider purchase clusters — forward pre-registration (stage 2, the single confirmatory test)

**Status:** LOCKED 2026-09-04 (this document is the executable spec; frozen constants in `docs/research/preregistration/params_insider_cluster_forward_2026_09.json`, ledger id `insider_cluster_forward_2026_09`). Results are appended as a separate section by the look commit, which must not touch §1-§9.
**Parent:** [`event_sourced_lane_design_2026_09_03.md`](event_sourced_lane_design_2026_09_03.md) §6 (two-stage design), §14 (review amendments). **Stage 1:** [`insider_cluster_retro_design_2026_09_03.md`](insider_cluster_retro_design_2026_09_03.md) §13 (+0.76 pp, 90% CI [+0.21, +1.31], planning rule BUILD, marginal).
**Epic:** #1293 (sub-issues #1294 this document, #1295 detection command, #1296 day integration, #1297 outcomes and cohort separation, #1298 SPA chip, #1299 SIM basket — gated).
**Class:** informed-trader flow, Form-4 based. Shares data with paradigm #11 (Cohen-Malloy opportunistic) and with stage 1, so this is NOT a fresh class for multiplicity: programme-level Bonferroni applies. Family for this lane = 2 event classes (insider cluster now; Schedule 13D reserved, its own registration later) x one primary test each.
**Owner decision 2026-09-04:** build as a candidate SOURCE with logging (rows appear on the daily brief beside the thematic ones, outcomes accrue under their own cohort), not as an automated strategy. Nothing is traded by the lane; the SIM basket (#1299) is a separate, gated decision.

## 1. Hypothesis (one primary, one pre-registered secondary)

**H1 (primary):** the equal-weight basket of ALL eligible insider-cluster episodes surfaced by the live lane earns a positive market-adjusted 20-session return from the first obtainable price after the cluster becomes public (`car_20_event` > 0).

**H1b (secondary, dose-response, pre-registered here so it can never be swapped in post hoc):** the subset with `>= 3` distinct insiders has a larger mean `car_20_event` than the `2`-insider subset. Descriptive unless H1 clears; never a substitute for H1.

Measured prior (stage 1, survivor-reconstructed): +0.76 pp gross per 20 sessions; `>= 3` insiders +1.35 pp; 2 insiders +0.31 pp. Smallest actionable effect: **+0.5 pp gross** (roughly break-even against the Saxo LIVE fee model on a fixed ticket). Fee model: 0.66 pp round trip (0.08% per side + 0.25% FX each way, `reference_saxo_fees_sim_vs_live_2026_07_29`).

## 2. Population (frozen)

Unit = ticker-episode (one event per ticker per 20 sessions, first wins). An episode enters the population when it is written by the lane with `eligible = True` on a brief date on or after the accrual start (§7). Two kinds of rows count:

- `source = insider_cluster` — the cluster surfaced a name the thematic pipeline did not select that day;
- `event_overlap = True` — a thematic card for the same ticker existed that day; the card keeps the thematic catalyst, the episode is counted in BOTH cohorts and the overlap share is reported.

Rows with `eligible = False` (`exclusion_reason` set) form the **shadow arm**: never on the brief, always in the parquet, reported descriptively as the value of the exclusions.

## 3. Event definition (frozen; identical to stage 1 where stage 1 measured it)

From `~/.alphalens/form4_parquet/` (VPS source of truth, daily incremental at 02:30 UTC):

- **Leg:** `transaction_code == "P"`, `acquired_disposed == "A"`, `is_amendment == False`, `is_officer or is_director` (10% owners excluded unless also officer/director), `transaction_price_per_share` present (no imputation), `transaction_shares x price >= 10,000 USD`.
- **Cluster:** `>= 2` distinct `reporting_owner_cik` with qualifying legs whose `filed_date`s fall within 2 trading sessions of each other, inclusive: the completing leg's `filed_date <= advance_trading_sessions(first leg filed_date, 2)` (distance 0, 1 or 2 sessions). The PIT clock is `filed_date`, never `transaction_date`. Officer/director status = the reporter flags ON the filing (not a later status). Cluster USD = sum of qualifying legs, **floor 100,000 USD** (250,000 reported as a strength check, not a second test).
- **Event date** = `filed_date` of the leg that completes the cluster (the 2nd distinct insider; ties on the same `filed_date` are ordered by `accession_number`, the completing leg is the second distinct CIK in that order). Later amendments never retract or move an event.
- **Episode dedup:** one event per ticker per 20 sessions, first wins (as measured in stage 1; supersedes the "same ticker within 5 sessions" line in the parent memo §5, which the 20-session rule subsumes).
- **Late filings:** completing leg filed more than 10 business days after its `transaction_date` -> excluded (`late_filing`).

## 4. Arrival, anchor and brief-date mapping (frozen)

- **Acceptance time** = EDGAR `<ACCEPTANCE-DATETIME>` of the completing filing (canonical SEC client, cache `~/.alphalens/edgar_acceptance/`). Unknown acceptance is treated as after-hours (conservative).
- **Arrival session** = the filing date's session when accepted before 09:00 ET, otherwise the next session.
- **Brief date** of the event row: `D = F` when accepted before 09:00 ET on filing date `F`, else `D = F + 1 calendar day`. By construction `session_on_or_after(D)` equals the arrival session, so the population monitor's ladder anchor and the event anchor coincide (a Friday after-close filing lands on the Saturday brief and arrives Monday; it is never duplicated on the Sunday or Monday brief).
- **Anchor price (`car_*_event`)** = the arrival session OPEN — the first PUBLIC-INFORMATION price after acceptance (the filing is public before that open; the Form-4 store refresh at 02:30 UTC = 22:30 ET also precedes it). This is the stage-1 estimand and the ladder anchor, so the two lanes stay structurally identical. It is obtainable by a real-time EDGAR follower, NOT by a reader of our brief: under the T-1 dating the brief carrying the row is first built after the arrival session (the thematic lane has the same property — its ingest window is the whole UTC day D against a D-open anchor).
- **Reader anchor (`car_*_reader`, pre-registered secondary)** = the OPEN of `session_on_or_after(D + 1 calendar day)`: the first session after the brief that carries the row exists (its 04:30 UTC refresh, after the store update, precedes that open; every slot up to 12:30 UTC does). This is what a reader of our brief could obtain; it is one session later than the public-information anchor and therefore expected to show less drift.
- **Store-lag failure mode:** brief D is (re)built only during calendar day D+1; if the SEC daily index lags by more than one day the filing reaches the store after the last slot for D and the event is never surfaced. Counted (store `filed_date` vs first-seen) and reported as a data-source failure rate; not an exclusion, the row simply does not exist.

## 5. Hard exclusions at detection (frozen; the only gate, fact-based, logged in both arms)

Applied in this order; the first hit is the stamped `exclusion_reason`:

1. `late_filing` — §3.
2. `mcap_unknown` — no market cap obtainable at detection (conservative exclusion; stamped so the shadow arm shows how often the data source fails).
3. `mcap_out_of_bracket` — market cap outside **500M-10B USD** at detection (the thematic bracket; a test pins equality with `DEFAULT_MCAP_RANGE`).
4. `sic_excluded` — SIC in {6770 blank checks / SPACs, 6722 open-end management investment offices, 6726 unit investment trusts and closed-end funds}. Unknown SIC is NOT excluded. ADRs are structurally absent from the source (foreign private issuers are exempt from Section 16, so they file no Form 4) — stated, not filtered.
5. `earnings_window` — next confirmed earnings date on or before arrival + 9 sessions (i.e. inside the first 10 sessions of the hold). Unknown earnings date is NOT excluded; the value the gate saw is stamped (`event_next_earnings_date`).

All exclusion inputs (market cap, SIC, next earnings date, acceptance time) are AS OBSERVED AT DETECTION and stamped on the row; they are never recomputed or revised later, so a vendor restatement cannot move an episode between arms. Dedup (§3) runs on ALL detected clusters before the exclusions, so an excluded first event still suppresses a later cluster of the same ticker inside its 20 sessions (both are visible in the shadow arm).

Anti-patterns pinned: no LLM judgment of conviction, credibility or direction; no threshold edited after outcomes exist; the thematic press gate is never applied to event rows. Any change to §3-§5 bumps `event_gate_version` and opens a NEW cohort; cohorts are never pooled across versions.

## 6. Outcomes (frozen)

- **Primary:** `car_20_event = (close[arrival + 19] / open[arrival] - 1) - the same for SPY` (beta = 1). Prices: UNADJUSTED daily open/close from the population monitor's grouped-daily cache (Polygon, `adjusted=false`; SPY from the same rows); dividends ignored; split guard: every consecutive-close ratio inside the window within [0.55, 1.8], otherwise the episode is null for that horizon. Stamped `event_car_version`.
- **Secondary (pre-registered, reported with their own CIs):** `car_20_reader` (reader anchor, §4), `car_40_event` (arrival + 39). Ladder metrics (`realized_r`, `market_excess_return`, fills inside the 7-session TTL) are descriptive: they measure OUR execution mechanics, not the drift.
- **Net variant:** `car_20_event - 0.0066` and `car_20_reader - 0.0066` (fee model: one round trip per episode, fixed ticket, no liquidity scaling — a transformation of the gross figure, not a trading simulation), reported beside the gross figures.
- **Denominator:** complete-case — eligible episodes with a non-null `car_20_event` (null = split guard, halt, delisting or missing open/close; no delisting-return source exists). The null share and the reasons are reported; a null share above 10% is flagged in the verdict text. The same rule applies to `car_20_reader` separately.
- Never pooled with the thematic `market_excess_return`, and the thematic aggregates on `/edge` exclude `source != thematic` from PR #1297 on.

## 7. Accrual, floor and the single look (frozen)

- **Accrual start** = the first VPS brief date produced with `ALPHALENS_EVENT_LANE=1`, recorded in the ledger entry `outcome.accrual_start` when the flag is flipped (after #1297 is deployed, never before). Rows before that date do not exist.
- **Floor:** the look is computed once, when **N >= 150 eligible episodes have a matured `car_20_event`** (arrival + 19 sessions closed) **AND those episodes span >= 50 distinct arrival sessions** (the bootstrap's effective sample is the cluster count). This is the unblocking CONDITION on issue #1294; no wake date is estimated. Stage-1 rates (2013-2023, store-wide ~40 events/month before the bracket and earnings exclusions) suggest 6-12 months, but the floor is the rule, not the calendar.
- **No interim peeks.** No dashboard, no partial mean, no median before the floor. The `/edge` source chip shows the row COUNT only (the sanctioned accrual indicator).
- **Power (honest):** sd(car_20) ~ 0.20 -> at N = 150 the standard error of the basket mean is ~1.6 pp under independence, larger after clustering; the look detects ~4 pp at 80% one-sided power, and "cleared" needs an observed mean of roughly +3.2 pp. Against the measured +0.76 pp prior the expected outcome of the first look is "inconclusive"; the look exists to check the lane under REAL frictions (acceptance-time detection, daily store lag, 7-session TTL, bracket and earnings exclusions) and to close the retrospective's survivor bias, not to manufacture a p-value. Stage 1 is a DESIGN INPUT (it fixed the constants and the floor before any forward row exists), not evidence in this test.
- **Battery script:** committed to `apps/alphalens-research/scripts/ml/` at least 30 sessions BEFORE the look, as a registration amendment PR that adds (never edits) params; the script prints the three-way verdict from the frozen constants (`n_boot = 9999`, `seed = 0`).

## 8. Inference and verdict language (frozen)

- Statistic: mean over episodes of `car_20_event`; clusters = arrival sessions. **Bootstrap (frozen algorithm):** resample arrival-session clusters with replacement, B = 9,999, `numpy.random.default_rng(0)`; recompute the episode-weighted mean each draw. CI = percentile (2.5th / 97.5th for the 95% CI used below). One-sided p for H1 = `(1 + #{b : mean_b - mean_obs >= mean_obs}) / (B + 1)` (bootstrap distribution re-centred at zero). CR2 t reported alongside; ticker-cluster and two-way (ticker x arrival session) CIs as sensitivity.
- Family bar: **one-sided alpha 0.025** (2 event classes in the lane family). Only H1 is confirmatory; H1b, `car_20_reader`, `car_40_event` and every §9 output are secondary/descriptive and carry no charge.
- **Single look.** There is no second look: the confirmatory test happens exactly once at the floor. Any later test of this lane is a NEW registration with its own programme charge (this replaces the earlier "one re-look at N >= 300" wording; a pre-specified second look without alpha spending would not hold the family bar).
- Three-way conclusion, evaluated in this order (inequalities strict as written):
  1. **Cleared** — bootstrap one-sided p < 0.025 for `mean(car_20_event) > 0` AND `mean(car_20_event) - 0.0066 > 0` AND `mean(car_20_reader) - 0.0066 > 0` (a cleared verdict may not rest on a price our reader cannot obtain). Consequence: lane stays ON; the SIM-basket decision (#1299) is opened; promotion into thematic SELECTION or ORDERING remains a separate registration (the lane stays a source, never a filter).
  2. **Evidence against an actionable effect** — 95% percentile-CI upper bound of `mean(car_20_event)` < +0.5 pp (the smallest actionable effect; the 95% bound matches the one-sided 0.025 family alpha). Consequence: flag OFF, lane retired, cohort frozen, postmortem row in `paradigm_failures_postmortem.md`.
  3. **Inconclusive** — everything else. Consequence: lane stays ON as display-only telemetry with no further test; operational retirement (flag OFF) is an owner decision recorded in the ledger, not a statistical one.
- H1b is reported with its own CI in every case; it changes no consequence above.
- Verification battery for a cleared result (all must pass): exact reproduce from the frozen parquets; leave-one-arrival-month-out worst case p < 0.05; ticker-collapsed (first episode per ticker) sign retained and >= 50% magnitude; `event_car_version` and `event_gate_version` single-valued in the sample.

## 9. Required descriptive outputs (frozen list; none is a test)

Shadow arm (excluded rows by reason, same arrival and outcome construction, with their `car_20_event` where computable; can never affect the verdict); overlap share (episode- and ticker-level) and the overlap subset's mean — the primary analysis is ALL eligible episodes, the two cohort labels overlap by design and are not additive; `>= 3` vs 2 insiders (H1b); USD floor 250k cut; `car_20_reader`; ladder fill rate inside the 7-session TTL and `realized_r` of filled episodes; retail-book simulation with 3- and 5-name caps (equal-dollar, fee model, first-come); `car_40_event`; per-month accrual counts; null-outcome share by reason; data-source failure rates (`mcap_unknown`, unknown acceptance, unknown earnings, store-lag drops).

## 10. Abort and deviation rules

Abort uncharged only for outcome-blind defects discovered before the floor (detection bug, join integrity, store gaps) — such a defect bumps `event_gate_version` and restarts the cohort. The moment any `car_20_event` aggregate is computed the look is spent. Deviations are logged in the results section with the reason; a deviation that changes §3-§8 is a new registration.

## 11. Cohort keys and where they live

`source` (thematic | insider_cluster), `event_overlap`, `event_gate_version` (`insider_cluster_gate_v1`), `event_car_version` (`event-car-v1`), plus the existing `scorer_config_version` and `ladder_config_version`. Stamped on the brief parquet by the lane, carried into `~/.alphalens/population_ladders/` by the monitor (#1297), mirrored into Postgres `edge_ladderoutcome` (`source`, `event_overlap` only; the CAR columns stay parquet-only until the look).

## 12. Adversarial review (2026-09-04, before lock) — adjudication

Round: zen `deepseek/deepseek-v4-pro` thinking=high (4 findings) + Perplexity `reason` (25 findings). Each finding was adjudicated separately from its remedy.

**Adopted (folded into §3-§9):** (1) both reviewers — the arrival-open anchor is not obtainable by a reader of our brief under the T-1 dating: the observation is correct, the remedy "move the primary anchor to the brief-availability session" is NOT adopted (it would break comparability with stage 1 and with the ladder anchor, and the thematic lane carries the same property); instead `car_20_reader` is pre-registered as a secondary and "cleared" now requires the reader-anchored net mean > 0 as well (§4, §6, §8). (2) A pre-specified second look at N >= 300 without alpha spending inflates the family error — the re-look is removed; single look (§8). (3) Bootstrap under-specified (CI type, null centring, p formula) — frozen algorithm written out (§8). (4) The bootstrap's effective sample is the cluster count — floor gains ">= 50 arrival sessions" (§7). (5) "Evidence against" used a 90% bound against a one-sided 0.025 alpha — now the 95% percentile bound (§8). (6) Missing-outcome denominator unspecified — complete-case with the null share reported and a 10% flag (§6). (7) Exclusion inputs could be revised later — stamped as observed at detection, never recomputed; dedup position fixed before the exclusions (§5). (8) Cluster-window endpoints, tie-breaking, officer status date, amendments — made explicit (§3). (9) Price adjustment and dividends unspecified — unadjusted grouped-daily with the split guard, dividends ignored (§6). (10) Fee arithmetic — one round trip per episode, fixed ticket, a transformation not a simulation (§6). (11) Overlap cohorts non-additive — primary = all eligible episodes; overlap descriptive (§9). (12) Store-lag failure mode named and counted (§4). (13) Power statement now names the ~+3.2 pp observed mean "cleared" would need and labels stage 1 a design input (§7).

**Rejected (with reason):** alpha-spending / group-sequential boundaries — moot once the second look is removed; ITT denominator with delisting returns — no delisting-return source exists in this project, disclosed instead; corporate-action-adjusted price series — the monitor's source of truth is unadjusted Polygon grouped-daily, the split guard is the pre-registered protection; re-running stage 1 with the forward filters — stage 1 is a design input, not a comparator, and re-running it would be a further look on burnt data; calendar-time / issuer-dependence bootstrap as the primary — two-way clustering is already the pre-registered sensitivity; a `detection_timestamp`-based anchor — the store refresh (22:30 ET) precedes the arrival open, so the public-information anchor holds; "distinct insider identity key" — `reporting_owner_cik` is the SEC's stable key and was already the definition.
