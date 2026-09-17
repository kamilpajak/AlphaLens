# ML label registry — which outcome each future model trains on

**Status:** LOCKED 2026-09-17 — owner decisions D1-D5 taken (§9); evidence and review record in §12. Nothing here changes a running registration (§7), with one planned amendment before a registration is filed (#1227, §8 step 3).
**Date:** 2026-09-16
**Scope:** the TARGET (label) of every future ML model over the thematic and event lanes. Not the features, not the model class, not `/edge` display.
**Parents:** ADR 0013 (T1-T8 layers, R1/R2), `apps/alphalens-research/scripts/ml/README.md` (house rules 1-12), `edge_hypothesis_budget_2026_07.md` (looks ledger), `docs/superpowers/specs/2026-06-16-fixed-horizon-car-survival-fill-design.md` (the `car_k` origin), PR #996 (market-model beta), `insider_cluster_forward_prereg_2026_09.md` (`car_20_event`).

---

## 1. Question and TL;DR

"Was this a good pick?" has no single answer. `% Book` and the ladder-window `market_excess_return` can have opposite signs for the same candidate, because both mix the pick with the order shape (fill, stop, take-profit path) and with market beta. A model trained on such an outcome learns the ladder as much as the pick — the `tp1_r > 1.5` collider (ADR 0013 R1) is the recorded case.

The project already measures selection on a fill-independent, fixed-horizon outcome (`car_10`), but the definition lives in each script, it differs between the two lanes, and for ML use it has known defects: a window that contains a session closed before the brief existed, a β = 1 SPY benchmark, a horizon shorter than the trade, and a population of briefed names only. This memo proposes a **versioned label registry**: one label per DECISION, computed once, stamped in the store under a version key, and read by every model.

TL;DR of the recommendation:

1. **One label per decision layer**, never exchanged: selection (T1-T3) trains on a fixed-horizon abnormal return over ALL proposals; entry (T5) on censored time-to-fill; exit (T6/T7) on a policy-minus-no-policy delta; sizing on a downside distribution. §5.
2. **Selection label `sel_ar_h`:** anchor at the OPEN of the first session a reader of the brief can trade (`ladder_arrival_session`), and only when the date's candidate set was final before that open; fixed horizon `h`; benchmark = IWM times the raw OLS beta over the 250 sessions before the anchor, intercept not carried (one liquid factor; out of sample no richer benchmark removed materially more noise, and shrinking the beta toward 1 predicted later betas worse, §4, §12 D2); gross of cost; continuous. Sector-relative (large-cap SPDR, small-cap sector ETF) and SPY variants stored beside it as sensitivities, and every registered test is repeated on them. §5.1.
3. **Store both scales:** the raw abnormal return AND the same return divided by ex-ante residual volatility × √h. ATR is the only standing signal and ATR spans 2.6× across candidates, so which scale the model trains on decides what "ATR predicts" means. No separate ATR test is run on this label; ATR is confirmed once in #1227 and reported beside every model (§8 step 3). §4, §6.
4. **Label every proposal, not only briefed names.** `proposal_shadow` holds a median 12 LLM proposals per date, 38 % of them rejected by the gates; the selection estimand needs the rejected names. Gate status is kept per (date, ticker, theme); the mechanical arm is a separate population, never pooled. §5.1, §12 D3.
5. **Missingness is a coded outcome, never a silent drop** (split guard, no close at horizon, benchmark missing, immature). §5.1.
6. **Horizons 1 / 3 / 5 / 10 / 20 / 40 stored; primary = the 20-session terminal value `sel_ar_20`** (D1, locked before any outcome read). Secondary, pre-specified: `sel_car_mean_20` (mean of the cumulative path over sessions 1-20) and the path at 1 / 3 / 5 / 10 / 20. §12 D1.
7. **Inference respects calendar overlap.** 73 arrival sessions span 77 trading sessions, so a 20-session window gives ~4 non-overlapping calendar blocks today. Selection questions are asked WITHIN session (session fixed effects), where names sharing a window share its common shock; between-session claims need block bootstrap with blocks ≥ h.
8. Registered looks keep their registered outcome (`car_10`, `car_10_ex0`, `car_20_event`). The registry binds registrations filed AFTER it is locked. §7.

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
4. **The benchmark is the cheapest alternative the decision replaces.** For a thematic small / mid-cap pick that is its size-matched sector exposure (a small-cap sector ETF plus the small-cap market), not only SPY — and not a mega-cap-weighted sector fund it does not resemble (§4).
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
| Episodes per arrival session | p10 **2**, p50 **5**, mean 6.2, p90 **11**; **23 of 74** sessions carry ≤ 3 episodes | a within-session rank / Gaussianised label (Numerai-style) is not estimable — rejected, §6 |
| `technical_atr_pct` across episodes | p10 3.14 %, p50 4.80 %, p90 8.17 % (p90/p10 = **2.6×**) | if label dispersion scales with ATR, a raw-return loss is weighted ~7× (2.6²) toward high-ATR names; the scaled twin is required (§5.1) |
| 60-session OLS beta vs SPY (pre-anchor window) | p10 −0.28, p25 0.18, **p50 0.83**, p75 1.86, p90 2.97; **18.9 % negative**; `|β−1| > 0.5` for **69 %** | β = 1 is wrong for most names; the unshrunk estimate is too noisy to use raw (next row) |
| Are the negative betas artefacts? | split guard hit in 1 of 462 pre-windows; ≥ 10 flat sessions in 0.4 %; daily residual sd p10 1.9 %, p50 3.2 %, p90 5.3 % | not splits, not staleness — estimation noise of a 60-session single-name beta at 3 %/day idiosyncratic risk. A market-model label needs shrinkage and a longer window |
| 60-session beta vs own sector ETF | p50 0.66, p10 −0.28, p90 1.74; `|β−1| > 0.5` for 60.5 % | sector-relative does not fix beta by itself |
| **250-session** beta vs own sector ETF (458 episodes, all with 250 usable pairs) | p10 0.16, p25 0.44, **p50 0.89**, p75 1.38, p90 1.68; **4.8 % negative** (19.9 % at 60 sessions); corr with the 60-session estimate 0.83 | the long window removes most of the noise; β = 1 still misstates a p10-p90 range of 0.16-1.68, so the primary label uses the shrunk 250-session beta (§5.1) |
| When was each date's candidate set final? (journal of `alphalens-thematic-build`, 2026-05-25 … 2026-09-16) | **16 of 114** asof dates had their last candidate RECOMPUTE after the open of the ladder arrival session: 13 before the idempotent freeze existed (asof ≤ 2026-06-15), 3 after it (2026-08-02, 08-18, 08-19 — a config change or a degraded first run breaks the freeze) | for those dates a name may not have been visible at the anchor open; the label must know when the set was final (§5.1). The same fact bears on the ladder replay's arrival VWAP — out of scope here, see §8 |
| Calendar overlap of label windows (arrival = `ladder_arrival_session`) | **73 arrival sessions over a span of 77 trading sessions**; non-overlapping calendar blocks: h10 **7.7**, h20 **3.9**, h40 **1.9** | clustering by arrival session does not make windows independent; any claim that depends on the COMMON (between-session) component has ~4 independent blocks at h20. Within-session contrasts cancel the shared window shock (§5.1 inference) |
| Benchmark fit IN SAMPLE (superseded by the next row), 250 pre-anchor sessions, median R² of daily candidate returns (458 episodes; 434 with a small-cap sector ETF) | SPY 0.085 · large-cap SPDR sector 0.079 · IWM 0.116 · small-cap sector (PSC*) 0.118 · SPY + SPDR 0.121 · **IWM + PSC* 0.165**; PSC* beats the SPDR on 68.7 % of paired episodes; median corr(SPDR, SPY) 0.73, corr(PSC*, IWM) 0.83 | the Select Sector SPDRs hold only S&P 500 names, cap-weighted (XLK is dominated by a few mega caps); for $0.5-10 bn candidates the SPDR explains LESS than SPY. In sample the small-cap pair explains the most variance — but in-sample R² rises mechanically with a second factor and says nothing about noise in a FUTURE window; see the next row |
| Benchmark fit OUT OF SAMPLE (434 episodes): loadings fitted on the first 190 of the 250 pre-anchor sessions, residuals measured on the last 60 (still before `t0`) | median residual variance relative to SPY — daily: IWM 1.000, SPDR 1.006, PSC* 0.992, SPY+SPDR 0.992, IWM+SPDR 0.974, IWM+PSC* 0.975; **20-session compounded blocks** (label-shaped): IWM 0.988, SPDR 0.976, PSC* 0.952, SPY+SPDR 0.976, IWM+SPDR 0.975, IWM+PSC* 0.992. IWM+PSC* beats IWM+SPDR on 54.8 % (daily) / 53.9 % (20-session) of episodes — a coin flip | every benchmark leaves 95-100 % of SPY's residual variance: idiosyncratic risk dominates these names, so the benchmark choice barely changes label noise. The in-sample ranking does not survive; the simplest liquid factor is enough (§5.1) |
| Liquidity of the candidate factors (one recent anchor) | median daily dollar volume: SPDR sector ETFs $0.6-2.2 bn; Invesco small-cap sector ETFs **$0.0-1.2 m** (PSCD ~$0.0 m, PSCF / PSCM $0.1 m) | the small-cap sector series are too thin to serve as the primary factor; kept as a sensitivity only |
| Session fixed effects: what they keep (458 episodes with ATR) | 4 singleton sessions (4 episodes lost to FE); 40 of 73 sessions carry ≥ 5 episodes; **80.1 % of ATR variance is within session** | a within-session ATR test keeps most of the ATR variation; it is not driven by a handful of sessions |
| Collinearity of candidate ATR-test regressors (Spearman, pre-event) | ATR–MAX 0.62, ATR–prior-20 return −0.17, MAX–prior return 0.31, ATR–60-session sd 0.88; VIF (ATR, MAX, ret20) 1.53 / 1.98 / 1.52 | collinearity is moderate, not extreme — but MAX is a plausible MEDIATOR of ATR, which is a design problem, not a VIF problem (§8 step 3) |
| Reconciling the counts | 462 episodes; 458 have a resolved sector (4 unresolved) — the 458 in the rows above is sector-resolution, not a lack of history; 74 arrival sessions under `session_on_or_after(brief_date)` (the old `car_10` rule) vs **73** under `ladder_arrival_session` (a Friday brief and a weekend brief land on the same Monday) | counts differ by definition, not by exclusion |
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
| **T5 entry adverse selection** | whether a filled tier is a falling knife | `post_fill_ar_5` = abnormal return from fill to fill + 5 sessions (primary benchmark of §5.1) | filled tiers | descriptive / Huber | — |
| **T6 IN-FLIGHT / T7 EXIT** | a stop / take-profit / trail policy | `policy_delta_h` = lens abnormal return minus the NO-POLICY abnormal return over the same fixed window from the same filled entry; after a lens exits before `h` its leg holds the benchmark, so its abnormal return is 0 for the remaining sessions (Kaminski-Lo stopping premium); sessions in-market stored beside the delta | filled episodes, one lens per row, lens registry cap applies (R4) | paired session-cluster bootstrap of the delta; winners-harmed count first-class | raw `realized_r` alone (it cannot separate the pick from the policy) |
| **Sizing** (outside ADR 0013 today) | how much risk per name | `sel_ar_h` quantiles q10 / q50, `mae_h` (max adverse excursion from anchor, sector-relative), P(disaster stop within h) | same population as selection | pinball loss, calibration of the lower tail | a win flag; historical `suggested_size_pct` (it is a policy, not an outcome) |
| **Meta-label** (future "take this trade?") | act / skip a briefed name WITH its setup | triple-barrier outcome with the ACTUAL disaster stop, first take-profit and time stop | briefed plannable setups | multiclass log loss + policy P&L | used only here — never as a selection label |

### 5.1 The selection label in detail

**Anchor (D4, locked).** `t0 = OPEN(ladder_arrival_session(brief_date))` for the thematic lane — the official open of the first NYSE session after the brief date (`feedback/ladder_config.py`, #1416): a brief dated Monday 2026-08-10 starts at the open of Tuesday 08-11; Friday, Saturday and Sunday briefs start at Monday's open; a date before an exchange holiday starts at the first session after it. For the event lane the lane's registered rule (`session_on_or_after(brief_date)`) already makes that session the arrival; keep it. Benchmark legs start at the same open. A brief published after that open never shifts the anchor to a later session: the date is excluded (`set_final_after_open`). The anchor is justified by information timing (the first price after the brief exists), not by tradability — the fill is the ladder's label. Verified: the grouped-daily `o` is the official open (100 of 100 sampled ticker-days equal to Yahoo's open, max difference 0.01 %). A delayed open keeps the official open price and sets `delayed_open`; no open at all is the code `no_open`.

**The population is the brief as published before `t0`.** The owner reads the brief twice on D+1 — around 09:00-10:00 Warsaw time and just before the NYSE open (15:30 Warsaw) — and not after the open. So the list a decision was made on is the list that existed before `O[t0]`, not the last version of the file. Today briefs for date D are rebuilt at six slots on D+1, the last one after the US close. Since 2026-06-15 the mapper freezes the candidate set at the first deciding slot (≈ 01:30 UTC) and later slots only rewrite prose — but a config change or a degraded first run breaks the freeze, and before the freeze existed every slot recomputed. Measured from the build journal: 16 of 114 dates had their last candidate recompute AFTER the open of `t0` (§4). The journal's per-slot brief counts show the lists really changed on those dates, in BOTH directions: at least 37 names were added after the open (never seen by the owner) and at least 32 names seen before the open are missing from the final file (2026-08-02: 10 before the open, 1 at the end). Removed names cannot be recovered — the stores keep only the last version.

Rule: going forward, #1479 decides the brief once per date and makes it immutable after publication, with a `brief_published_at` stamp; the stamper labels a row only when that time is before `O[t0]`. Historically no per-name publication time exists, so the 16 dates get the status `set_final_after_open` and are excluded (this drops the added names but cannot restore the removed ones — stated, not fixed). Is the exclusion itself a bias? 13 of the 16 dates are the whole pre-freeze era (asof ≤ 2026-06-15, every slot recomputed) and 3 are freeze breaks after a deploy or a degraded first run — mechanical causes, not news intensity. The rule therefore drops mostly an EARLY PERIOD, which is a regime restriction to state, not a hidden selection on busy news days; the 3 later dates are audited against pre-open variables (candidate count, theme, weekday, VIX) before the lock.

**Open-price realism.** The grouped-daily open is the official open print, not a fill. Attention-driven small caps tend to rise overnight and reverse intraday, so a buy at the open carries a hidden cost ("Overnight Returns and the Hidden Cost of Buying at the Open"). The label is named for what it is — the return available to a reader of an on-time brief who buys at the open — and stored sensitivities bound it: `sel_ar_h_close_prev` (anchor close(`t0` − 1)), `sel_ar_h_close` (anchor close of `t0`), `sel_ar_h_vwap` (anchor daily VWAP of `t0`) and `sel_ar_h_vwap30` (arrival 30-min VWAP, briefed names only, where minute bars exist). A decomposition is stored beside them: overnight (close(`t0` − 1) → open), day-0 intraday (open → close(`t0`)), and the rest of the window. Measured before the lock, on the 250 pre-anchor sessions only: on high-volume days split by the overnight gap, the market-adjusted open → close was +1.5 % after gaps above +5 %, +2.2 % after +2..+5 % and −1.4 % after gaps below −2 % (2816 stock-days) — continuation, not the open-overpricing reversal the literature reports for attention stocks. `pre_ar` (close(D) → open(t0)) stays a diagnostic, never P&L. No per-name first-appearance time exists in the stores, so the rule works per date, not per name.

Why not the current `car_10` anchor: `car_10` starts at close(`session_on_or_after(brief_date)` − 1). For a weekday brief dated D that window CONTAINS session D, which closed before the brief existed and from whose close the features were computed. The July adversarial review measured that session at ~14 % of `car_10` variance. That is not tradable by the group and it couples label to features mechanically. The options pre-registration already moved to `car_10_ex0` for this reason; the registry makes it the rule. Keep `pre_ar = close(D)/close(D−1)` versus benchmark as a separate diagnostic column (the pre-publication reaction), never inside the label.

Why OPEN and not the 30-min VWAP (`reference_close`): the open is in the grouped-daily store for every ticker including the rejected proposals, so the whole population can be labelled from one source; the VWAP needs minute bars that exist only for briefed names. The VWAP stays the anchor of T5 execution labels.

**Horizons.** Stored: 5, 10, 20, 40 sessions (`h` inclusive of the arrival session), plus the non-overlapping increments 1-10, 11-20, 21-40 so decay can be read without re-counting overlapping windows. One PRIMARY horizon per registration (budget rule 6). **Locked primary: the terminal value at 20 (D1).** Pre-specified secondary: `sel_car_mean_20` = mean of the cumulative abnormal path over sessions 1..20, the path at 1 / 3 / 5 / 10 / 20, and a split by whether the catalyst event names the firm (`primary_entities`). The choice between a terminal value and the path mean is a bet on the speed of the reaction, not a power result (§12 D1).

- Hold today: p50 27 sessions, p95 42 (`/edge`, 2026-09-16); time stop 42 sessions; entry TTL 7 sessions. A 10-session label scores a pick on a third of the trade it feeds.
- The event lane registered `car_20_event` / `car_40_event`; the same pair for the thematic lane gives the project one convention.
- 20 matures twice as fast as 40 and overlaps half as much; at h40 only 269 of 462 episodes have elapsed today.
- Perplexity recommended 30 (centre of a 20-40 hold). Rejected as primary: it is a new, unused horizon.
- The calendar overlap in §4 (h20 ≈ 4 non-overlapping blocks) does not by itself favour h10: the selection question is asked within session (see Inference), where overlap across sessions matters much less. It does make every between-session claim at h20 or h40 weak for months.
- **The primary horizon was locked BEFORE any outcome-based read.** The only quantitative input was an outcome-blind synthetic simulation built on the real session structure (73 arrival sessions, real session sizes) with artificial prices; its result and its adversarial review are in §12 D1.

**Benchmark (D2, locked).** Primary: IWM times `beta_ols`, the raw OLS slope of daily close-to-close stock returns on daily IWM returns over the 250 sessions ending at close(`t0` − 1); intercept NOT carried; fallback `beta = 1` with a coded `beta_source` when the window is thin or degenerate (the `estimate_beta` contract, #996).

Why this and not the richer model the second pass adopted (kept below as a record of what was rejected): out of sample, inside the pre-anchor window, no benchmark removed materially more residual variance than any other — all sit within 5 % of SPY, and the in-sample winner (IWM + small-cap sector) ranks near the bottom on 20-session blocks (§4). Idiosyncratic risk dominates these names. The choice therefore goes to the factor with the fewest estimated parameters, deep liquidity, and a size match to the $0.5-10 bn candidates: IWM. The draft used a Blume-adjusted beta; the lock uses the raw beta because, measured outcome-blind (fit on the first 190 pre-anchor sessions, target = OLS beta on the last 60), shrinking toward 1 or toward a peer mean predicted the later beta worse than the raw estimate: MSE β = 1 1.18, Blume 0.60, Vasicek 0.63, raw 0.46, exponentially weighted (half-life 60) 0.40 (§12 D2). A fixed β = 1 is the worst option by a wide margin.

Sensitivity labels, each stored and each re-run in every registered test (a sign change against the primary is reported as benchmark fragility):

- `sel_ar_h_spdr1` — large-cap SPDR sector, β = 1 (the sector map is today's SIC index, not point-in-time: stated, not fixed);
- `sel_ar_h_psc1` — small-cap sector ETF, β = 1 (thin: $0.0-1.2 m daily volume);
- `sel_ar_h_spy` — SPY, β = 1 (continuity with every past `car_k`);
- `sel_ar_h_iwm1` — IWM, β = 1;
- `sel_ar_h_blume` — IWM, Blume-adjusted beta;
- `sel_ar_h_ew60` — IWM, beta with exponential weights (half-life 60 sessions; the half-life was chosen on the same data, which is why it is not the primary);
- `sel_ar_h_mom` — factor model with a momentum factor (daily factor file), a check on the selection-on-prior-return bias (Ahern 2009) that the market model does not remove;
- `russell_recon_window` — flag for windows that span the late-June Russell reconstitution.

Rejected in the third pass — the two-factor small-cap model:

```
r_i = a_i + b_i * r_IWM + c_i * s_perp + e_i        s_perp = small-cap sector ETF return orthogonalised to IWM
```

- *(rejected)* **Factors.** IWM (small-cap market) and the Invesco S&P SmallCap sector ETF mapped from the SPDR sector (XLK→PSCT, XLV→PSCH, XLI→PSCI, XLF→PSCF, XLY→PSCD, XLE→PSCE, XLB→PSCM, XLU→PSCU, XLP→PSCC). Orthogonalising the sector leg keeps a disguised market loading from being reported as sector exposure. XLC / XLRE have no small-cap twin: fall back to IWM + the SPDR, `benchmark_source = spdr_fallback`.
- *(rejected)* **Why not the SPDR.** Measured IN SAMPLE (§4): for these candidates the large-cap SPDR explains less pre-event variance than SPY (median R² 0.079 vs 0.085), and IWM + PSC* explains the most (0.165). Choosing the benchmark by PRE-event fit is outcome-blind and minimises the benchmark noise left in the label (Ahern 2009: for samples selected on size and prior return, the benchmark matters more than Brown-Warner's generic result suggests).
- *(rejected)* **Shrinkage.** Vasicek: each loading shrunk toward the mean loading of its sector-peer episodes, weighted by its own standard error (imprecise estimates move more). This is NOT Blume (which shrinks market betas toward 1) — the first draft conflated the two. Fallback to the peer mean and a coded `beta_source` when the window is thin or degenerate (the `estimate_beta` contract, #996). Measured single-factor evidence for the window: the 250-session beta vs the SPDR has 4.8 % negative estimates against 19.9 % at 60 sessions.
- **Construction (kept for the primary).** Daily abnormal returns compounded as two wealth paths (stock, fitted benchmark) from `O[t0]` — not a single cumulative ETF return times a beta. Intercept `a_i` is NOT carried into the label (a 250-session alpha of a news-selected small cap is momentum, not an expected return: the picks trailed IWM by a median 33 % over the 250 pre-anchor sessions, so a carried alpha would add about +2.6 % of artificial lift per 20 sessions).
- *(rejected)* **Thin trading.** PSC* ETFs are small; zero-return share of both legs is stamped, and a Dimson lead-lag loading is a stored diagnostic, not the default.



Why sector first: briefs are theme-clustered (a third of episodes in XLK), so SPY-relative labels carry the theme's common move; the within-theme selection question (#1268 already pre-registered `sector_excess_return` as its secondary for exactly this) needs it removed. Characteristic-matched controls (DGTW: size, book-to-market, momentum) are the literature's stronger benchmark but need point-in-time shares outstanding and book values for the whole market, which the stores do not carry — deferred, not rejected.

**Formula.**

```
W_stock[t] = W_stock[t-1] * (1 + r_i[t]);   W_bench[t] = W_bench[t-1] * (1 + beta_ols * r_IWM[t])   (both = 1 at O[t0])
sel_ar_h   = W_stock[t0+h-1] - W_bench[t0+h-1]
sel_zar_h  = sel_ar_h / (sigma_resid_pre * sqrt(h))
```

`sigma_resid_pre` = daily residual sd of the stock against the same benchmark over the 60 sessions ending at close(t0 − 1). It is deliberately NOT `technical_atr_pct`: dividing the label by a feature would make the label a function of that feature. The two are strongly related. Caution for any analysis on `sel_zar_h`: dividing by a volatility that tracks ATR can create an ATR correlation with no predictive content — through a non-zero mean, and also through estimation noise in `sigma_resid_pre`, volatility-dependent skew, or ATR and `sigma_resid_pre` not being proportional (the first draft said "only through a non-zero mean"; that was too strong). So `sel_zar_h` is a training target for a scale-free ranking model, no confirmation test uses it as its primary (§8 step 3), and any use of it states the artefact and checks it by simulation.

**Gross, not net of cost.** Perplexity recommended net-of-cost labels. Rejected for selection: cost depends on order type, size and broker (T5 / execution), differs per group member, and does not belong to the pick. Costs enter the entry and sizing labels.

**Inference.** Two kinds of question need two procedures:

- **Within session** — "which of today's proposals did better" (the selection question): session fixed effects. Names from one arrival session share one window, so the common market / sector shock cancels in the contrast. Errors clustered by arrival session with CR2 and a null-imposed wild cluster bootstrap (Cameron-Gelbach-Miller). The cancellation is only partial: names in one session have different betas and sectors, and adjacent sessions' windows share 19 of 20 days, so residuals stay correlated across sessions. A moving-block bootstrap over calendar-ordered sessions (blocks ≥ `h`) is reported beside CR2 for every within-session test; a disagreement between the two is reported, not resolved by picking one. Measured support for the design: 80 % of ATR variance is within session, 40 of 73 sessions carry ≥ 5 names (§4).
- **Between sessions** — anything about the level of the label over time (e.g. "the proposals beat the benchmark on average"): moving-block bootstrap over arrival sessions in calendar order with blocks ≥ `h`, or HAC on session-aggregated scores with lag = actual window overlap. Today that is ~4 blocks at h20 (§4), so such claims are weak by construction and are reported as such. A plain Newey-West t-test on ~73 session means is NOT an acceptable procedure: measured on independent normal data it rejects 7.9 % / 10.9 % / 15.1 % of true nulls at lags 10 / 20 / 40 against a nominal 5 % (§12 D1).

**Population (D3, locked).** Every LLM proposal in `thematic_candidates/proposal_shadow` (post market-cap bracket, pre gates) is labelled, one label per (brief_date, ticker), briefed or not. Rules:

1. **Stage status beside the label, never as the label.** `gate_passed` per (date, ticker, THEME) — 34 of the 82 date-tickers proposed under more than one theme passed the gate in one theme and failed in another — plus `briefed_any_theme` per (date, ticker). Stage flags select the population and stratify evaluation; they are NEVER predictors of the decision that produced them (a `briefed` flag used to predict selection is a collider).
2. **Earlier stage too.** The pre-bracket `thematic_candidates/proposal_funnel` rows (since 2026-08-06) get the same label with their `bracket_verdict`, so the bracket stage can be modelled on the population it acts on.
3. **The mechanical arm is a separate population** keyed by `source`, labelled only where the entity resolves to a US-listed ticker, never pooled with LLM rows, and compared only after size matching (median pre-anchor dollar volume $1.9 bn against $66 m for LLM proposals; 2.3 % overlap; entities such as "OPENAI" or "SAMSUNG" are not tickers). It belongs to the head-to-head design, not to the selection model.
4. **Excluded dates:** 2026-08-02, 08-18 and 08-19 — the shadow was rewritten after the arrival open (the same three freeze breaks as the brief, §4).
5. **Mapper config versions are never pooled.**
6. **Features must exist before the stage the model replaces** (a gate output is never a feature of a gate model).
7. **Labels live in their own store**, never joined into a feature table; the first read of reject labels is a registered look.

Verified before the lock: the shadow covers 66 of 66 calendar days (2026-07-12..09-15); on every day the shadow, the candidates parquet and the brief come from the same map-themes run (same config token, same write time; the frozen-reuse branch writes neither file); every candidate is briefed; the 1.2 % of brief rows absent from the shadow are event-lane rows. The estimand is "which of the names this pipeline proposed did better", not selection skill over the whole market. Today only ~155 matured rejected proposals exist, all in one config version: D3 is about storing, not about training now.

**Missingness codes** (stamped in `sel_label_status`, one per horizon): `ok`, `immature`, `split_guard` (any consecutive-close ratio outside [0.55, 1.8] inside the window), `no_close_at_horizon` (ticker absent — treat as a delisting candidate, never forward-fill), `benchmark_missing`, `sector_unresolved`, `no_open`, `set_final_after_open`; flag `delayed_open` beside `ok`. Every model report prints the code counts by ATR tercile, so a guard correlated with volatility is visible.

**Version key.** `SEL_LABEL_VERSION` over anchor rule, horizons, benchmark rule, guard bounds, shrinkage rule and residual window — the `EVENT_CAR_VERSION` pattern.

## 6. Adjudication of the literature review (what was taken, changed, rejected)

| Recommendation (Perplexity, 2026-09-16) | Decision | Reason |
|---|---|---|
| Fixed horizon for selection, path-dependent labels only for exit | **taken** | same as ADR 0013 R1 |
| Anchor at first executable price | **taken** (open of ladder arrival) | §5.1 |
| Primary horizon 30 sessions | **changed → 20** (owner decision D1) | lane consistency, maturation; locked before any outcome read, informed only by a synthetic power simulation |
| Matched characteristic controls (DGTW) as primary benchmark | **deferred** | no PIT shares outstanding / book value in the stores |
| Sector-adjusted benchmark | **sensitivity only** (SPDR and small-cap sector, β = 1) | theme clustering, 99.1 % coverage, #1268 precedent |
| Market-model beta | **taken, one factor (IWM), 250 sessions, raw OLS beta, intercept not carried** | §4: 60-session beta is noise-dominated; out-of-sample fit shows no richer benchmark removes materially more noise (third pass, §11); shrinkage toward 1 predicted later betas worse (§12 D2) |
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

0. **D1-D5 locked 2026-09-17, before anything read an outcome** (§9, §12). Record the decisions and the `SEL_LABEL_VERSION` freeze timestamp in the looks ledger (§4 row, no charge). A later change is a new version and a new registration.
1. **Stamp the selection label** — pipeline side, `feedback/selection_label.py` beside `event_car.py`, same shape (newest-first, disk-first from the grouped cache, atomic rewrite, deadline, never raises), for briefed rows AND a new `proposal_shadow` label parquet. Parquet-only; nothing on the wire (no interim peeks). TDD with hand-computed windows, including a weekday brief whose day-0 session must be excluded and a date whose candidate set was recomputed after the open. It depends on #1479 (one immutable brief per date with `brief_published_at`); until #1479 ships, the journal read in §4 is the only availability source.
2. **Promote the census** (`label_census*.py`) to `scripts/ml/2026_09_label_census.py` — outcome-blind, prints the §4 table and the missingness code counts by ATR tercile.
3. **No separate ATR test (D5).** The draft proposed one registered look at ATR on `sel_ar_20`. Rejected: ATR already has a held-out confirmation planned (#1227: ATR, MA50 extension, press gate, Holm, power-gated, Wake 2026-10-14) and a July kill line for the live ATR tilt (`selection_score_v2_ext_tilt_decision_2026_07_06.md` §5), both on the same held-out episodes. A third analysis would be another look at the same signal on the same data (ledger rules 3 and 6). Instead:
   - **#1227 is registered on `sel_ar_20` as its primary** for all three signals, `car_10` descriptive only. The registration is not yet filed, so this is a pre-registration choice, not an outcome switch. It needs a ledger amendment (primary horizon of clusters 1, 2 and 9) and the discovery effects recomputed on `sel_ar_20` on the burnt discovery panel for the power simulation. Cost stated: if part of the discovery ATR effect came from the pre-brief session or from β = 1, the effect on the new label is smaller and the power gate is reached later.
   - **One ATR decision, not two.** When #1227 is registered, the July kill line is folded into it as one decision with three outcomes (confirmed / tilt unchanged / tilt retired); the owner decides the exact form at registration.
   - **Reporting rule from now on:** every model and every analysis on the new label reports an ATR-only baseline beside it. This is a report, not a promotion gate (a gate would need its own registered protocol).
   - **Until #1227 reports, no model influences selection** without its own registration (ledger rule 3 already requires this).
4. **#1479 (filed 2026-09-16): one immutable brief per date.** The ladder replay has the same timing gap as the label. Measured impact: the default 90-day `/edge` window holds 21 affected rows (headline +3.4 % → +3.5 % without them — cosmetic); the full history holds 166 of 699 matured rows (+2.0 % → +2.4 %). #1479 removes the cause (one deciding run at 00:30 UTC, repair-only runs, deadline at the open) and flags the 16 historical dates.
5. **README house rules update:** rule 4 names `sel_ar_h` / `sel_zar_h`; rule 9 gets the embargo = horizon statement; a new rule: "a model reads the stamped label; recomputing a label in a script is a new definition and needs its own version".

## 9. Owner decisions (locked 2026-09-17)

| # | Decision | Locked choice | Evidence (§12) |
|---|---|---|---|
| D1 | Primary selection horizon | **`sel_ar_20`, the terminal value at session 20.** Secondary, pre-specified: `sel_car_mean_20`, the path at 1 / 3 / 5 / 10 / 20, a split by whether the catalyst names the firm. Inference stricter than a plain Newey-West test | synthetic simulation on the real session structure; adversarial review reversed an early preference for the path mean; literature on reaction speed |
| D2 | Primary benchmark | **IWM × raw OLS beta over 250 pre-anchor sessions, intercept not carried.** Sensitivities: EW-60 beta, Blume beta, IWM β = 1, SPY, SPDR sector, small-cap sector, momentum-factor model, Russell-reconstitution flag | beta holdout prediction; thin-trading check; benchmark-only window read |
| D3 | Label the rejected proposals | **Yes**, with the seven population rules in §5.1 | shadow census, run consistency, multi-theme verdicts |
| D4 | Anchor | **Official open of the first NYSE session after the brief date**, stock and IWM; a late brief excludes the date, never shifts it; close / VWAP anchors and a decomposition stored | vendor open verified against Yahoo; pre-anchor gap and reversal test |
| D5 | ATR test before any new model | **No separate test.** #1227 registered on `sel_ar_20`; July kill line folded into it; ATR-only baseline reported beside every model | overlap with #1227 and the July kill line |

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

### Second pass — literature check of D1-D5 (Perplexity, 2026-09-16)

Each point adjudicated; the measurable ones were measured outcome-blind before the text changed (§4 rows "Calendar overlap" and "Benchmark fit").

| Point | Verdict on the observation | Remedy decision | What changed |
|---|---|---|---|
| Session clustering ignores calendar overlap between sessions | **CONFIRMED** — 73 sessions over 77 trading sessions; h20 ≈ 3.9 blocks | within-session questions with session FE; block bootstrap / HAC for between-session claims — **taken** | §1 item 7, §5.1 Inference |
| Horizon 20 not "confirmed" by literature; power simulation must be built from daily returns | **CONFIRMED** | simulation spec — **taken**; h10 as timing diagnostic — **taken**; embargo = h replaced by exact purging of overlapping label intervals for CV — **taken** in spirit, kept conservative for the scientific test | §5.1 Horizons, D1 |
| Blume ≠ Vasicek; shrink toward peer mean | **CONFIRMED** (naming error in the first draft) | Vasicek toward sector-peer mean, SE-weighted — **taken**, then *superseded by the third pass* (look-ahead through later peers; Blume adopted) | §5.1 Benchmark |
| SPDR sector ETFs are S&P 500-only, mega-cap weighted; sector beta ≈ disguised market beta | **CONFIRMED in sample only** — SPDR R² 0.079 < SPY 0.085; corr(SPDR, SPY) 0.73. *Superseded by the third pass: out of sample the difference vanishes* | two-factor market + orthogonalised sector — **taken**, with IWM + small-cap sector ETFs chosen by pre-event fit (0.165); characteristic-matched controls (Ahern) — **still deferred** (no PIT size/book data) | §4, §5.1, D2 |
| Multiply cumulative ETF return by beta ≠ compounding daily expected returns | **CONFIRMED** | wealth-path construction — **taken**; include alpha — **rejected** (a pre-event alpha of a news-selected small cap is momentum) | §5.1 formula |
| Labelling all proposals: not literally the selective-labels problem, but stage flags can leak | **CONFIRMED** | flags never as predictors — **taken** | §5.1 Population |
| "13 × 74 ≠ 462" data audit | **REFUTED** — 462 are briefed episodes (mean 6.2 per session), proposals are a separate 66-date table (809 distinct LLM date-tickers); the memo never equated them | clarified the mean | §4 |
| Open anchor: hidden cost of buying at the open; per-date exclusion may select calm days | **CONFIRMED** (cost); **mostly REFUTED** (selection) — 13 of 16 excluded dates are the pre-freeze era, 3 are deploy / degraded-run breaks | named estimand + VWAP / close sensitivities — **taken**; audit of the 3 dates on pre-open variables — **taken** | §5.1 Anchor |
| "Ratio artefact only through a non-zero mean" | **CONFIRMED too strong** (noisy σ, skew, non-proportional ATR) | softened + simulation check — **taken** | §5.1 caution |
| WLS is not "return per unit of risk" | **CONFIRMED** | WLS demoted to an efficiency check in a Holm family — **taken** | §8 step 3 |
| ATR effect may be MAX / lottery or overreaction | **plausible, untested here** | MAX and prior return as pre-specified controls — **taken** | §8 step 3 |

### Third pass — adversarial review of the second-pass conclusions (`deepseek/deepseek-v4-pro`, fresh thread, 2026-09-16)

The reviewer got the memo only, not the author's reasoning. The measurable points were measured outcome-blind before the text changed (§4 rows "OUT OF SAMPLE", "Liquidity", "Session fixed effects", "Collinearity", "Reconciling the counts").

| # | Finding | Observation | Remedy decision | What changed |
|---|---|---|---|---|
| 1 | Benchmark chosen by in-sample R²; a second factor wins mechanically; PSC* thin; sector map not point-in-time | **CONFIRMED** — out of sample the ranking collapses (all within 5 % of SPY; the in-sample winner is near the bottom on 20-session blocks); PSC* trade $0.0-1.2 m a day | out-of-sample criterion — **taken and run**; primary moved to the simplest liquid factor (IWM, Blume) — **taken**; point-in-time sector map — **not available**, stated on the sector sensitivities | §4, §5.1, D2, §6 |
| 2 | Vasicek peer mean can use later episodes (look-ahead) | **CONFIRMED** — the second-pass text did not restrict peers in time | Blume toward 1 needs no peers — **taken** (expanding-window Vasicek would also work but adds a parameter for no measured gain) | §5.1 |
| 3 | Session FE does not fully cancel overlap; sparse within-session variation | **PARTLY CONFIRMED** — residual cross-session correlation is real; "driven by a handful of sessions" **REFUTED** (80 % of ATR variance within session, 40 / 73 sessions ≥ 5 names, 4 singleton episodes) | calendar block bootstrap beside CR2 — **taken**; "folded" non-overlapping subsample — **rejected** (at ~4 blocks it discards nearly all data) | §5.1 Inference |
| 4 | Journal parsing may miss recomputes; exclusion is a regime restriction | **observation CONFIRMED as a risk** | cross-check with the candidates parquet mtime — **rejected**: every slot rewrites the file, so its mtime is the last slot, not the recompute; full-sample sensitivity including the 16 dates — **taken** | §8 step 3 |
| 5 | MAX collinear with ATR; MAX may be a mediator | collinearity "extreme" **REFUTED** (VIF ≤ 2.0, corr 0.62); mediator **CONFIRMED** as a design problem | total-effect estimand, pre-treatment controls only; MAX model descriptive — **taken**; formal mediation analysis — **rejected** at this N | §8 step 3 |
| 6 | 462 vs 458 and 74 vs 73 unreconciled | **CONFIRMED** unreconciled; the reviewer's guesses at the cause were both **wrong** (458 = sector resolution, 73 = the arrival rule) | reconciliation row — **taken** | §4 |
| 7 | Over-engineered for 73 sessions | **PARTLY CONFIRMED** — the out-of-sample result shows the benchmark complexity bought nothing | simpler primary + every test repeated on the sensitivity labels — **taken**; horizons and stage flags kept (stamping is cheap and outcome-blind; the forking risk sits in the registration, which names one primary) | §5.1, §8 |

## 12. Decision record (2026-09-16/17)

Every number below was measured outcome-blind: pick prices only from sessions strictly before the anchor, post-anchor reads limited to IWM / SPY and to open-price availability. Each decision went through a literature check (Perplexity, asked without the draft recommendation where the recommendation could anchor the answer) and an adversarial review (`deepseek/deepseek-v4-pro`, fresh thread per decision), and every review finding was run before it was accepted or rejected. Scripts: session scratchpad `sim_horizon2.py`, `d2_*.py`, `d3_*.py`, `d4_*.py` (to be promoted with the census, §8 step 2).

### D1 — horizon and statistic

- **Simulation.** Real structure (73 arrival sessions, 462 picks, real per-session counts), artificial daily prices (IWM t5 shocks, 8 theme factors with 60 % within-session concentration, lognormal idiosyncratic sd with median 3.2 %, estimated-beta error), known effect paths, 5 000 replications. Minimum detectable mean effect (80 % power, size-adjusted), entry-day noise 1.6×:

| True path | A10 | A20 | A40 | C20 (path mean 1..20) |
|---|---|---|---|---|
| immediate jump | 1.8 | 2.7 | 4.3 | **1.6** |
| jump, half given back by 20 | 2.3 | 5.3 | — | **2.1** |
| peak at 11, back to 0 | **1.9** | blind | blind | 3.2 |
| steady rise to 20 | 3.5 | **2.7** | 4.3 | 3.0 |
| delayed (flat to 5, rise to 20) | 5.3 | **2.7** | — | 4.0 |
| slow rise to 40 | 7.1 | 5.3 | **4.3** | 6.1 |

- **Review outcome.** A first reading ("C20 loses little where A20 wins") was refuted: the first shape set had two of five paths blind for A20 and no delayed path; with the delayed path C20 needs a 49 % larger effect. The choice is a bet on reaction speed. Rejected review claims: a bug in the Newey-West code (the same code on iid normal data gives 7.9 / 10.9 / 15.1 % at lags 10 / 20 / 40 — the estimator's small-sample bias, not a defect) and "immediate jump favours neither" (1.6 vs 2.7).
- **Why A20.** The tool selects names linked to a theme, often not named in the article; the closest literature predicts gradual diffusion to linked and neglected small firms (Cohen-Frazzini 2008, Menzly-Ozbas 2010, Hou 2007, DellaVigna-Pollet 2009) against an attention pop and reversal (Barber-Odean 2008, Da-Engelberg-Gao 2011). Fast pops are already measured by the exit ladder's own label.
- **Entry-day noise** (pre-anchor high-volume days, open → close abnormal sd vs a normal close → close day): median 1.6×, p75 2.1×.

### D2 — benchmark

| Check | Result |
|---|---|
| OLS beta vs IWM, 250 sessions, p10/50/90 | 0.22 / 0.81 / 2.14 |
| Dimson (lag + lead) minus OLS | median −0.009; Dimson higher in 48.7 % → no thin-trading bias; Dimson adds noise |
| Beta prediction, fit 190 → OLS beta on last 60 (MSE) | β = 1 1.18 · Blume 0.60 · Vasicek (per-ticker prior from earlier episodes, variance floor) 0.63 · raw 0.46 · EW half-life 60 0.40 |
| Beta drift | first-190 → last-60 slope 1.23, reverse direction 0.49, beta sd 0.66 → 0.87, IWM vol flat → betas diverged over time (calendar regime or selection effect, unresolved) |
| Prior return vs IWM (raw differential) | 60 sessions median −5.5 %; 250 sessions median −33 % |
| Benchmark-only window read (IWM / SPY legs only, 359 elapsed episodes) | mean label shift Blume vs β = 1 0.00 %; IWM vs SPY 0.28 %; per-episode shift sd 1.2 % (Blume vs β = 1), 0.6 % (raw vs Blume) |

Review: "Blume was pre-committed" was false (D2 was open) — withdrawn; the first Vasicek implementation floored a negative prior variance and pooled repeated tickers — fixed, result unchanged; "momentum selection refuted" withdrawn (raw differential, not a residual). Rejected: close-to-close beta applied to an open-anchored window (19 of 20 nights are inside the window) and carrying the intercept (+2.6 % artificial lift).

### D3 — rejected proposals

Census: 66 shadow days, 809 LLM date-tickers (median 12 per date), 61.6 % briefed, cohorts v1 451 / v2 60 / v3 13 / v4 378 rows, h20-elapsed rows only in v1 (407) and v2 (58). Open available: briefed 100 %, rejected 99 %, mechanical 94.3 %. Review findings accepted: per-theme gate status (34 / 82 mixed); separate label store. Rejected after running them: an incomplete shadow (the gap is event-lane rows), a shadow from a different run than the brief (66 / 66 consistent), manual removals between candidates and brief (none), mechanical rows inside the bracket stage (the funnel holds LLM proposals only).

### D4 — anchor

Pre-anchor medians across stocks: |close → open| 0.6 % on all days, 1.8 % (p75 3.8 %) on high-volume days; |open → daily VWAP| 1.3 % and 3.4 %. Review findings accepted: the reversal check had to be split by gap sign and market-adjusted (done: continuation), the draft's argument against VWAP was illogical (withdrawn; the valid reason is that VWAP forms after the window starts), the anchor is defended by information timing rather than tradability. Rejected: an unverified vendor open (verified), a pre-brief start through the holiday mapping (the rule uses the XNYS calendar and #1479 closes late dates).

### D5 — ATR

Found during the review: #1227 and the July kill line already test ATR on the held-out episodes. Literature (registered reports, COMPare, FDA multiple endpoints; Gu-Kelly-Xiu 2020, Harvey-Liu-Zhu 2016): a second test on the same episodes is not an independent confirmation, and a known predictor belongs in the baseline rather than in its own pre-model test. Review findings accepted: a secondary `sel_ar_20` endpoint inside #1227 would be a second look and a rescue channel; "a model must beat ATR" as a gate was undefined and not free; #1227 plus the July kill line already form two looks at ATR and must become one decision.
