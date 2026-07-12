# theme_mapper grounding-leak A/B — design memo

**Status:** LOCKED (pre-compute) — adversarial review (zen deepseek-v4-pro + Perplexity) pending before the first-look compute.
**Date:** 2026-07-12
**Author:** Kamil Pająk
**Related:** [[reading-quality-eval]] `docs/research/reading_quality_eval_design_2026_07_11.md`; selection-edge diagnosis `docs/research/` (2026-06-23, uniformly-negative BHAR); EDGE signal-attribution July re-run (2026-07-06).

---

## 1. Problem — how it works today

`theme_mapper` (Layer: thematic candidate proposal) is the **one place in the pipeline where an LLM PREDICTS** rather than READS. Confirmed from code:

- The LLM prompt receives **only the theme name** — `<theme>{theme}</theme>` (`apps/alphalens-pipeline/alphalens_pipeline/thematic/mapping/theme_mapper.py:70-123`). No news, filings, holdings, or ticker universe is injected.
- The LLM freely generates **5-15 US tickers + a subjective `confidence`** from its training knowledge (`_MAPPER_RESPONSE_SCHEMA`, lines 42-68).
- The catalyst (news) is resolved **after** proposal (`orchestrator.py:500` catalyst pre-gate vs `512-520` proposal) and is **never passed back to the mapper**. Verification gates (10-K grep, recent press, insider) run **post-hoc**; they can only reject what the LLM already proposed — they cannot add a grounded name the LLM omitted.
- `llm_confidence` orders the `cap=3` / `budget=5` truncation (a selection lever), but EDGE attribution shows it is a **clean null** (no Bonferroni-clear edge; 2026-07-06).

This is the project thesis in miniature: **let the LLM READ (text → verified facts, falsifiable now); let a MECHANICAL rule PREDICT (facts → forecast, forward-only).** `theme_mapper` violates it — the LLM predicts the tickers.

### 1.1 Characterization (measured 2026-07-12, VPS, production 30-day lookback)

For every scored candidate that reached the population-ladder outcome store (`~/.alphalens/population_ladders/`, 577 rows, brief_date 2026-05-19 → 2026-07-11), we reconstructed whether the proposed ticker was actually **named** in the pipeline's own ingested news (`thematic_events.primary_entities`) over the same 30-day catalyst window:

| grounding definition | grounded / 478 with `market_excess` | grounded / 128 matured (terminal) |
|---|---|---|
| **theme-matched** (ticker named in an article tagged with the candidate's *exact* theme) | 15 (~3%) | **0** |
| **any-news** (ticker named in *any* ingested article, theme-agnostic) | 149 (~31%) | 34 |
| **named in ZERO ingested articles** | 329 (~69%) | 94 (~73%) |

**Headline:** `theme_mapper` is **~69-97% free-association** — the majority of its picks appear in **no** article the pipeline ingested, and only ~3% are named in an article carrying their own theme. Theme-vocabulary mismatch is ruled out as the cause: **117 of 119** ladder themes appear in the extractor's tag vocabulary. The near-zero theme-matched grounding is a fact, not a join artifact.

## 2. Goal

Test one falsifiable sub-hypothesis of the leak, and pre-register the powered (forward) version:

> **H1** — Among the candidates `theme_mapper` proposed, those **grounded** in the pipeline's contemporaneous news have a **different** forward `market_excess_return` (vs SPY) than those from **free-association** (named in no news). Direction is *not* pre-supposed — see §4.

If grounded ≫ free → the free-association tail is the drag, and the actionable fix is to require news-grounding for the proposal (a mechanical READING rule replacing the LLM PREDICTION). If grounded ≈ free → the leak is not the free-association tail but the whole selection premise (a valid negative that redirects effort).

**Scope guard:** this memo is telemetry / first-look, **not** a capital-deploy claim and **not** a production change to `theme_mapper`. It pre-registers the measurement; any pipeline change is a separate, later PR gated on a powered verdict.

## 3. Direction is genuinely two-sided (why H1 is a real test)

- **Grounded could be WORSE:** a ticker named in the theme's news = the obvious, attention-heavy beneficiary = more likely already priced. This aligns with the EDGE "late-entry / already-popped" drag (`technical_atr_pct`, `ma50_distance`, press-gate separators; 2026-06-23 + 2026-07-06).
- **Grounded could be BETTER:** a ticker named in a real company-specific catalyst has an actual event driving it, vs a free-associated name riding a vague theme with no news of its own.

Both are plausible; H1 is falsifiable in either direction. This is the value of the test.

## 4. Instruments (grounding definitions)

Three definitions, pre-registered with their retrospective power:

| # | definition | matured N (grounded / free) | retrospective power | role |
|---|---|---|---|---|
| **A** | **theme-matched** — ticker ∈ `primary_entities` of an article tagged the candidate's exact theme, within 30d | 0 / 128 | **dead** (grounded arm empty among matured) | **forward-only** (primary powered instrument) |
| **B** | **any-news** — ticker ∈ `primary_entities` of *any* ingested article within 30d | 34 / 94 | marginal (both arms ≥ 30) | **retro first-look** |
| **C** | **own-catalyst** — ticker ∈ `primary_entities` of the candidate's *assigned* `source_event` (per-candidate, via `source_event_url` → `news_id` → event) | to be computed | unknown | evaluated during compute; report if ≥ 30/arm |

**Pre-registered primary retrospective instrument = B (any-news).** Definition A is the sharp test of the leak thesis but is retrospectively unpowered (the ~15 theme-matched-grounded picks are all recent, still-open positions), so it is deferred to the forward stamp (§7). C is evaluated opportunistically.

### 4.1 Grounding rule (exact, for reproducibility)
For a ladder row `(brief_date D, ticker T, theme Θ)`:
- Load `thematic_events/{d}.parquet` for `d ∈ [D − 30d, D]` (matching production `DEFAULT_LOOKBACK_DAYS = 30`, `catalyst_resolver.py:44`).
- **A grounded** iff `T ∈ ⋃ primary_entities` over rows where `Θ ∈ themes`.
- **B grounded** iff `T ∈ ⋃ primary_entities` over *all* rows (theme-agnostic).
- Tickers uppercased on both sides; join is `events.news_id == news.id` per-date.

## 5. Method — first-look A/B (instrument B)

- **Universe:** the 128 **matured / terminal** rows (real, non-interim outcomes). Ongoing rows carry only interim `market_excess` (biased by holding-period-so-far) and are reported **separately** as supporting, never pooled into the primary.
- **Primary metric:** `market_excess_return` (forward return − SPY window return; pre-computed on the parquet, `benchmark_excess.py:155-205`). No recomputation.
- **Comparison:** mean `market_excess` grounded vs free; report both arm means, the difference, and **Wilson score CIs** on the sign-rate (P(market_excess > 0) per arm) plus a bootstrap CI on the mean difference. Non-parametric (Mann-Whitney) as the headline test — small N, non-normal returns.
- **Multiplicity:** H1 is one new hypothesis on the EDGE selection-attribution program. Report the raw p-value **and** the Bonferroni-adjusted threshold against the running program count (per project ledger discipline). N=34/94 will not clear a strict Bonferroni bar — this is stated up front as a **first-look, not a verdict**.

### 5.1 Confounders (must be reported, not silently dropped)
1. **Recency skew** — grounded picks skew to *later* brief_dates (news-active → recent). Any market-regime trend over 2026-05 → 2026-07 confounds a raw grounded/free split. Mitigation: report the brief_date distribution per arm; add a **date-block-matched** secondary comparison (compare grounded vs free within the same week) and note if the effect survives.
2. **Interim-excess bias** — ongoing rows are not matured; excluded from primary (see universe).
3. **Within-day look-ahead (mild)** — `thematic_events/D.parquet` reflects the *last* run of day D (6×/day overwrite; one `.bak` at 2026-05-30). The grounding label may see intra-day-later news than was available at brief generation. Impact is small (same calendar day) and **inflates** grounding, biasing toward finding grounding *present*; cross-checked against the append-only `thematic_news_lake/` (available 2026-06-04+) where feasible.
4. **Base-rate imbalance** — 27% grounded / 73% free; the free arm dominates and sets the panel mean. Report arm sizes prominently.

## 6. Success / kill criteria (first-look)

This is a **first-look**, so criteria are directional signals for whether to invest in the forward instrument, not accept/reject gates:
- **Signal worth forward-tracking:** |mean market_excess difference| CI excludes 0 at 90% *and* the sign survives date-block matching → build the forward stamp (§7) with priority.
- **Clean null:** difference CI spans 0 and sign flips under date-matching → the free-association tail is *not* the drag; deprioritize; record as a negative in the EDGE attribution ledger.
- Either way, the forward stamp (§7) is still shipped, because instrument A (the sharp test) can only be measured forward.

## 7. Forward instrument (the powered path) — stamp grounding as-of

Because instrument A is retrospectively dead, the only way to test the sharp leak hypothesis with power is to **persist the grounding label at map/score time**, avoiding all reconstruction look-ahead:

- `primary_entities` is extracted but **never propagated** downstream (`orchestrator._build_row()` at `orchestrator.py:269-302` writes `source_event_*` but no entity field; `CatalystPayload` at `catalyst_contract.py:30-45` omits it). The resolver *already computes* the theme-scoped entity set for story-arc overlap (`catalyst_resolver.py:664-717`) — it is discarded.
- **Minimal change:** stamp on the candidate/scored parquet: `grounded_in_theme_news` (bool, instrument A), `grounded_in_any_news` (bool, instrument B), and `grounding_config_version` (poolability key, per the `options_*` / `novelty_*` / expert-panel stamp precedent). Parquet-only SoT; Django ingest may drop them (like `options_*`). No migration.
- **Deploy:** VPS pipeline image rebuild (forward-only). Operator-owned.
- **Verdict horizon:** matured outcomes accrue at the 42-session time-stop (`population_ladder_monitor.py:79`). N ≥ 30 per arm on instrument A is ~**Q4 2026 → early 2027**, same order as the other forward-only EDGE first-looks (buffett/oneil ~2026-09+).

This forward-logging PR **is** the pre-registration of instrument A: the grounding rule (§4.1) and metric (§5) are frozen here; the N ≥ 30 verdict is deferred.

## 8. Feasibility (data audit, 2026-07-12)

All three joins compose over the same dates on the **VPS** (source of truth; the Mac cache lacks the history):
- **Entities:** `~/.alphalens/thematic_events/{date}.parquet` (`primary_entities`, `themes`) — 55 files, 2026-05-18 → 2026-07-11, continuous.
- **News:** `~/.alphalens/thematic_news/{date}.parquet` — 55 files; append-only lake `thematic_news_lake/session_date=…` exists from 2026-06-04.
- **Outcomes:** `~/.alphalens/population_ladders/` (128 terminal / 478 with `market_excess`), brief_date 2026-05-19 → 2026-07-11 → **100% overlap** with entity history.
- **Compute cost:** seconds (parquet reads on VPS). No >1h run; adversarial review is nonetheless run because the *methodology* (grounding definition, confounders, multiplicity) is the risk, not the runtime.

## 9. Test plan

- [ ] Reconstruct grounding labels A/B/C on VPS from the frozen rule (§4.1); assert theme-vocab overlap (117/119) unchanged.
- [ ] Instrument B first-look: arm means + Mann-Whitney + Wilson sign-rate CIs + bootstrap mean-diff CI, matured universe only.
- [ ] Date-block-matched secondary comparison (confounder 1).
- [ ] Ongoing/interim panel reported separately (never pooled).
- [ ] Instrument C computed; reported only if ≥ 30/arm.
- [ ] Results written into §10 of this memo; verdict logged to the EDGE attribution ledger.
- [ ] Forward stamp PR (§7) — separate, TDD, zen pre-merge, VPS image deploy.

**Known gaps / not covered:** N=34/94 is below any strict multiplicity bar (first-look only); interim-excess on ongoing rows is not a matured outcome; within-day overwrite is a mild upward look-ahead on the grounding label (§5.1.3). The sharp instrument A has **no** retrospective power — its verdict is forward-only.

## 10. Adversarial review outcome (2026-07-12) — retro A/B DOWNGRADED

zen (`deepseek-v4-pro` and `gemini-3.1-pro-preview`) both failed on a tool cache bug (embedded unrelated PEAD memos, analyzed the wrong artifact) — not used. The review rests on a Perplexity literature-grounded critique (Sonar Reasoning Pro) + inline analysis. It found the retrospective outcome comparison is **near-uninterpretable**, and two of the flaws are **not fixable retrospectively**:

1. **Confounding — instrument B is not a grounding instrument (fatal to interpretation).** "Named in news" is a bundled proxy for size / dollar-volume / attention / news-sentiment, each of which independently predicts short-horizon returns (NBER w30860; SSRN 4538670 news-disagreement; Kothari-Warner event-study). The exclusion restriction ("news-mention affects 42-day excess return *only* through grounding") is implausible. A grounded-vs-free difference is explainable entirely by the attention/liquidity channel without any LLM-grounding effect. **Carries forward to instrument A.**
2. **Collider bias (not retro-fixable).** Grounding is labelled only on gate-survivors (the pipeline includes a recent-press gate). Conditioning on selection S — a collider driven by both grounding (via the press gate) and latent outcome-relevant traits — opens a spurious grounded↔outcome path even under a true null. Correcting it needs an explicit selection model (Heckman / propensity), which N=34 cannot support.
3. **Matured-only survival bias (not retro-fixable).** If grounded names (higher attention/vol) resolve faster, the 128-matured subset over-represents part of each arm's return distribution. Diagnosable (time-to-resolution + censoring by arm) but not correctable at this N.
4. **Underpower.** N=34/94 on fat-tailed 42d returns (σ≈10-15%) gives ~25-40% power for a plausible d≈0.2-0.3 → winner's-curse risk. Mann-Whitney assumes equal distribution shapes (breaks under unequal variance); use a **permutation test** on the mean/median/sign-rate instead.

### Revised plan (supersedes §5-§7 as the *primary* deliverable order)

- **(D1) Characterization is the deliverable now.** The 69-97% free-association count (§1.1) is a descriptive count with **no outcome comparison**, so it is free of flaws 1-4. It is strong, publishable-internally evidence that the leak (LLM predicting tickers from the theme name) structurally dominates. This stands on its own.
- **(D2) Retro is DOWNGRADED to confounded diagnostics — no outcome verdict.** Report per-arm balance (market cap, dollar-volume, sector, entry-date) + time-to-resolution + censoring (the flaw-2/3 diagnostics), and the raw grounded/free `market_excess` means with a permutation p-value and a size/dollar-volume/sector-matched sensitivity — all explicitly labelled **confounded + collider-biased + underpowered, NOT a verdict**. This tests whether the confounds are even present, it does not test H1.
- **(D3) Forward instrument must stamp the CONFOUND COVARIATES too.** The forward stamp (§7) adds, alongside `grounded_in_{theme,any}_news` + `grounding_config_version`: `mcap_at_pick`, `dollar_volume_20d`, `sector`, `entry_session` — so the forward test can run a size/liquidity/sector-matched permutation comparison. Without these the forward instrument A inherits flaw 1 and is equally uninterpretable.
- **(D4) The genuinely clean leak test is a forward mechanical-rule head-to-head**, not grounded-vs-free within the LLM's own picks: generate a mechanical news-grounded candidate set in parallel with the LLM's free-association, size/liquidity-match, and compare forward `market_excess`. This removes the collider (both candidate sources evaluated on the same downstream gates) and directly tests "does READING beat PREDICTING". Larger, forward-only; pre-registered as the follow-on to D3.

## 11. Results (2026-07-12, VPS)

### D1 — Characterization (robust; no outcome comparison, free of the §10 flaws)
- **any-news grounding** (L=30): 149/478 (31%) of all scored picks; 34/128 (27%) of matured. → **69-73% free-association.**
- **theme-matched grounding**: 15/478 total, **0/128 matured** → retrospectively dead (deferred to forward stamp A).
- Theme-vocabulary overlap 117/119 → the near-zero theme-matched grounding is a fact, not a join artifact.
- **Headline: `theme_mapper` is 69-97% free-association** (69% named in no article at all; 97% not named in an article carrying their own theme). Strong, confound-free evidence the predicting-leak structurally dominates.

### D2 — Confounded diagnostics (NOT a verdict on H1)

**Confound present (as the review predicted):** matured dollar-volume median **grounded 10^8.19 ≈ $155M/day** vs **free 10^7.97 ≈ $93M/day**, with the free arm's lower quartile far smaller (10^7.51 ≈ $32M) — grounded = news-active = larger/more-liquid names. Recency (both arms' median brief_date 2026-06-07) and time-to-resolution (both median 10.5 sessions) are **balanced** — confounders 1 (recency) and 3 (survival) are weaker than feared; the size/liquidity confound is the live one.

**Raw outcome (confounded + collider-biased + underpowered):** grounded mean `market_excess` −0.049 (median −0.075, 47% positive), free +0.044 (median +0.065, 74% positive), difference −0.093, permutation p = 0.004. Read naively this says "grounded picks fade" — but:

**Dollar-volume-matched sensitivity — the grounded/free sign FLIPS across liquidity terciles:**

| dv tercile | grounded mean (n) | free mean (n) | diff |
|---|---|---|---|
| low | +0.176 (5) | +0.071 (38) | **+0.105** |
| mid | −0.016 (12) | +0.025 (30) | −0.041 |
| high | −0.139 (17) | +0.026 (26) | **−0.164** |

The pooled −0.093 is a **Simpson's-paradox artifact**: grounded names concentrate in the high-dv tercile (17/34) where the fade is deepest, while free names skew low-dv (38/94). Once liquidity is held fixed, the grounding effect is **unstable — it reverses sign** (grounded better among low-dv, worse among high-dv). This **confirms §10 flaw 1**: instrument B measures the size/liquidity/attention channel, not grounding. The retrospective outcome comparison is **not interpretable as a grounding effect** and is not carried forward as evidence for or against H1.

### Verdict
- **D1 stands** as the deliverable: the leak (LLM predicting tickers from the theme name) is real and dominant (69-97% free-association).
- **D2 confirms the retro A/B is uninterpretable** (liquidity confound flips the sign under matching; collider + underpower on top). H1 gets **no retrospective verdict** — as pre-registered.
- **Next: D3** — forward stamp (`grounded_in_{theme,any}_news`, `grounding_config_version`, **plus** `mcap_at_pick` / `dollar_volume_20d` / `sector` / `entry_session` confound covariates) so instrument A can be tested forward with a size/liquidity/sector-matched design (~Q4 2026+). D4 (mechanical-rule head-to-head) remains the ultimately clean, collider-free test.
- **Note for D3:** the matched design is mandatory, not optional — an unmatched forward grounded/free comparison would reproduce exactly this Simpson artifact.

### 11.1 D3 scope re-evaluation (post code-path map, 2026-07-12)

Mapping the exact propagation path (`catalyst_resolver.find_trigger_event` → `CatalystPayload` → `orchestrator._build_row` → scorer → brief) plus a persistence audit changed the D3 value estimate:

- **The confound covariates and instrument B are RECONSTRUCTABLE forward — they do not need stamping.** `market_cap` + `sector_name` are already on the scored/brief row; dollar-volume rebuilds from the persistent per-date `grouped_daily_history` store (as D2 did); `entry_session` is deterministic from `brief_date` + the exchange calendar; **any-news grounding (B)** is a theme-agnostic union over the persistent `thematic_events` files and does not depend on resolver internals. So a forward matched analysis needs **no new stamp** for any of these.
- **Only theme-matched grounding (instrument A) is as-of-critical and not cleanly reconstructable** — it depends on the resolver's exact production window + its noise / state-media / dedup filters (`catalyst_noise_filters.yaml`), which a naive reconstruction union would not reproduce.
- **But instrument A has a ~3% base rate** (D1: 15/478 total, **0/128 matured**). Forward, at ~2 matured outcomes/day × 3% ≈ 0.06 grounded-matured/day, **N ≥ 30 in the grounded arm is ~1.5 years out.** Instrument A is the clean test but its forward verdict is far away.

**Implication:** D3 (stamping grounding on the LLM's own picks) has **low near-term value** — instrument A is ~1.5 yr from power; instrument B is confounded *and* reconstructable without a stamp. The genuinely clean, better-powered forward test is **D4 — the mechanical-rule head-to-head**: a mechanical news-grounded generator proposes candidates in parallel with the LLM's free-association; the grounded arm is then "however many the rule generates" (not 3% of LLM picks → power in months, not years), and evaluating both sources through the *same* downstream gates removes the collider. D3 remains valid only as cheap long-horizon insurance for the production-exact instrument-A label; it is **not** the powered path.
