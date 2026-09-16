# ML label registry — which outcome each future model trains on

**Status:** DRAFT — five owner decisions open (§9). Nothing here changes a running registration (§7).
**Date:** 2026-09-16
**Scope:** the TARGET (label) of every future ML model over the thematic and event lanes. Not the features, not the model class, not `/edge` display.
**Parents:** ADR 0013 (T1-T8 layers, R1/R2), `apps/alphalens-research/scripts/ml/README.md` (house rules 1-12), `edge_hypothesis_budget_2026_07.md` (looks ledger), `docs/superpowers/specs/2026-06-16-fixed-horizon-car-survival-fill-design.md` (the `car_k` origin), PR #996 (market-model beta), `insider_cluster_forward_prereg_2026_09.md` (`car_20_event`).

---

## 1. Question and TL;DR

"Was this a good pick?" has no single answer. `% Book` and the ladder-window `market_excess_return` can have opposite signs for the same candidate, because both mix the pick with the order shape (fill, stop, take-profit path) and with market beta. A model trained on such an outcome learns the ladder as much as the pick — the `tp1_r > 1.5` collider (ADR 0013 R1) is the recorded case.

The project already measures selection on a fill-independent, fixed-horizon outcome (`car_10`), but the definition lives in each script, it differs between the two lanes, and for ML use it has known defects: a window that contains a session closed before the brief existed, a β = 1 SPY benchmark, a horizon shorter than the trade, and a population of briefed names only. This memo proposes a **versioned label registry**: one label per DECISION, computed once, stamped in the store under a version key, and read by every model.

TL;DR of the recommendation:

1. **One label per decision layer**, never exchanged: selection (T1-T3) trains on a fixed-horizon abnormal return over ALL proposals; entry (T5) on censored time-to-fill; exit (T6/T7) on a policy-minus-no-policy delta; sizing on a downside distribution. §5.
2. **Selection label `sel_ar_h`:** anchor at the OPEN of the first session a reader of the brief can trade (`ladder_arrival_session`), and only when the date's candidate set was final before that open; fixed horizon `h`; benchmark = the candidate's own sector ETF times a shrunk 250-session beta; gross of cost; continuous. Sector β = 1 and SPY β = 1 variants stored beside it as sensitivities. §5.1.
3. **Store both scales:** the raw abnormal return AND the same return divided by ex-ante residual volatility × √h. ATR is the only standing signal and ATR spans 2.6× across candidates, so which scale the model trains on decides what "ATR predicts" means. The ATR interpretation test uses weighted regression on the raw label, not the ratio label (§8). §4, §6.
4. **Label every proposal, not only briefed names.** `proposal_shadow` already holds ~13 LLM proposals per date against ~5 briefed episodes per session; the selection estimand needs the rejected names. §5.1.
5. **Missingness is a coded outcome, never a silent drop** (split guard, no close at horizon, benchmark missing, immature). §5.1.
6. **Horizons 5 / 10 / 20 / 40 stored; primary horizon is an owner decision** (§9 D1), recommended 20, locked before any outcome-based read (no decay curve informs it).
7. Registered looks keep their registered outcome (`car_10`, `car_10_ex0`, `car_20_event`). The registry binds registrations filed AFTER it is locked. §7.

## 2. What exists today

| Outcome | Where | Anchor | Window end | Benchmark | Fill-independent | Layer it measures |
|---|---|---|---|---|---|---|
| `forward_return`, `market_excess_return` | `population_ladders` (stamped) | arrival 30-min VWAP (`reference_close`) | `matured_at` = ladder EXIT session | SPY β=1 | yes (exists for NO_FILL) | **selection × exit policy** — the window length is set by the ladder |
| `sector_excess_return` | stamped (#1435) | same | same | own SPDR sector ETF | yes | same as above |
| `car_5/10/20` | computed per script (`diagnostics/fixed_horizon.py::car_for_event`) | close(`session_on_or_after(brief_date)` − 1) | anchor session + k | SPY β=1 | yes | selection (house outcome of every §3 cluster) |
| `car_mm_k` | computed (`car_for_event_market_model`, #996) | same | same | SPY × 60-session OLS beta | yes | selection, beta-adjusted |
| `car_10_ex0` | options pre-reg only | close(arrival) | arrival + 10 | SPY β=1 | yes | selection, day-0 excluded |
| `car_20_event`, `car_40_event` | stamped for event rows (`feedback/event_car.py`, `event-car-v1`) | OPEN(arrival) | arrival + 19 / + 39 | SPY β=1 | yes | selection, event lane |
| `realized_r`, `tiers_filled_count`, NO_FILL | stamped | blended fill | exit | none (R units) | no | T5 + T7 as specified |
| what-if lens R (`breakeven_realized_r_json`, …) | stamped | blended fill | lens exit | none | no | T6 counterfactual |
| time-to-fill (Kaplan-Meier) | computed (`diagnostics/fill_survival.py`) | — | TTL (censored) | — | — | T5 entry |

Two observations drive the rest of the memo:

- **The headline stamped outcomes are not selection labels.** Their window ends at `matured_at`, which is set by the stop / take-profit path. Perplexity's review (§10) and ADR 0013 R1 agree: an exit-dependent window measures pick × exit policy. Keep them, but never as the target of a selection model.
- **The two lanes already use different anchors and horizons** (`car_10` from close(arrival−1), `car_20_event` from open(arrival)). Not a defect for registered looks — the lanes are never pooled. The registry adds one rule for models (open of the first tradable session). For the event lane that session is already its registered arrival, so the rule coincides with `car_20_event`'s anchor; for the thematic lane it replaces the `car_10` anchor for NEW registrations only.

## 3. Principles (each is a rule a label must satisfy)

1. **Label matches the decision.** A label answers the question the model's output is used for. Selection asks "among what we could have proposed, which did better than the cheapest alternative?"; entry asks "did this order fill, and when?"; exit asks "was holding better than exiting at this state?"; sizing asks "how large and how likely is the loss?". A model trained on another layer's label learns that layer's policy (ADR 0013 R1/R2; López de Prado meta-labeling separates side from act/size for the same reason).
2. **Fixed horizon for selection.** Every candidate is scored over the same number of sessions from the same kind of anchor. An endpoint chosen by the price path (stop, take-profit, exit) is a policy outcome (Kaminski & Lo; event-study practice, MacKinlay 1997).
3. **Executable anchor.** The label starts at the first price a reader of the signal could trade. Features computed from session D's close must not share a return window with session D.
4. **The benchmark is the cheapest alternative the decision replaces.** For a thematic pick that is the sector ETF (buy XLK instead of choosing a name inside it), not only SPY.
5. **Scaling uses ex-ante quantities only.** Never divide by volatility measured inside the label window.
6. **Continuous first** (house rule 6). Binary and rank views are derived, never the stored label.
7. **The label population is everything the decision could have chosen** — including names it rejected — or the model learns the old filter.
8. **Missing is an outcome code.** A dropped episode is recorded with a reason; a guard correlated with the outcome is informative missingness.
9. **Versioned and stamped once** (ADR 0013 R3). A definition change bumps the key and starts a new cohort; scripts read the stamped value, they do not recompute it.

## 4. Measured facts (outcome-blind census, VPS stores, read 2026-09-16 ~14:00 UTC)

No outcome column was loaded (`forward_return`, `*_excess_return`, `car_*`, `realized_*` were never read). Horizon-close availability is a computability check only. Scripts: session scratchpad `label_census.py`, `label_census2.py` (to be promoted to `scripts/ml/` with the first registry build).

| Fact | Value | What it implies for the label |
|---|---|---|
| Population | 1081 ladder rows, 995 plannable (989 thematic, 6 insider-cluster); **462 thematic ticker-episodes over 74 arrival sessions**, arrival 2026-05-27 … 2026-09-15 | independent information is ~74 sessions, not 462 rows |
| Episodes per arrival session | p10 **2**, p50 **5**, p90 **11**; **23 of 74** sessions carry ≤ 3 episodes | a within-session rank / Gaussianised label (Numerai-style) is not estimable — rejected, §6 |
| `technical_atr_pct` across episodes | p10 3.14 %, p50 4.80 %, p90 8.17 % (p90/p10 = **2.6×**) | if label dispersion scales with ATR, a raw-return loss is weighted ~7× (2.6²) toward high-ATR names; the scaled twin is required (§5.1) |
| 60-session OLS beta vs SPY (pre-anchor window) | p10 −0.28, p25 0.18, **p50 0.83**, p75 1.86, p90 2.97; **18.9 % negative**; `|β−1| > 0.5` for **69 %** | β = 1 is wrong for most names; the unshrunk estimate is too noisy to use raw (next row) |
| Are the negative betas artefacts? | split guard hit in 1 of 462 pre-windows; ≥ 10 flat sessions in 0.4 %; daily residual sd p10 1.9 %, p50 3.2 %, p90 5.3 % | not splits, not staleness — estimation noise of a 60-session single-name beta at 3 %/day idiosyncratic risk. A market-model label needs shrinkage and a longer window |
| 60-session beta vs own sector ETF | p50 0.66, p10 −0.28, p90 1.74; `|β−1| > 0.5` for 60.5 % | sector-relative does not fix beta by itself |
| **250-session** beta vs own sector ETF (458 episodes, all with 250 usable pairs) | p10 0.16, p25 0.44, **p50 0.89**, p75 1.38, p90 1.68; **4.8 % negative** (19.9 % at 60 sessions); corr with the 60-session estimate 0.83 | the long window removes most of the noise; β = 1 still misstates a p10-p90 range of 0.16-1.68, so the primary label uses the shrunk 250-session beta (§5.1) |
| When was each date's candidate set final? (journal of `alphalens-thematic-build`, 2026-05-25 … 2026-09-16) | **16 of 114** asof dates had their last candidate RECOMPUTE after the open of the ladder arrival session: 13 before the idempotent freeze existed (asof ≤ 2026-06-15), 3 after it (2026-08-02, 08-18, 08-19 — a config change or a degraded first run breaks the freeze) | for those dates a name may not have been visible at the anchor open; the label must know when the set was final (§5.1). The same fact bears on the ladder replay's arrival VWAP — out of scope here, see §8 |
| Open price at the ladder arrival session for `proposal_shadow` rows | LLM arm **99.6 %** of 783, mechanical arm 94.3 % of 801 (elapsed arrivals, distinct date-ticker-source) | the rejected-proposal population can be labelled from the grouped store today |
| Sector ETF resolved | **99.1 %** of episodes; XLK 158, XLI 94, XLF 76, XLV 39, XLY 27, XLE 18 | a sector benchmark is available today; a third of episodes sit in one sector, so the sector shock is a large share of common variance |
| `proposal_shadow` | 1937 rows / 66 dates: LLM 902 (p50 **13 per date**, 809 distinct date-ticker), mechanical 1035; four `mapper_config_version` cohorts (v1 955, v4 790, v2 150, v3 42) | the rejected-proposal population exists; its cohorts must never be pooled (`theme_mapper_mechanical_rule_headtohead_design_2026_07_12.md`) |
| Horizon close missing once the window elapsed | h10 0 / 419, h20 1 / 359, h40 2 / 269 | delisting-type missingness is rare now, but must be coded (Shumway 1997: missing delisting returns bias upward) |

## 5. The registry — one label family per decision

| Layer (ADR 0013) | Decision the model informs | Label | Unit and population | Loss / evaluation | Must never train on |
|---|---|---|---|---|---|
| **T1 SIGNAL / T2 SELECTION** | which proposals become brief candidates | `sel_ar_h` + `sel_zar_h` (§5.1) | every LLM proposal in `proposal_shadow` (briefed or not), one row per ticker-episode, one `mapper_config_version` cohort; stage flags `in_bracket` / `gate_verified` / `briefed` so each registration trains on the population its decision acts on (a model that runs after the market-cap bracket trains on `in_bracket` rows only) | Huber regression on `sel_ar_h`; pinball q10/q50 as auxiliaries; within-session Spearman only on sessions with ≥ 5 names; session-cluster bootstrap | ladder-window excess, `realized_r`, any T5 output |
| **T3 ORDERING** | rank inside the brief | same `sel_ar_h` / `sel_zar_h` | briefed candidates only (ordering acts on the selected set) | top-k minus rest spread per session; Spearman on sessions ≥ 5 | same |
| **T5 ENTRY** | tier depth / TTL / whether a tier fills | `(t_first_touch, touched, tiers_filled)` censored at the frozen `order_ttl_days`; policy cancellation as a competing event | every plannable setup, one row per tier (continuation-ratio stacking, as in `2026_07_ladder_fill_depth_cr.py`) | Cox / discrete-time hazard, Brier at TTL, calibration | `sel_ar_h` as a target (a fill is not a return) |
| **T5 entry adverse selection** | whether a filled tier is a falling knife | `post_fill_ar_5` = abnormal return from fill to fill + 5 sessions (sector ETF, β=1) | filled tiers | descriptive / Huber | — |
| **T6 IN-FLIGHT / T7 EXIT** | a stop / take-profit / trail policy | `policy_delta_h` = lens abnormal return minus the NO-POLICY abnormal return over the same fixed window from the same filled entry; after a lens exits before `h` its leg holds the benchmark, so its abnormal return is 0 for the remaining sessions (Kaminski-Lo stopping premium); sessions in-market stored beside the delta | filled episodes, one lens per row, lens registry cap applies (R4) | paired session-cluster bootstrap of the delta; winners-harmed count first-class | raw `realized_r` alone (it cannot separate the pick from the policy) |
| **Sizing** (outside ADR 0013 today) | how much risk per name | `sel_ar_h` quantiles q10 / q50, `mae_h` (max adverse excursion from anchor, sector-relative), P(disaster stop within h) | same population as selection | pinball loss, calibration of the lower tail | a win flag; historical `suggested_size_pct` (it is a policy, not an outcome) |
| **Meta-label** (future "take this trade?") | act / skip a briefed name WITH its setup | triple-barrier outcome with the ACTUAL disaster stop, first take-profit and time stop | briefed plannable setups | multiclass log loss + policy P&L | used only here — never as a selection label |

### 5.1 The selection label in detail

**Anchor.** `t0 = OPEN(ladder_arrival_session(brief_date))` for the thematic lane — the first session a reader of the brief can trade (`feedback/ladder_config.py`, #1416). For the event lane the lane's registered rule (`session_on_or_after(brief_date)`) already makes that session the arrival; keep it. Benchmark legs start at the same open.

**The candidate set must be final before `t0`.** Briefs for date D are rebuilt at six slots on D+1, the last one after the US close. Since 2026-06-15 the mapper freezes the candidate set at the first deciding slot (≈ 01:30 UTC) and later slots only rewrite prose — but a config change or a degraded first run breaks the freeze, and before the freeze existed every slot recomputed. Measured from the build journal: 16 of 114 dates had their last candidate recompute AFTER the open of `t0` (§4). For those dates a name may not have been visible at that open. Rule: the stamper reads the time of the date's last candidate recompute (going forward a `candidate_set_final_at` stamp on the candidates parquet; historically the journal read in §4) and labels a date only when that time is before `O[t0]`; otherwise the status is `set_final_after_open`. No per-name first-appearance time exists in the stores, so the rule works per date, not per name.

Why not the current `car_10` anchor: `car_10` starts at close(`session_on_or_after(brief_date)` − 1). For a weekday brief dated D that window CONTAINS session D, which closed before the brief existed and from whose close the features were computed. The July adversarial review measured that session at ~14 % of `car_10` variance. That is not tradable by the group and it couples label to features mechanically. The options pre-registration already moved to `car_10_ex0` for this reason; the registry makes it the rule. Keep `pre_ar = close(D)/close(D−1)` versus benchmark as a separate diagnostic column (the pre-publication reaction), never inside the label.

Why OPEN and not the 30-min VWAP (`reference_close`): the open is in the grouped-daily store for every ticker including the rejected proposals, so the whole population can be labelled from one source; the VWAP needs minute bars that exist only for briefed names. The VWAP stays the anchor of T5 execution labels.

**Horizons.** Stored: 5, 10, 20, 40 sessions (`h` inclusive of the arrival session), plus the non-overlapping increments 1-10, 11-20, 21-40 so decay can be read without re-counting overlapping windows. One PRIMARY horizon per registration (budget rule 6). Recommended primary: **20** (§9 D1).

- Hold today: p50 27 sessions, p95 42 (`/edge`, 2026-09-16); time stop 42 sessions; entry TTL 7 sessions. A 10-session label scores a pick on a third of the trade it feeds.
- The event lane registered `car_20_event` / `car_40_event`; the same pair for the thematic lane gives the project one convention.
- 20 matures twice as fast as 40 and overlaps half as much; at h40 only 269 of 462 episodes have elapsed today.
- Perplexity recommended 30 (centre of a 20-40 hold). Rejected as primary: it is a new, unused horizon.
- **The primary horizon is locked BEFORE any outcome-based read.** Choosing it from a decay curve computed on outcomes would be a forking path (review §11). The decay curve is descriptive only, after the lock. The input that may inform D1 is outcome-blind: a synthetic power simulation at 74 arrival clusters with purged, embargoed folds (embargo = horizon), comparing h10 / h20 / h40.

**Benchmark.** Primary: own sector ETF (`sector_etf` resolution already stamped, 99.1 %) times the stock's SHRUNK 250-session beta to that ETF: OLS over the 250 sessions ending at close(`t0` − 1), shrunk toward 1 (Vasicek / Blume), with a fallback to 1 and a coded `beta_source` when the window is thin or degenerate (the `estimate_beta` contract, #996). Measured: all 458 resolved episodes have 250 usable pairs; the 250-session beta has p50 0.89, p10-p90 0.16-1.68 and 4.8 % negative, against 19.9 % negative at 60 sessions (§4). With β = 1 a low-beta name in a rising sector gets a negative label for its exposure alone, so the model would learn a beta tilt. Stored sensitivities:

- `sel_ar_h_sec1` — sector ETF, β = 1 (the simplest cheapest-alternative reading);
- `sel_ar_h_spy` — SPY, β = 1 (continuity with every past `car_k`).

Dimson sum-beta for thin trading is a later refinement, not part of v1.

Why sector first: briefs are theme-clustered (a third of episodes in XLK), so SPY-relative labels carry the theme's common move; the within-theme selection question (#1268 already pre-registered `sector_excess_return` as its secondary for exactly this) needs it removed. Characteristic-matched controls (DGTW: size, book-to-market, momentum) are the literature's stronger benchmark but need point-in-time shares outstanding and book values for the whole market, which the stores do not carry — deferred, not rejected.

**Formula.**

```
sel_ar_h   = (C_stock[t0+h-1] / O_stock[t0] - 1) - beta_shrunk * (C_etf[t0+h-1] / O_etf[t0] - 1)
sel_zar_h  = sel_ar_h / (sigma_resid_pre * sqrt(h))
```

`sigma_resid_pre` = daily residual sd of the stock against the same benchmark over the 60 sessions ending at close(t0 − 1). It is deliberately NOT `technical_atr_pct`: dividing the label by a feature would make the label a function of that feature. The two are strongly related. Caution for any analysis on `sel_zar_h`: when the mean of `sel_ar_h` is not zero, dividing by a volatility that tracks ATR creates a correlation between ATR and `sel_zar_h` with no predictive content (a positive mean shrinks faster for volatile names). So `sel_zar_h` is a training target for a scale-free ranking model, and the ATR interpretation test does NOT use it (§8 step 3).

**Gross, not net of cost.** Perplexity recommended net-of-cost labels. Rejected for selection: cost depends on order type, size and broker (T5 / execution), differs per group member, and does not belong to the pick. Costs enter the entry and sizing labels.

**Population.** All LLM proposals in `proposal_shadow` for the live cohort, plus briefed rows, deduplicated to ticker-episodes (house rule 1), with stage flags `in_bracket`, `gate_verified`, `briefed`. Measured: the open at the arrival session exists for 99.6 % of elapsed LLM proposals and 94.3 % of mechanical ones (§4). The estimand is "which of the names this pipeline proposed did better", not selection skill over the whole market. The mechanical arm is labelled too (it is the head-to-head's control). A model of T2 fitted on briefed names only estimates "which briefed names did better", which is T3's question.

**Missingness codes** (stamped in `sel_label_status`, one per horizon): `ok`, `immature`, `split_guard` (any consecutive-close ratio outside [0.55, 1.8] inside the window), `no_close_at_horizon` (ticker absent — treat as a delisting candidate, never forward-fill), `benchmark_missing`, `sector_unresolved`, `no_open`, `set_final_after_open`. Every model report prints the code counts by ATR tercile, so a guard correlated with volatility is visible.

**Version key.** `SEL_LABEL_VERSION` over anchor rule, horizons, benchmark rule, guard bounds, shrinkage rule and residual window — the `EVENT_CAR_VERSION` pattern.

## 6. Adjudication of the literature review (what was taken, changed, rejected)

| Recommendation (Perplexity, 2026-09-16) | Decision | Reason |
|---|---|---|
| Fixed horizon for selection, path-dependent labels only for exit | **taken** | same as ADR 0013 R1 |
| Anchor at first executable price | **taken** (open of ladder arrival) | §5.1 |
| Primary horizon 30 sessions | **changed → 20** (owner decision D1) | lane consistency, maturation; locked before any outcome read, informed only by a synthetic power simulation |
| Matched characteristic controls (DGTW) as primary benchmark | **deferred** | no PIT shares outstanding / book value in the stores |
| Sector-adjusted benchmark | **taken as primary, with a shrunk beta** | theme clustering, 99.1 % coverage, #1268 precedent |
| Market-model beta | **taken, against the sector ETF, 250 sessions, shrunk** | §4: 60-session beta is noise-dominated (19.9 % negative), 250-session is not (4.8 %) |
| Net-of-cost selection label | **rejected** | cost is an execution property, member-specific |
| Store raw and vol-scaled labels, ex-ante scaling | **taken** | ATR spread 2.6× |
| Rank / Gaussianised labels | **rejected as stored label** | p50 5 names per session, 23 / 74 sessions ≤ 3 |
| Huber + quantile losses, no winsorising of real outcomes | **taken** | heavy tails (p10 −21 %, p90 +25 % on `/edge`) |
| Label every proposed candidate + matched non-candidate controls | **proposals taken; external controls deferred** | proposals exist; controls need the same PIT characteristics as DGTW |
| Purged + embargoed chronological CV, 1/n per session weights | **taken** — embargo = primary horizon | house rule 9 deferred purging until "> 6 months of history"; history is ~3.7 months, so at h20 purging costs ~20 sessions of 74 — say so in each registration |
| Longstaff-Schwartz continuation values for exit | **rejected for now** | T6 is NOT_ENOUGH data (46 filled episodes in July); the lens delta is the tractable form |
| Trend-scanning labels | **rejected** | picks the horizon from the outcome |

## 7. What this does NOT change

- **Registered looks keep their registered outcome.** Cluster families on `car_10`, the options first look on `car_10_ex0`, the insider-cluster lane on `car_20_event`, the experts last look on `car_10`. Switching a registered outcome after registration is a forking path.
- **No live selection, ordering or display change.** R1/R2 and the sort-lock tests stand.
- **`/edge` keeps its current headline.** Display is out of scope.
- **Looks accounting is unchanged.** Defining and stamping a label is outcome-blind; every model run against it is a look (house rule 11).

## 8. Build path (proposed, not filed)

0. **Lock D1-D5 before anything reads an outcome.** Record the decisions and the `SEL_LABEL_VERSION` freeze timestamp in the looks ledger (§4 row, no charge). A later change is a new version and a new registration.
1. **Stamp the selection label** — pipeline side, `feedback/selection_label.py` beside `event_car.py`, same shape (newest-first, disk-first from the grouped cache, atomic rewrite, deadline, never raises), for briefed rows AND a new `proposal_shadow` label parquet. Parquet-only; nothing on the wire (no interim peeks). TDD with hand-computed windows, including a weekday brief whose day-0 session must be excluded and a date whose candidate set was recomputed after the open. The mapper starts stamping `candidate_set_final_at` on the candidates parquet in the same change.
2. **Promote the census** (`label_census*.py`) to `scripts/ml/2026_09_label_census.py` — outcome-blind, prints the §4 table and the missingness code counts by ATR tercile.
3. **One registered look: the ATR interpretation test.** Held-out panel, one charge to cluster 1. Three pre-registered regressions of `sel_ar_20` on ATR with session-clustered errors: (a) OLS (does ATR move the mean?), (b) WLS with weights 1 / `sigma_resid_pre`² (does it move the mean per unit of risk? — the scale question without the ratio artefact of §5.1), (c) quantile regression at q10 (does it move the downside?), plus P(disaster stop within 20) on ATR. It answers whether ATR predicts return, scale, or downside — the one fact every later model depends on.
4. **Separate issue (not this registry): the ladder replay has the same timing gap.** For the 16 dates in §4 the arrival 30-min VWAP (`reference_close`) may precede the final candidate set, so those `/edge` rows may carry an entry the group could not have taken. Worth its own issue with the dates attached.
5. **README house rules update:** rule 4 names `sel_ar_h` / `sel_zar_h`; rule 9 gets the embargo = horizon statement; a new rule: "a model reads the stamped label; recomputing a label in a script is a new definition and needs its own version".

## 9. Open owner decisions

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | Primary selection horizon | 10 (continuity) / **20** / 30 / 40 | 20, with 40 secondary — matches the event lane, half the overlap of 40; confirm with the synthetic power simulation before locking, never with outcomes |
| D2 | Primary benchmark | SPY β=1 / sector ETF β=1 / **sector ETF × shrunk 250-session beta** | sector ETF with shrunk beta; the other two stored |
| D3 | Label the rejected proposals | yes / no | yes — without them T2 cannot be modelled |
| D4 | Anchor | close(arrival − 1) / close(arrival) / **open(ladder arrival)** / arrival VWAP | open of ladder arrival |
| D5 | ATR interpretation test before any new model | yes / no | yes — it is one look and it decides how every later coefficient is read |

## 10. Sources

- Literature review via Perplexity (2026-09-16, two passes): signal-vs-execution separation and label construction for small event-driven equity panels.
- Quantopian alphalens — fixed-period forward returns and IC: https://quantopian.github.io/alphalens/alphalens.html
- MacKinlay (1997), *Event Studies in Economics and Finance*: https://www.bu.edu/econ/files/2011/01/MacKinlay-1996-Event-Studies-in-Economics-and-Finance.pdf
- Daniel, Grinblatt, Titman, Wermers (1997), characteristic-matched benchmarks: https://onlinelibrary.wiley.com/doi/full/10.1111/j.1540-6261.1997.tb02724.x
- Barber & Lyon (1997) and Lyon, Barber & Tsai (1999), long-horizon BHAR biases: https://ideas.repec.org/a/eee/jfinec/v43y1997i3p341-372.html
- Kaminski & Lo, *When do stop-loss rules stop losses?*: https://dspace.mit.edu/handle/1721.1/114876
- Perold (1988), *The Implementation Shortfall*: https://www.hbs.edu/faculty/Pages/item.aspx?num=2083
- López de Prado, *Advances in Financial Machine Learning* ch. 3-4, 7 (triple barrier, meta-labeling, sample uniqueness, purged CV): https://www.oreilly.com/library/view/advances-in-financial/9781119482086/c04.xhtml
- Gu, Kelly, Xiu (2020), *Empirical Asset Pricing via Machine Learning* (excess-return target, Huber loss, R²_oos): https://academic.oup.com/rfs/article/33/5/2223/5758276
- Moskowitz, Ooi, Pedersen (2012), ex-ante volatility scaling: https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf
- Lo, MacKinlay, Zhang (2002), survival models of limit-order execution time: https://web.mit.edu/Alo/www/Papers/JFE2002_Pub.pdf
- Shumway (1997), delisting bias: https://www.tylergshumway.org/Shumway-DelistingBiasCRSP-1997.pdf
- Altman & Royston (2006), cost of dichotomising: https://www.bmj.com/content/332/7549/1080.1
- Harvey, Liu, Zhu (2016), multiple testing in factor research: https://academic.oup.com/rfs/article-abstract/29/1/5/1843824

## 11. Review trail

Adversarial review, `deepseek/deepseek-v4-pro` (thinking high), 2026-09-16, on the first draft. Eight findings; each adjudicated by running it, observation and remedy judged separately.

| # | Finding | Observation | Remedy | What changed |
|---|---|---|---|---|
| 1 | Anchor open may precede the brief (six rebuild slots) | **PARTLY CONFIRMED.** The claim that later slots can change the set is refuted since the freeze (journal: "mapper reused a frozen candidate set"), but the freeze breaks: 16 of 114 dates had their last recompute after the open (13 pre-freeze, 3 after) | per-name first-appearance time — **not available** in the stores; per-date rule adopted instead | §4 row, §5.1 "candidate set must be final", status code, §8 steps 1 and 4 |
| 2 | Sector β = 1 rewards beta, not selection | **CONFIRMED** (sector beta p10-p90 0.16-1.68 at 250 sessions) | shrunk market-model against the sector ETF as primary — **taken**, after measuring that 250 sessions removes the noise (4.8 % negative vs 19.9 %) | §5.1 benchmark, formula, §6, D2 |
| 3 | Vol-scaled label makes the ATR test mechanical | **PARTLY CONFIRMED.** A zero-mean label divided by a positive volatility carries no correlation with ATR in expectation; the artefact exists only through a non-zero mean. It is real, because the mean is not zero | "use a gapped window for σ" — **rejected** (the coupling is through the mean, a gap does not remove it); WLS on the raw label — **taken** | §5.1 caution, §8 step 3 |
| 4 | All proposals include names removed by hard filters | **CONFIRMED** | train only on post-filter rows — **changed** to stage flags, each registration names its population | §5 table, §5.1 population |
| 5 | Horizon 20 chosen without a power argument | **CONFIRMED** | outcome-blind synthetic power simulation before D1 — **taken** | §5.1 horizons, D1 |
| 6 | `policy_delta_h` undefined after an early exit | **CONFIRMED** | post-exit leg holds the benchmark (abnormal return 0) — **taken**; truncating both legs at the exit — **rejected**, it discards what the policy forgoes after exit | §5 table |
| 7 | Primary horizon chosen from a decay curve is a forking path | **CONFIRMED** (the first draft said so literally) | lock D1-D5 before any outcome read, ledger row — **taken** | §5.1, §8 step 0 |
| 8 | Inconsistencies: "no third convention", proposal coverage unmeasured | **CONFIRMED** both | reword; measure coverage — **done** (99.6 % / 94.3 %) | §2, §4 row |
