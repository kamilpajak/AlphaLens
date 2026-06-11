# Tuning AlphaLens from EDGE outcomes: data requirements & the selection-vs-ladder decomposition

**Status:** LOCKED — §7 log-now PRs 1-5 SHIPPED 2026-06-11 (#522-#526); statistical/inference items (§7 PR-8..12) remain behind the N≥30 gate
**Date:** 2026-06-11
**Scope:** What the ~3-month EDGE-driven tuning program needs, what we already persist, what must be logged NOW vs reconstructed later, and how (and whether) we can separate *wrong-candidate* from *badly-configured-ladder* causes at N~30-50.
**Method:** repo-grounded multi-agent workflow (4 discovery + 4 design lenses + 3 adversarial critiques + synthesis) cross-checked against external practitioner best-practice (Perplexity Sonar Deep Research, 26 sources — BuildAlpha E-ratio, JournalPlus MAE/MFE, Brinson-Hood-Beebower, Ortec, Man Group).

---

## 1. Question & TL;DR

In a few months we will tune the whole AlphaLens funnel (not just Buffett) by reading EDGE results. What data do we need, do we have it, and how do we tell whether a bad result came from *wrong candidate selection* vs a *badly-configured ladder*?

- **The decomposition is real at the row level, but NOT statistically actionable at N~30-50.** We can build a descriptive selection-vs-ladder split today from columns already in `edge_ladderoutcome`, but the diagnostic off-diagonal cell ("good pick, ladder lost it") will hold ~2-6 rows. Treat every slice as **descriptive-only / a maturation tracker** until pooled N clears ~150-200 with a theme-concentration guard.
- **Ship the logging now, gate the inference later.** A small set of genuine **log-now-or-lose-it** captures (a per-decision ladder-grid replay before cached minute bars are evicted; a `ladder_config_version` stamp; structured gate-verdict *reasons*; denormalizing selection covariates onto the immutable outcome row). Everything statistical (estimators, regime/theme slices, DR/IPW) is **can-add-later** over captured data.
- **Two adversarial corrections override the lenses.** (a) **Regime is RECOVERABLE, not log-now** — `FREDClient.fetch_series("VIXCLS")` returns full daily history; only the live cache stores the latest observation. Backfill `regime_at_brief` keyed on `brief_date`. (b) **The ε-injection / propensity-π arm is NOT a logging item** — it surfaces rank-4+ names to the real-money WhatsApp group. Server-side cap-lift + gate-check logging is safe now; the user-facing sampler is **BLOCKED pending explicit human GO/NO-GO**.
- **The two axes are NOT orthogonal as currently anchored, and one mechanically excludes the most important ladder failure.** `market_excess_return` anchors to arrival-VWAP (`reference_close`); `realized_r`/`mfe` anchor to `blended_entry` (the fill). A deep entry tier double-counts into both. And `realized_r` is NULL for `NO_FILL` — so "never got in", arguably the biggest ladder failure, is dropped from the capture axis. Fix the anchor before persisting any `capture_efficiency`.
- **Funnel-not-rank-top-N is the structural identifiability ceiling.** Never-proposed names have propensity π = exactly 0 (positivity violation, not a sample-size problem). The only identifiable estimand is the conditional-support one already locked in `docs/research/feedback_ledger_counterfactual_design_2026_06_02.md` (Variant A): `E[outcome | LLM-proposed ∧ in mcap-bracket ∧ reached verify]`. No EDGE data lets us claim "the LLM should have proposed X".

---

## 2. What "tuning AlphaLens from EDGE" actually means

EDGE outcomes (`/v1/edge`, backed by `edge.LadderOutcome`) are the realized market behavior of every verified candidate. Tuning means using those outcomes to adjust **two knobs** in different layers:

1. **Candidate-selection knob** — the funnel that decides *which* tickers surface: theme proposal (DeepSeek v4-pro), mcap filter, the 3 verification gates (`tenk`/`press`/`insider`), the Phase-D scorer (`layer4_weighted_score`), the diversity cap (`_MAX_CANDIDATES_PER_THEME = 3`, `thematic/mapping/orchestrator.py:58`). A wrong-candidate failure = the ticker did not move favorably regardless of how we traded it.
2. **Ladder-config knob** — the deterministic trade-setup geometry that decides *how* we trade a surfaced name: entry tiers (E1/E2/E3), TP tranches, disaster stop, order-TTL, the 42-session time-stop (`paper/constants.py TIME_STOP_DAYS = 42`). A bad-ladder failure = the ticker moved fine but our entry/exit geometry failed to capture it (tiers too deep to fill, stop too tight, TP banked too early).

**Why telling them apart is the crux:** the corrective action is completely different. A selection problem points at gate/scorer weights or theme filters; a ladder problem points at `build_trade_setup_from_frame` geometry constants. Without attribution you risk re-weighting the scorer to "fix" a stop-placement bug, or re-tuning the ladder to "fix" a bad pick. The whole program is **telemetry/firebreak only** (doctrine) — the deliverable is an *honest attribution a human reads*, not an estimator that writes scorer weights.

---

## 3. What we persist TODAY

### 3.1 EDGE outcomes — three measure layers (`edge.LadderOutcome` PG / `~/.alphalens/population_ladders/{date}.parquet`)

The replay engine (`feedback/ladder_replay.py`) produces three deliberately-separated layers per `(brief_date, ticker)`:

- **Layer 1 — substrate (policy-free):** `forward_return` (close-to-close from `reference_close`, fill-INDEPENDENT, exists even for NO_FILL), `mfe`/`mae` + `_pct` (R-units), `sequence_str` (e.g. `E1->E2->TP1->SL`), `ambiguous_bars`; plus benchmark-relative `benchmark_window_return` + `market_excess_return` (SPY same-window) from `benchmark_excess.py`.
- **Layer 2 — as-specified headline:** `realized_r`, `open_r` (mark-to-market for ongoing), `ladder_classification`, `blended_entry`.
- **Layer 3 — ratchet what-if:** `ratchet_realized_r` (BE-after-TP1 / lock-in-after-TP2 on the SAME cached bars — proof that multi-config replay is free).

Plus the **size layer** (`*_gross_weight_pct`, `stop_distance_pct`, `realized_risk_pct`, `*_return_pct_of_book`, `tiers_filled_count`), **TTL geometry**, `holding_days_elapsed`, `reference_close` (frozen arrival 30-min VWAP), `terminal`/`matured_at` gating, `chart_payload_json`.

### 3.2 Selection-time covariates (`briefs.Brief` PG + `~/.alphalens/thematic_briefs/{date}.parquet`)

`layer4_weighted_score`, `rank_in_day`, `cohort_size_in_day`, `brief_model_used`, `llm_confidence`, `catalyst_*`, `insider_score_usd` + sector percentile, `fcff_yield_pct`, `valuation_*`, `roic/roe`, `magic_formula_*`, `technical_*`, `gates_passed/failed/unknown`, `verified`, `peer_cohort_level`, `market_cap`, `next_earnings_date`, `brief_trade_setup` JSON (the immutable ladder geometry).

### 3.3 Population-monitor "baseline"

`population_ladder_monitor.py` replays **every verified + plannable candidate**, including rank-4+ names the diversity cap never surfaced — the closest thing to a counterfactual baseline, **with the §4/§5 caveat**.

### 3.4 Verified repo facts (checked for this memo)

- `edge/models.py` denormalizes **only `theme`** onto the outcome row. No `regime`, `market_cap`, `config_version`, or selection covariates.
- `briefs/ingest/parquet.py:254` **hard-deletes** Brief rows when their source parquet disappears (`Brief.objects.filter(date__in=deleted).delete()`).
- `FREDClient.fetch_series` returns the full series — regime is reconstructable.
- **Live config divergence:** `builder.py _DEFAULT_ORDER_TTL_DAYS = 10` vs `constants.py DEFAULT_ORDER_TTL_DAYS = 7`. The replay fallback uses 7, so rows already blend a 10-day brief intent with a 7-day replay fallback, and nothing records which TTL each row used.
- **Two-file orchestrator collision:** diversity cap is `thematic/mapping/orchestrator.py`; `rank_in_day` assignment is `thematic/argumentation/orchestrator.py`. Any ε-sampler must target the **mapping** file, or π for the cap-excluded pool can never be logged.

---

## 4. The selection-vs-ladder decomposition — method and LIMITS

### 4.1 Method (what the data supports)

- **Ladder-independent selection probe.** `forward_return` / `market_excess_return` is anchored to `reference_close` (arrival VWAP), independent of any fill, and exists even for NO_FILL. The edge summary already makes `market_excess_return` the headline — the selection probe is **already** the primary metric. *(External: this is the BuildAlpha "E-ratio" idea — a fill-independent measure of whether the signal itself had edge.)*
- **Ladder-capture term.** `realized_r` is the banked return; the intuitive ratio `realized_r / mfe` ("of the favorable excursion offered, how much did the TP/SL ladder bank?") is computable. *(External: JournalPlus "capture ratio"; <60% = systematic early exits, >80% = optimized — but see §4.2 for why we must NOT use the raw ratio.)*
- **Within-decision ladder-grid sweep (the genuinely sound idea).** The ratchet pass already re-walks the SAME cached bars under a different exit policy at zero Polygon cost. Generalize it: `replay_ladder(trade_setup, bars, ...)` is pure and `build_trade_setup_from_frame` is deterministic, so the same OHLCV can be re-laddered under K alternate configs (entry: {as-specified, single-fill-at-arrival, shallow-only}; exit: {as-specified, 2R-flat, time-stop-only}) with **no refetch**. The ONLY way to vary the ladder while holding the candidate fixed.
- **Population-monitor baseline.** Roll the same stats over `population_ladders/{date}.parquet` (all verified, incl. never-surfaced rank-4+) and contrast surfaced-minus-population on the selection axis.
- **A 2x2 firebreak diagnostic** computable today: axis A = `sign(market_excess_return)`, axis B = `sign(realized_r)`. Cells: (good pick + banked), (good pick + ladder lost it = **ladder fault**), (bad pick + ladder salvaged), (bad pick + ladder lost = **selection fault**).

**External anchor — multi-benchmark "triad".** Practitioner desks (Ortec, Brinson-adapted) measure ladder quality against three baselines at once, not one: **naive** (next-bar-open→next-bar-close passive execution of the pick), **volatility-adjusted** (TP/SL in ATR units), **optimal** (exit at MFE = the ceiling). Position relative to all three localizes the fault: below naive → selection; beats naive but below vol-adjusted → ladder mis-scaled to volatility; below optimal only → exit-timing inefficiency. Our SPY-excess covers part of "naive"; the vol-adjusted and optimal baselines are the grid-sweep's job (PR-2).

### 4.2 LIMITS (adopted from the critiques — these override the lenses)

1. **The 2x2 is not interpretable at N~30-50.** The actionable off-diagonal cell ("good pick + ladder lost it") is the *smallest* cell; at ~40 matured rows with theme concentration it holds ~2-6 rows. **Fix:** render the 2x2 as **raw counts only**, no within-cell means, until each off-diagonal cell independently clears N≥30. Maturation tracker, not attribution, for the first ~150 pooled outcomes.

2. **`capture_efficiency = realized_r / mfe` is statistically pathological** *(independently flagged by both the repo workflow AND external best-practice).* `mfe → 0` for trades that never moved favorably (precisely the losers) → unbounded/undefined where you most want to read it. `mfe` is a max-over-bars order statistic → grows with hold length and volatility → mechanically confounds capture with hold-length and mcap. Mean-of-ratios ≠ ratio-of-means and diverges at N~40. **Fix:** do NOT report a per-trade ratio. Use the bounded **R-unit difference `capture_gap = mfe − realized_r`**, aggregate as a pooled distribution (median + p10/p90, never mean-of-ratios), gate on `mfe` above a minimum-R threshold. *(External corroboration: winsorize skewed MFE; logit-transform or non-parametric for bounded capture ratios; exclude sub-threshold-volatility periods.)*

3. **The two axes are not orthogonal — the entry ladder double-counts.** `market_excess`/`forward_return` anchor to `reference_close`; `realized_r`/`mfe` anchor to `blended_entry`. A deep entry tier makes `blended_entry << reference_close`, simultaneously raising `realized_r`/`mfe` AND being itself an entry-ladder decision. Not additive. **Fix:** put both axes on **one anchor** — convert `forward_return` to an R-scale via the full-ladder risk anchored to `reference_close` (an arrival-anchored selection ceiling), then define capture relative to THAT. Persist a third explicit **entry-ladder counterfactual** `realized_r` at the already-stored `full_ladder_blended_entry`, so the entry-tier contribution is a separate term, not silently folded into both.

4. **The "selection-free" baseline is NOT selection-free.** `population_ladder_monitor` only replays **verified + plannable** rows — it conditions on the entire LLM-propose → mcap → 3-gate funnel; it is free only of the final rank≤3 cap. rank is assigned by `layer4_weighted_score`, so surfaced-minus-population is a **score-confounded observational contrast**, not a counterfactual. **Fix:** rename "verified-pool baseline (conditional on funnel)"; mark deltas descriptive-only; it becomes a real counterfactual only after π is logged at the cap (GO-gated).

5. **The capture axis drops the most important ladder failure.** `realized_r` is NULL for NO_FILL, so "never got in" is invisible to the capture axis; the selection sample is strictly larger than the capture sample. **Fix:** make "entry-miss" an explicit category and gate the two axes on SEPARATE N counts; the capture gate lags the selection gate by the no-fill rate.

6. **Grid-sweep traps.** All R is gross/pre-cost, so a gross sweep systematically prefers over-trading configs (more fills/tranches = more gross AND more uncaptured cost; cost is mcap-correlated). And best-of-K per decision is an in-sample max. **Fix:** uniform per-mcap-bucket cost haircut before any cross-config comparison; never report best-of-K as achievable; select the alternate config on a train window, report on a disjoint validation window.

**Cross-source note on attribution math:** the Brinson-Hood-Beebower decomposition (allocation × selection × interaction) maps onto a single trade as selection-effect (signal-return − benchmark) + execution-effect (actual − naive ladder) + interaction. External practice reports selection ≈ 60-70% of P&L variance, trade-management the rest, shifting hard by regime. We treat that only as a sanity prior, never a target — the project doctrine is "literature ≠ oracle".

---

## 5. Confounders & what makes a result actionable vs descriptive-only

- **Funnel-not-rank-top-N (structural, not sample-size).** Never-proposed names have π = exactly 0. Positivity fails. Only the conditional-support estimand (Variant A) is identifiable. **No EDGE data answers "should the LLM have proposed X".** A downstream builder must not regress outcome on covariates and implicitly claim a full-universe selection edge.
- **Theme concentration is the dominant finite-sample confounder.** Cap=3/theme, a few themes/day; at N~40 one hot theme can supply most rows AND their market-excess. `theme` IS on the outcome row, so it is measurable — but `summary.py` pools with NO concentration guard. **Report effective-N / theme-Herfindahl next to every pooled mean.**
- **Regime (RECOVERABLE — corrected).** FRED VIXCLS has full daily history; backfill `regime_at_brief` from `brief_date` (look-ahead-free). BUT even with the label, at N~30-50 the sample sits in one or two VIX buckets, so a regime split stays hidden until each cell clears N≥30 — many months out.
- **Mcap/liquidity confounds the LADDER, not just selection.** Small-caps gap → more NO_FILL, BAD_GEOMETRY, `ambiguous_bars`. `market_cap` is on the (mutable) Brief, not the outcome row. Carry a **daily** ADV proxy (reconstructable from Polygon grouped-daily history — NOT "near-free minute liquidity", which the cheap-path majority never fetches). *(External: ATR-normalize MFE/MAE at entry so cross-ticker excursions are comparable — a $2 move means different things in a quiet vs volatile name.)*
- **Maturation length-bias.** The 42-session hold means the first matured rows are enriched for fast resolvers (early SL/TP). The early pooled mean is a censored, length-biased sample. **Expose `n_matured_fast` vs `n_matured_slow`** (derivable from `holding_days_elapsed` + `position_ttl_days`).
- **Multiplicity.** A per-cell N≥30 gate is NOT family-wise correction. Slicing ~40 shared outcomes by theme × regime × mcap is dozens of post-hoc hypotheses on one sample. **Pre-register slice axes against a frozen funnel; apply Holm-Bonferroni/FDR within strata; increment program-level Bonferroni per declared cell.** "Descriptive-only" labels do not neutralize multiplicity for a real-money group reading a fluke cell.
- **Benchmark choice.** SPY-excess on a small/mid-cap thematic book leaves size/sector beta in the residual. **Keep SPY-excess frozen as the accruing headline** for this cohort (swapping mid-capture fragments the distribution and resets the N clock); route universe-cyclicality through the existing `classify_cyclicality_excess` pre-screen; treat IWM/sector-relative as a SEPARATE pre-registered estimand on a fresh cohort.
- **Cost.** All R is gross/pre-cost by design (`gross_of_cost=True`), mcap-correlated. Every actionable claim carries the caveat; a static per-mcap-bucket bps haircut is enough for descriptive net-adjustment.

**Actionable vs descriptive bright line:** a result is **descriptive-only** until (1) pre-registered against a frozen funnel, (2) its cell independently clears N≥30 (off-diagonal/slice) or pooled ≥150, (3) family-wise correction applied, (4) a single anchor + explicit entry-miss category + cost haircut in place. Everything stays **telemetry-only** regardless — no self-driving re-weight.

---

## 6. Data gaps

| Datum | Have today? | Urgency | Where to change |
|---|---|---|---|
| 3 measure layers (substrate / realized_r / ratchet) | Yes | — | `feedback/ladder_replay.py`, `edge.LadderOutcome` |
| Selection covariates (score, gates, llm_confidence, catalyst, mcap) | Yes, on **Brief** (mutable, hard-deleted at `parquet.py:254`); also append-only `thematic_briefs/*.parquet` | **can-add-later** (recoverable; denormalize for durability) | `edge/models.py` + nightly ingest mirror (same pattern as `theme`) |
| **Gate-verdict REASONS** (`net=$31k < $50k floor`, not just name `insider`) | **No** — computed in `verify_candidate`, discarded; re-run hits churned SEC/mcap data | **log-now-or-lose-it** | `thematic/mapping/orchestrator.py` `verify_candidate` + new proposals store |
| **Per-decision K-config ladder grid** (`grid_realized_r_json`) | **No** — only the single ratchet contrast | **log-now-or-lose-it** (cached minute bars are evicted) | `feedback/ladder_replay.py` (`replay_ladder_grid`) + `population_ladder_monitor.py` + column |
| **Entry-ladder counterfactual** `realized_r_full_fill` | **No** — counterfactual entry price stored, realized_r under it not | **log-now-or-lose-it** (bar-eviction) | `ladder_replay.py` + monitor + column |
| **`ladder_config_version`** (TIME_STOP_DAYS, order-TTL used, ratchet/tiebreak rule id, VWAP window) | **No** — replay re-derives semantics from CURRENT code → silent retro-poison; divergence already LIVE (10 vs 7) | **log-now-or-lose-it** | `edge/models.py` + stamp at resolve; fix the 10-vs-7 single-source + parity test |
| Pre-gate proposal set (rejected names + LLM confidence) | **No** — only aggregate counts dropped on ingest | **log-now-or-lose-it** (for future OPE) | `thematic/mapping/orchestrator.py` proposals parquet + PG table |
| Selection propensity **π** at the diversity cap | **No** — cap deterministic; rank-4+ π=0 | **server-side logging safe now; user-facing sampler BLOCKED-pending-GO** | `thematic/mapping/orchestrator.py` (NOT argumentation) — §8 |
| `regime_at_brief` (VIX bucket) | **No** column, **RECOVERABLE** from FRED | **can-add-later** (backfill; defer split until N≥30/cell) | `edge/models.py` + one-shot backfill via `FREDClient.fetch_series` |
| `market_cap` + daily ADV proxy on outcome row | mcap on Brief; ADV from Polygon grouped-daily | **can-add-later** | `population_ladder_monitor.py` + columns |
| Briefs-parquet retention guard / soft-delete | **No** — hard-delete on missing parquet | **log-now-or-lose-it** (source-of-truth protection) | `briefs/ingest/parquet.py:254` + retention note |
| `capture_gap = mfe − realized_r` (bounded) | derivable | can-add-later | `edge/ingest/parquet.py` + column |
| Maturation-speed flag | derivable | can-add-later | `edge/api/summary.py` |
| ATR-at-entry (for cross-ticker MFE/MAE normalization) | **No** | **log-now-or-lose-it** (point-in-time vol) | stamp at resolve in monitor |
| DR/IPW estimator + walk-forward + EB shrinkage | No (designed-only) | can-add-later, **blocked on π GO** | new `apps/alphalens-research/` module |

---

## 7. Instrumentation roadmap (sequenced, PR-sized, TDD, additive)

**"Start logging today" (log-now-or-lose-it; no scorer touched, no user-facing change):**

- **PR-1 — `ladder_config_version` stamp (S).** Small JSON of load-bearing globals (`TIME_STOP_DAYS`, the `order_ttl_days` actually used, ratchet/tiebreak rule ids, VWAP window) onto `LadderOutcome` at resolve. Bundle the fix of the live `_DEFAULT_ORDER_TTL_DAYS=10` (builder) vs `=7` (constants) divergence into one source of truth + parity test. *Test first.*
- **PR-2 — per-decision K-config ladder grid (L).** Generalize the ratchet second pass into `replay_ladder_grid` over a small fixed grid (incl. the vol-adjusted + MFE-optimal baselines) on the SAME cached bars; persist `grid_realized_r_json`. Zero Polygon cost; the only within-decision selection-vs-ladder lever; bars vanish later.
- **PR-3 — entry-ladder counterfactual `realized_r_full_fill` (M).** Re-run the exit ladder at the stored `full_ladder_blended_entry`; difference vs headline = entry-fill drag. Isolates entry-tier-spacing from exit quality.
- **PR-4 — structured gate-verdict reasons (M).** Capture `{gate, threshold, actual_value, pass/fail}` at `verify_candidate` into a durable proposals parquet → PG table.
- **PR-5 — briefs-parquet retention guard (S).** Soft-delete (mark stale) instead of cascade-delete at `parquet.py:254`, or a deploy retention note. Protects the source-of-truth for ALL selection covariates while outcomes mature ~2 months.
- **PR-1b — ATR-at-entry stamp (S).** Point-in-time ATR on the outcome row so MFE/MAE can be ATR-normalized for cross-ticker E-ratio. Cheap; reconstructable-but-fragile, fold into the resolve stamp.

**Durability hardening (recoverable, cheaper to denormalize now):**

- **PR-6 — denormalize selection covariates onto `edge.LadderOutcome` (M).** Mirror `layer4_weighted_score`, `llm_confidence`, `catalyst_strength`, `insider_score_usd`, `gates_passed_str`, `rank_in_day`, `cohort_size_in_day`, `brief_model_used`, `market_cap` at ingest (same pattern that froze `theme`).
- **PR-7 — pre-gate proposal set persistence (M).** Write every proposed `(theme, ticker, llm_confidence)` BEFORE mcap + gates to a proposals parquet → PG table. Unrecoverable if skipped.

**Behind N≥30 / pre-registration (pure compute over captured data):**

- **PR-8 — `regime_at_brief` backfill (M)** via `FREDClient.fetch_series`; defer the split until N≥30/cell.
- **PR-9 — daily ADV/mcap proxy on outcome row (S/M)** from Polygon grouped-daily.
- **PR-10 — `capture_gap` + maturation-speed + theme-Herfindahl in `build_edge_summary` (M).** Descriptive-only, per-cell N-gated, NOT mean-of-ratios.
- **PR-11 — 2x2 attribution as RAW COUNTS (M).** `/v1/edge/attribution` + SvelteKit panel; no within-cell means until off-diagonal cells clear N≥30; single-anchor selection ceiling + explicit entry-miss category.
- **PR-12 — DR/IPW + walk-forward + EB shrinkage (L), BLOCKED on π GO.** Research-tier module; consume only logged π; enforce in-code train/eval/validate splits; emit interval estimates tagged telemetry/exploratory/human-disposes; enforcement test that it never writes a scorer/gate weight.

---

## 8. Open questions / GO-NO-GO items needing the user

1. **GO/NO-GO: ε-injection sampler (real-money intervention, not logging).** **Half A** (lift the verify cap so rank-4+ names get gate-checked and logged server-side) is pure telemetry — safe now. **Half B** (the ε-sampler that *surfaces* rank-4+ names to the WhatsApp group purely to log π) **changes what real-money users see and act on** — human-DISPOSES, needs explicit GO. Without Half B, surfaced-vs-population stays observational and DR/IPW stays under-identified. **Do you authorize Half B?**
2. **Funnel freeze.** The ~3-month horizon assumes the funnel (gates, scorer weights, cap=3, brief_model, SPY benchmark) stays **frozen** over the capture window. Any mid-window change fragments the covariate distribution and compounds Bonferroni. **Confirm a frozen, pre-registered funnel** before the capture clock starts.
3. **Benchmark for the SELECTION axis.** Keep SPY frozen for this cohort and treat IWM/sector-relative as a separate pre-registered estimand — or commit now to a size/sector benchmark before any outcomes mature? (Swapping mid-window resets the N clock.)
4. **Regime source for the project-side label.** FRED-reconstructed VIXCLS is enough. Also want a recomputable SPY/IWM realized-vol bucket as the canonical decision-time label for provenance? Cheap nice-to-have.
5. **`thematic_briefs/` retention.** Any operational job (outside the repo) rotating `~/.alphalens/thematic_briefs/`? If append-forever, PR-5 is defensive; if anything prunes it, PR-5 is urgent. Needs a VPS disk check.
6. **Cost model granularity.** Static per-mcap-bucket bps haircut acceptable for descriptive net-adjustment, or per-name spread data (not currently fetched)?

---

*Doctrine compliance: every recommendation is additive (new columns / modules, no behavior change to selection), telemetry/firebreak-only (no self-driving re-weight), TDD (test-first per PR). The single item that touches live user-facing output — the ε-sampler Half B — is explicitly carved out as GO-gated and NOT in the log-now set.*
