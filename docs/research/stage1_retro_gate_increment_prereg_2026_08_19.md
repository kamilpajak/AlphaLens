# Stage-1 retro gate-increment study — pre-registration

**Status:** LOCKED
**Date:** 2026-08-19
**Author:** Kamil Pajak
**Branch:** `research/stage1-retro-gate-increment`
**Evidence tier:** quasi-holdout / prior-adjusting — explicitly BELOW confirmatory. The
ISO week 40-42 forward ledger remains the ONLY confirmatory track for the Stage-1
gate. Nothing in this study can substitute for it, and its prompt freeze is untouched.

---

## 1. Question

Does the frozen production Stage-1 event-conditioned theme mapper's refusal behavior
sort matured market-excess returns among old-cohort brief survivors? Concretely: on
the CLEAN window (defined in §3), is the pair-cluster mean matured
`market_excess_return` of KEPT themes higher than that of REFUSED themes?

This measures the gate's *precision increment on survivors of the old pipeline* —
one-sided by construction. It cannot measure recall (tickers/themes the new mapper
would surface that the old pipeline never briefed), the new pipeline's standalone
forward performance, or the truthfulness of any `transmission_channel` text.

## 2. Frozen instrument identity

The replay runs the FROZEN production Stage-1 mapper prompt — verbatim, no rubric
tuning, no re-wording, ever (anchor failure HALTs; it never iterates, per the
role-classifier prompt-echo lesson).

- **Prompt sha:** `52b12550f344` (first 12 hex chars of sha256 of
  `theme_mapper._PROMPT_TEMPLATE`, computed from this branch's checkout at commit
  time).
- **Full `mapper_config_version` (the replay MUST assert equality against this exact
  string before any labeling call):**

```json
{"block_tag":"untrusted_event","field_constants":{"entities_max":10,"field_max_chars":80,"headline_max_chars":200,"implication_max_chars":240,"implications_max":5,"unavailable":"(none)"},"max_candidates":15,"max_output_tokens":8000,"mcap_range":[500000000,10000000000],"model":"deepseek/deepseek-v4-pro","prompt_sha":"52b12550f344","schema":"mapper-freeze-v2","schema_sha":"ec5d56e9d13a","temperature":0.0}
```

- sha256 of the string above: `54704ae415f8e4e1fcc8669b0928fb2803354d90d3fc53489167fb9f1c54d263`.
- **Production cross-check (performed 2026-08-19, read-only on the VPS):** every
  brief parquet row for brief dates 2026-08-03..2026-08-18 (16 day-files under
  `~/.alphalens/thematic_briefs/`) carries exactly ONE distinct
  `mapper_config_version`, byte-identical to the string above. The deployed
  post-08-03 instrument and this pre-registration pin the same prompt.
- Rendering note: `docs/research/golden_map_rebaseline_2026_08_03.md` records a
  2026-08-03-era rendering of the version string without the `field_constants`
  object. The stamped production rows do NOT show that older rendering; the
  authoritative identity for this study is the stamped-row string quoted above.
  `prompt_sha=52b12550f344` and `schema_sha=ec5d56e9d13a` are invariant across both
  renderings.
- Model: `deepseek/deepseek-v4-pro` via the canonical `OpenRouterClient`,
  temperature 0.0, `max_output_tokens` 8000, JSON response schema — all fingerprinted
  inside the version string above.

## 3. Cohort and windows

- **Cohort:** old-cohort brief rows, brief dates 2026-05-19..2026-08-01 inclusive
  (joinable to a stored extracted event AND carrying non-null matured
  `market_excess_return`; ~675 rows at design time). Join path: brief
  `source_event_url` → `thematic_news.url` → `id` → `thematic_events.news_id`.
- **CLEAN window (verdict-bearing for the single primary hypothesis):**
  2026-05-19..2026-06-18 MINUS the enumerated exclusion days below.
- **DEV window (exploratory only, forever):** 2026-06-19..2026-08-01. This window is
  development data for the Stage-1 rule (the 45-day role post-mortem spans
  2026-06-19..2026-08-02; the LYFT trigger card is 2026-08-02). No hypothesis slot,
  no verdict vocabulary — descriptives only, labeled as unvalidated per the
  unvalidated-display doctrine.

### 3.1 Enumerated CLEAN-window exclusion days (final, closed list)

Excluded because they were individually cited/inspected in Stage-1-lineage material
during the rule's development:

| Day | Reason | Citing artifact |
|---|---|---|
| 2026-05-24 | Benzinga round-up day cited in the EPIC #974 diagnosis; also the golden map-themes fixture asof (`quantum_computing @ 2026-05-24`; MANH/DFIN/QLYS/QUBT cassettes, `_ASOF = 2026-05-24`) used to develop and re-baseline the Stage-1 prompt | EPIC #974 diagnosis; `docs/research/golden_map_rebaseline_2026_08_03.md`; `docs/research/golden_map_ising_fixture_2026_08_03.md`; `docs/research/reading_quality_eval_design_2026_07_11.md` |
| 2026-06-16 | Individually inspected during catalyst-selection lineage work: `social_media_regulation` catalyst loss, SNAP + MITK named and examined row-level | `docs/research/catalyst_noise_discriminator_design_2026_06_18.md` |
| 2026-06-17 | Same citation ("2026-06-16/17"), same memo | `docs/research/catalyst_noise_discriminator_design_2026_06_18.md` |

**Enumeration procedure (auditable):** all Stage-1-lineage artifacts were grepped for
specific dates in 2026-05-19..2026-06-18: `theme_mapper_grounding_leak_design_2026_07_12.md`,
`theme_mapper_mechanical_rule_headtohead_design_2026_07_12.md`,
`catalyst_confidence_source_bias_2026_07_16.md`,
`catalyst_noise_discriminator_design_2026_06_18.md`,
`golden_map_rebaseline_2026_08_03.md`, `golden_map_ising_fixture_2026_08_03.md`,
`reading_quality_eval_design_2026_07_11.md`,
`apps/alphalens-research/scripts/classify_catalyst_roles.py` (anchors span
2026-06-19..2026-08-02 — entirely DEV),
`apps/alphalens-research/scripts/replay_nvda_qubt.py` (asof 2026-04-14 — outside
cohort), `apps/alphalens-research/scripts/reclassify_probe.py`.

**Aggregate-span mentions are NOT day-level exclusions** (they are covered by the
quasi-holdout classification in §9, not by deletion): 2026-05-19 (store span start in
the grounding-leak and head-to-head memos), 2026-05-30 (a `.bak` file artifact note),
2026-06-04 (news-lake availability start), 2026-06-07 (a median-brief-date statistic),
2026-06-10 (an H=21 window-fit bound), 2026-06-15 (start of the confidence-bias
measurement window). Excluding every day merely contained in an aggregate scan would
erase the entire CLEAN window — that leakage is program-level and irreducible, which
is exactly why this study's ceiling is quasi-holdout (§9), not why individual days
disappear.

This list is CLOSED at pre-registration. No day may be added or removed after the
first labeling call.

## 4. Unit of inference and label taxonomy

- **Unit:** the (theme, source_event) pair — identified as (theme,
  `source_event_url`). Rows sharing a catalyst across days share a pair by
  construction; outcomes aggregate to pair-cluster means. (~230 pairs full cohort,
  ~80-110 in CLEAN, at design time.)
- **Three-level label per pair (k=5 majority, §5):**
  - `THEME_REFUSED` — the mapper refuses the theme for this catalyst (no candidates).
  - `KEPT_TICKER_PROPOSED` — theme kept AND the old brief row's ticker is among the
    proposed candidates.
  - `KEPT_TICKER_ABSENT` — theme kept but the old ticker is not proposed
    (no-channel-for-this-name or large-cap crowd-out; disambiguated descriptively).
- **Primary contrast is at THEME level** (KEPT = `KEPT_TICKER_PROPOSED` ∪
  `KEPT_TICKER_ABSENT` vs `THEME_REFUSED`), because ticker-level absence conflates
  no-channel with large-cap crowd-out. The ticker-level split is a descriptive
  secondary with crowd-out reported separately; it carries no hypothesis slot.

## 5. Labeling procedure

- **Input construction:** `CatalystPayload` is built from the brief row's STORED
  stamped catalyst fields (`source_event_url`, title, `published_at`,
  `catalyst_event_type`, `catalyst_confidence`, `catalyst_template_facts_json`, …)
  joined to the stored `thematic_events`/`thematic_news` parquets. Today's
  `catalyst_resolver` is NEVER re-run for a past asof (resolver drift — the #634
  state-media gate, template precedence — could select a different catalyst than the
  one production stamped).
- **Calls:** `theme_mapper.propose_candidates(theme, catalyst)` with the frozen
  prompt (§2), k=5 independent calls per pair; the pair label is the majority; the
  per-pair vote split (flip rate) is recorded and reported.
- **Concurrency:** max 3 threads, exponential backoff on transient errors. (The
  role-classifier run showed 10 threads drove the empty-response rate 25%→57%.)
- **Empty LLM response = FAILURE**, never a refusal label: retry with backoff; a pair
  that cannot complete k=5 valid responses is reported as UNLABELED, excluded from
  the contrast, and counted in the attrition table (#982 lesson).
- **Blinding:** the labeling code is FORBIDDEN to read `~/.alphalens/population_ladders/`
  (any path containing `population_ladders`). This is pinned by a unittest committed
  with the labeling script, before any outcome join exists.
- **Provenance per call:** provider, `served_model`, `generation_id`, timestamp —
  logged to the label parquet sidecar.

## 6. Pinned-provider procedure (replay environment ONLY)

- The replay environment sets `ALPHALENS_OPENROUTER_PROVIDER_ORDER=<one provider>`
  and leaves `ALPHALENS_OPENROUTER_ALLOW_FALLBACKS` unset ⇒ `allow_fallbacks: false`
  (hard pin; unreachable provider errors loudly rather than silently rerouting).
  Production env is untouched; the production fp8-only quantization pin is
  insufficient for measurement (routing rotates within fp8).
- The concrete provider is CHOSEN in Phase 0 (first reachable fp8 provider serving
  `deepseek/deepseek-v4-pro` with `require_parameters` honored) and RECORDED BY
  AMENDMENT to this memo — a docs-only commit appending the provider name to §11 —
  BEFORE the first Phase-1 labeling call. The choice of provider is an
  instrument-qualification decision, not a degree of freedom over results: it is made
  strictly before any label exists.

## 7. Phase 0 — instrument qualification and HALT gates

All gates are pass/HALT. A HALT stops the study before Phase 1; the only permitted
continuation after a HALT is a re-scope to instrument-qualification + power-planning
deliverables (no hypothesis test, the Bonferroni slot is returned/not consumed).

1. **Training-cutoff verification:** verify DeepSeek v4's documented training cutoff
   (expected ≤ early 2026; the pipeline swapped to v4 on 2026-05-30) and record it by
   amendment. If the documented cutoff overlaps the cohort window, HALT.
2. **Provider pin recorded** (§6) before any labeling call.
3. **Flip-rate stability gate:** 20 pilot (theme, source_event) pairs × k=10, split
   as two independent k=5 batches. Require the two batch-level majority labels to
   agree on ≥90% of pairs (≥18/20). Below 90% ⇒ HALT (instrument unfit, per the
   2026-08-05 single-provider doctrine).
4. **Memorization probe:** ask the pinned model for the realized post-event price
   direction/outcome of 10 CLEAN-window cohort events (sampled preferentially near
   the May release boundary). ANY demonstrated outcome recall ⇒ HALT.
5. **Anchor gate (sanity, not evidence):** under the pinned provider, the LYFT
   2026-08-02 harassment card and the `us_ukraine_relations` theme card must both be
   labeled `THEME_REFUSED` by k=5 majority. Any anchor failure ⇒ HALT. Anchors are
   never used to tune anything: the rubric is the production prompt verbatim, and a
   failed anchor stops the study rather than triggering iteration.

Pilot pairs and probe events are drawn and recorded (by amendment, list of pair ids)
before the pilot calls run. Pilot calls count as instrument qualification, not
labeling; the 20 pilot pairs keep their eventual Phase-1 labels from the main pass.

## 8. Single primary hypothesis and statistical procedure

- **H1 (one-sided, the ONLY verdict-bearing test):** on the CLEAN window, the
  pair-cluster mean matured `market_excess_return` of KEPT themes exceeds that of
  REFUSED themes: mean(KEPT) − mean(REFUSED) > 0.
- **Outcome:** stored `market_excess_return` from the population-ladder store, joined
  only AFTER the label parquet hash is committed (§10). Winsorized at 1% per leg
  (each leg winsorized separately; both legs also reported unwinsorized).
- **Inference:** two-way cluster bootstrap over (pair × brief-day) — resample pairs
  and brief days; report the bootstrap one-sided p, the point estimate, and both leg
  means with cluster counts. α = 0.05 one-sided against the single-slot-corrected
  program threshold per the ledger entry (§8.1).
- **Power statement:** ~40-60 pairs per leg in CLEAN supports only a coarse effect. A
  null result is "not a large effect", never "gate worthless". A null or inverted
  result is logged in the ledger and CANNOT be re-tried with a tweaked rubric or
  re-cut windows.
- **Everything else is descriptive, no slots:** DEV-window contrast, ticker-level
  secondary, crowd-out quantification, refusal-reason taxonomy, fabricated-channel
  incidence, flip-rate distributions, refusal-rate estimates.

### 8.1 Bonferroni ledger entry (one slot)

- Entry: `stage1_retro_gate_increment_clean_kept_minus_refused_2026_08_19`
- Family: program-level ledger (the same family that prices every AlphaLens
  hypothesis). Exactly ONE new slot is consumed by H1. The slot is registered at this
  memo's commit; it is consumed when the outcome join runs; it is returned only by a
  Phase-0 HALT (no label→outcome contact ever occurred).

## 9. Evidential status — explicit statement

This study is **quasi-holdout, prior-adjusting evidence — not confirmation.**

- CHANNEL 1 (model knows outcomes) is bounded by timeline (v4's checkpoint predates
  the cohort; verified in Phase 0), the hard provider pin, per-call provenance, and
  the memorization probe.
- CHANNEL 2 (rule designed on this history) is bounded by SPLITTING: DEV is
  development data forever; CLEAN was never quantitatively inspected for channel
  labels and its individually-inspected days are enumerated and excluded (§3.1).
- What splitting CANNOT remove: program-level leakage — the decision that the mapper
  needed an event-conditioned gate was itself reached by observing this cohort's
  disappointing aggregate profile (including the pre-07-12 grounding-leak scan, whose
  outcome-joined characterization spans brief dates 2026-05-19..2026-07-11, i.e. the
  whole CLEAN window in aggregate). Therefore the CLEAN result adjusts priors and can
  justify committing a pre-registered forward-window extension; it can never confirm
  the gate. **The ISO 40-42 forward ledger stays the only confirmatory track**, and
  any forward-window extension (e.g. ISO 40-46) must be pre-registered before any
  forward outcome matures (Phase 3 power memo).

## 10. Two-stage commit protocol

1. **Stage A (this commit):** this memo, Status LOCKED, committed and pushed BEFORE
   any label is generated and before any outcome is unblinded. Amendments permitted
   after Stage A and before Phase 1: provider name (§6), documented training cutoff
   (§7.1), pilot pair / probe event id lists (§7) — each a docs-only append, none may
   alter windows, exclusions, taxonomy, hypothesis, or thresholds.
2. **Stage B:** the Phase-1 label parquet is written; its sha256 is committed (memo
   amendment or sibling artifact file) BEFORE any code that joins labels to
   `population_ladders` outcomes runs. The blinding unittest (§5) is part of the
   labeling-script commit.
3. Only after Stage B lands may the outcome join and H1 inference execute.

## 11. Phase-0 amendment record

(Amended 2026-08-19, before any Phase-1 labeling call, per §10 Stage A.)

- **Pinned provider: `Alibaba`** (OpenRouter provider name; endpoint tag
  `alibaba/fp8`, quantization fp8). Chosen as the most-frequently-served fp8
  provider over 6 discovery calls through the canonical `OpenRouterClient`
  with the production fp8 quantization pool and no order pin (Alibaba 4/6,
  DeepInfra 2/6). Pin verified 5/5: with
  `ALPHALENS_OPENROUTER_PROVIDER_ORDER=Alibaba`,
  `ALPHALENS_OPENROUTER_ALLOW_FALLBACKS` unset (⇒ `allow_fallbacks: false`),
  `ALPHALENS_OPENROUTER_QUANTIZATIONS=fp8`, `require_parameters: true`, all 5
  verification calls returned `provider=Alibaba`,
  `served_model=deepseek/deepseek-v4-pro`. Replay environment only.
- **Documented v4 training cutoff + source:** DeepSeek publishes no exact
  knowledge-cutoff date. The binding evidence is checkpoint identity: the
  OpenRouter slug `deepseek/deepseek-v4-pro` is the **V4 Pro 0423
  checkpoint** ("DeepSeek: DeepSeek V4 Pro 0423", created 2026-04-24 per the
  OpenRouter `/api/v1/models` registry and the model page "Release Date:
  April 24, 2026"). A checkpoint released 2026-04-24 cannot contain training
  data past its release, so its cutoff strictly predates the cohort start
  2026-05-19. **Known hazard, bounded:** the GA **V4-Pro-0813** checkpoint
  (released 2026-08-13, training data potentially covering the cohort) is a
  DISTINCT OpenRouter slug (`deepseek/deepseek-v4-pro-0813`), and DeepSeek's
  own first-party API has re-pointed its native `deepseek-v4-pro` name to
  0813 — therefore the pinned provider must never be the first-party
  `DeepSeek` proxy. The pin (Alibaba, an open-weights hoster serving the
  0423-slug weights) plus per-call `served_model` logging plus the §7.4
  memorization probe bound this substitution risk.
- **Pilot pair ids (20; seed 20260819, 10 CLEAN + 10 DEV, no straddles):**
  CLEAN — `Artificial Intelligence|gadget.co.za/sasagenticai38f`,
  `ai_inference_hardware|macdailynews.com/2026/06/04/apple-finally-set-to-launch-all-new-siri…`,
  `consumer_electronics|techcrunch.com/2026/05/30/meta-is-reportedly-developing-an-ai-pendant`,
  `defense_procurement|attackofthefanboy.com/politics/us-will-need-years-to-replenish-stockpiles…`,
  `fda_approval|benzinga.com/…/vertex-says-pediatric-casgevy-data…`,
  `fintech|digitaltrends.com/computing/canva-adds-new-editing-tools-payments…`,
  `investment strategy|fool.com/investing/2026/05/25/billionaire-stanley-druckenmiller-just-dumped-alph`,
  `ios|techcrunch.com/2026/06/09/wwdc-2026-everything-announced…`,
  `passive_investing|benzinga.com/…/the-voo-and-chill-economy-is-now-worth-a-historic-1-trillion`,
  `quantum_computing|finance.yahoo.com/…/globalfoundries-gfs-launches-dedicated-quantum-200944118`;
  DEV — `augmented_reality|seekingalpha.com/news/4605204-snap-gets-a-reality-check…`,
  `bank_earnings|ground.news/article/how-major-us-stock-indexes-fared-tuesday-7-14-2026`,
  `dividend_etf|fool.com/coverage/etfs/2026/06/22/dividend-etfs-how-schd-and-fdvv-measure-up`,
  `dividend_king|fool.com/investing/2026/07/30/coca-cola-just-hit-an-all-time-high…`,
  `government_investigation|thesun.ng/tinubu-orders-fccpc-probe-of-big-tech…`,
  `investment_banking|investing.com/analysis/sp-500-rally-tests-whether-softer-inflation…200683900`,
  `precision_medicine|sec.gov/Archives/edgar/data/1217234/000121723426000038/…index.htm`,
  `press_freedom|yahoo.com/news/politics/articles/trump-t-stop-white-house-162003161`,
  `s_p_500|investing.com/analysis/factories-hold-their-ground…200683135`,
  `subpoena|military.com/justice-department-subpoenas-reporters-air-force-one-security`.
  Full exact `pair_id` strings in the study scratch artifact
  `phase0_selection.json` (seeded draw, reproducible).
- **Memorization-probe event ids (10; earliest CLEAN pairs, May-boundary
  weighted):** 2026-05-19 AI (`Artificial Intelligence|marketwatch.com/…anthropics-latest-hire…`),
  2026-05-19 QS (`Electric Vehicles|finance.sina.com.cn/…doc-inhymysz9850678`),
  2026-05-19 SOUN (`artificial_intelligence|techcrunch.com/2026/05/19/how-to-use-googles-new-ai-agents…`),
  2026-05-19 MP (`geopolitics|bignewsnetwork.com/…putin-visits-china…`),
  2026-05-20 BAH (`geopolitics|ft.com/content/c4dc62eb-2dc0-47b3-bcd5-0c4495872783`),
  2026-05-20 RDW (`space exploration|ft.com/content/c4dc62eb…`),
  2026-05-21 SYM (`AI|vietnamnet.vn/…google-deepmind-and-qualcomm-leaders…`),
  2026-05-21 AI (`Artificial Intelligence|gadget.co.za/sasagenticai38f`),
  2026-05-21 WK (`IPO|marketwatch.com/…spacex-has-a-lot-riding…`),
  2026-05-21 EXPO (`consumer warning|ktar.com/…newsoms-office-warns-californians-to-avoid…`).
- **Anchor payload identities (stored events, recovered read-only from the
  VPS stores):** (a) `harassment` × the eBay harassment-settlement event —
  news_id `1286800032aaad71`, stamped title "eBay pays $46M to journalists it
  targeted in bizarre harassment campaign", published 2026-07-28, event_type
  `settlement`, confidence 0.95, primary_entities [EBAY], no implications;
  (b) `us_ukraine_relations` × the Patriot-deal event — news_id
  `8bb2a99ed083524e`, title "Trump frânează acordul cu Ucraina pentru
  fabricarea rachetelor Patriot, invocând riscuri legate de tehnologia
  militară americană", published 2026-08-01, event_type `geopolitical`,
  confidence 0.8, primary_entities [RTX], one stored implication. These are
  the two events the deployed prompt declined in the first post-deploy run
  (asof 2026-08-02, 21:04 UTC; head-to-head memo §13.7).
- **Anchor call results (2026-08-19, pinned provider, k=5 each):** both
  anchors returned `THEME_REFUSED` 5/5 (unanimous, exceeding the k=5-majority
  bar). `harassment` × eBay settlement — all five decline reasons cite the
  one-time-settlement / no-beneficiary logic; `us_ukraine_relations` ×
  Patriot-deal delay — all five decline an adverse event rather than propose
  its victim as a long. No prompt or rubric was touched.
- **Flip-rate gate result:** 20 pilot pairs × k=10 (two independent k=5
  batches), 200/200 calls valid (0 unresolved failures, 0 empty-payload
  retries needed), all 200 served by `Alibaba` /
  `served_model=deepseek/deepseek-v4-pro`. Batch-majority agreement 19/20 =
  95% (both at 3-level and THEME level) ≥ the pre-registered 90% bar. Mean
  per-pair 10-vote label entropy 0.298 bits; 13/20 pairs unanimous 10/10.
  The one disagreeing pair (`consumer_electronics` × Meta AI-pendant rumor)
  split exactly 5/5 REFUSED vs KEPT_TICKER_ABSENT.
- **Memorization-probe result:** 10/10 CLEAN-window events (2026-05-19..21,
  May release-boundary weighted): the pinned model answered "I do not have
  post-event price information" on every probe and stated it does not
  recognize any of the events; zero direction/magnitude/date recall.
  Phase 0 verdict: ALL GATES PASS — Phase 1 may proceed.
- Label parquet sha256 (Stage B): —

## 12. Deliverables

1. Label parquet + provenance sidecar (hash-committed per §10).
2. H1 result on CLEAN (single ledger verdict) + descriptive report (DEV, secondary,
   taxonomies) with no verdict vocabulary outside H1.
3. Phase-3 power memo for ISO 40-42 using the measured refusal rate and CLEAN spread;
   if power < 0.8 at the pre-registered effect size, pre-register a forward-window
   extension before any forward outcome matures.

## 13. Cost and runtime envelope

Pilot 200 calls + probes + main pass ~230 pairs × k=5 ≈ 1,150 calls at ~2.4k in /
~1k out tokens on `deepseek/deepseek-v4-pro` ⇒ <$30 total. ~2-4h of API calls at 3
threads with backoff; ~1 working day end-to-end. All inputs already on the VPS under
`~/.alphalens/{thematic_news,thematic_events,thematic_briefs,population_ladders}/`
(read-only; replay compute runs off-VPS against synced copies).

## 14. Rejected alternatives (summary — full rationale in the study plan)

- Treating the June-July replay as confirmatory or an ISO 40-42 substitute (HARKing —
  DEV is the rule's development data).
- Re-running the full historical pipeline (post-mapper gates are PIT-dirty: current
  yfinance mcap, live 10-K, live press).
- Re-resolving catalysts with today's `catalyst_resolver` (resolver drift changes the
  mapper's input silently).
- Entity masking / date scrambling as decontamination (60-100% entity reconstruction;
  destroys legitimate priors; unnecessary post-cutoff). Retained only as an optional
  20-pair diagnostic if the memorization probe is ambiguous.
- Two-sided recall arm via synthetic outcomes (different outcome definition; day-1
  gap adverse selection misprices exactly these candidates).
- Relying on the production fp8-only pin for determinism (routing rotates within
  fp8).
- k-repeat voting as a truth/decontamination device (correlated draws; used strictly
  as instrument-noise control with flip rate reported).
- Point-in-time replacement models (validates a different model's gate).
- Extending the replay before 2026-05-18 (no stores exist; re-ingest is not
  PIT-identical).
- Ticker-level primary contrast (conflates no-channel with large-cap crowd-out).
- Tuning the replay prompt/rubric until anchors pass (prompt-echo lesson; anchors
  HALT, never iterate).
