# Reading-Quality Evaluation Harness — Design Memo

**Status:** LOCKED  **Date:** 2026-07-11  **Owner:** solo quant (AlphaLens)  **Context:** research-lab telemetry design; consumed by future sessions implementing the eval harness. Companion to `docs/research/edge_signal_attribution_*.md` and the thematic argumentation stage.

## TL;DR

- Reframe the qual-edge question on the right axis: **READING vs PREDICTING**. Reading = turning unstructured text into verified structured facts (verifiable NOW against ground truth). Predicting = forecasting which ticker outperforms (forward-only, months to a verdict, and so far net-negative per EDGE attribution).
- 15 paradigm failures show pure numeric published factors are arbitraged out on this universe; EDGE attribution shows the current LLM *selection* step is net-negative. The falsifiable, fast-loop part of "qual edge" is **reading quality**, not predicting quality.
- This memo LOCKS: (1) a reading-quality harness with six tasks **T1–T6** mapped to real pipeline stages, and (2) a **T6 brief-faithfulness pilot** — a deterministic-first scorer that flags fabricated numeric/date atoms and characterization violations against the frozen golden cassettes, shipped as a CI gate plus a non-gating measurement harness.
- **T6 is a generation-fidelity task, NOT a reading task.** T1–T5 score text → facts (extraction). T6 scores facts → text (constrained generation). T6's ground truth is the internal facts block, so it CANNOT support the "structuring-latency reading edge" thesis; it measures LLM-vs-facts fidelity only. It ships first because it is the highest-leverage falsifiable thing measurable today, not because it evidences the reading edge.
- Hard guardrail: the harness measures reading/fidelity correctness and is **deliberately NOT joined to any outcome/return ledger**. Reading-correct is necessary, not sufficient. A green reading dashboard never means "the tool makes money".

## 1. Motivation / thesis

The empirical record on this repo is strong and negative for one specific claim: **pure numeric published factors are effectively arbitraged out** on the US mid/large-cap universe. Fifteen paradigm failures are catalogued in `docs/research/paradigm_failures_postmortem.md`; the only standing positive is insider Cohen-Malloy at `PASS_MARGINAL`.

The natural next hope — "the alpha lives in the LLM's *decisions*" — is, so far, **falsified, not found**. EDGE selection-attribution (`docs/research/edge_signal_attribution_*.md`) shows the current LLM-driven selection is net-negative: uniformly-negative BHAR vs SPY that *deepens* with horizon. The tool "fades less, but never makes money". Betting the research program on an unmeasured "qual edge" is a falsifiability trap.

The way out is to split the LLM's job into two economically different axes:

- **Reading** — turn existing unstructured text into verified structured facts: is the catalyst real or noise, is the entity correctly resolved, does the article actually fit the theme, is a 10-K mention substantive, are two stories the same arc. Every one of these is a text → structured-fact extraction that is **verifiable now** against ground truth. (These are T1–T5.)
- **Predicting** — forecast which ticker outperforms. Here the LLM is cutoff-blind, buys the priced-in narrative, and imports a run-up → fade momentum-chasing bias (the `theme_mapper` sees only the theme *name*). This is exactly the net-negative step EDGE found.

**Where T6 sits (and does not).** T6 (brief faithfulness) is a THIRD, distinct axis: **generation fidelity** — given a fixed facts block, does the generated prose stay inside those facts? It is neither reading (text → facts) nor predicting (facts → forecast); it is facts → text. It is in this harness because it is falsifiable and cheap, but it must not be cited as evidence that the reading edge is real. Its ground truth is the internal facts block, not the world (see §10).

Key economic point: **the market prices information that is cheap to acquire, not information that is expensive to assemble.** A reading edge (T1–T5) is a *structuring-latency* edge — plausibly real in the thin-coverage $500M–$10B bracket — and it **decays with LLM adoption**. The durable part is the proprietary multi-source × PIT × universe assembly, not the reading per se.

Doctrine payoff, and it is already the house rule (facts precomputed, LLM only reasons, filter post-hoc in Python): **let the LLM READ, let a MECHANICAL rule PREDICT.** The single pipeline step that currently lets the LLM *predict* — `theme_mapper` proposing tickers from a theme name — is precisely the leak EDGE identified.

Why build this harness: the reading half of qual edge is **falsifiable** and runs on a fast loop against ground truth; predicting-quality is forward-only (N ≥ 30, months). This resolves the trap — do not bet on unmeasurable edge; **measure the reading (and generation fidelity), ledger the predicting.**

## 2. Scope / guardrail

The harness measures **reading / generation-fidelity correctness** and is **deliberately NOT joined to any outcome or return ledger**. This protects the EDGE N ≥ 30 first-look discipline — a reading score must never leak into the forward selection ledger.

Reading-correct is a **necessary, not sufficient** condition for the tool working. A green reading dashboard must never be read as "the tool makes money". This sentence is load-bearing and is repeated in the doctrine-compliance section on purpose.

**Pre-registration clause (auditable, not prose intent).** The ONLY registered success criteria for the T6 v1 pilot are `fabricated_numeric_date_atoms == 0` AND `characterization_violations == 0` over the frozen `gold_v1` cassette set (plus the seeded positive-control in §6.7 firing red). No reading or faithfulness metric may be cited as evidence for a selection, ordering, or exit change. Any use of a reading score to inform, tune, or justify the forward EDGE ledger is explicitly out of scope and forbidden. This closes "reading looks good" into a line that cannot post-hoc rationalize a prediction decision.

## 3. Reading tasks T1–T6

T1–T5 map to real pipeline stages and are text → structured-fact extractions. T6 is the odd one out: it scores the GENERATED brief (facts → text), so it is labelled a **faithfulness / generation-fidelity** task, kept in the harness but held apart from the reading-edge thesis. `theme_mapper` is intentionally **not** on this list — proposing tickers from a theme name is the *predicting* step; its reading-equivalent is T5 theme-fit verification.

Each task has an asymmetric cost structure (a false positive and a false negative are not equally bad).

| Task | Kind | Pipeline stage | Question | Primary metric | Cost asymmetry |
|------|------|----------------|----------|----------------|----------------|
| **T1** Entity extraction | reading (text→facts) | `extractor` (`primary_entities`) | Does the article actually name these companies? | precision / recall / F1, **precision-weighted** | A false entity → false catalyst (e.g. `voc.com.cn` → BAH). Precision dominates. |
| **T2** Catalyst real/noise | reading (text→facts) | `catalyst_resolver` | Is the chosen trigger a real event? | precision / recall | Asymmetric: a noise catalyst drives a bad card. |
| **T3** State-media filter | reading (text→facts) | `catalyst_resolver._filter_entityless_events` | Was state media correctly dropped? | **FN rate** (state media slipped) reported separately from **FP** (legit source blocked) | FP must be ≈ 0 (fail-open backstops for Iran / NK / Belarus). |
| **T4** Story-arc dedup | reading (text→facts) | `catalyst_resolver` Jaccard arc | Are two items the same story? | pairwise same-story precision / recall (or Adjusted Rand) | Over-merge hides distinct catalysts; under-merge double-counts. |
| **T5** Gate substantiveness | reading (text→facts) | `tenk_grep` / `recent_press` | Is the theme keyword a *substantive* mention, not boilerplate? | precision | A boilerplate match is a false theme-fit. |
| **T6** Brief faithfulness | **fidelity (facts→text)** | `argumentation/` | Does the generated brief stay inside the injected facts (zero numeric/date fabrication, no forbidden framing)? | fabricated-numeric/date + violation counts (gating); groundedness + coverage (diagnostic) | **HIGHEST LEVERAGE** — a hallucinated fact actively misleads the buy-side reader. **FIRST PILOT.** |

## 4. Gold set construction

- **Bootstrap from live output, not synthetic.** Sample real `(date, ticker, article)` tuples from parquet history; label a stratified sample. Synthetic examples do not exercise the real failure surface. (The one deliberate exception is the seeded positive-control in §6.7, which is synthetic *by design* to prove the matcher fires.)
- **Stratify by**: source type (GDELT / RSS / Polygon / EDGAR), **language** (EN vs foreign — where state-media and entity-less cases live), theme, and **critically accept/drop**. Without dropped items in the gold set, reading **false negatives are invisible**.
- **Size**: mirror the PIT-validation doctrine (≥ 5 sector-diverse anchors × 2-source triangulation). MVP ≈ 50–80 items per task; ≈ 150 for the high-cost tasks T1 and T6.
- **Immutable + versioned**: hash-frozen, tagged `gold_v1` — same pattern as the existing golden cassettes. A new model re-runs against the frozen gold; the gold does not move to fit the model.
- **Numeric gold is machine-derived, not hand-labelled.** For the T6 numeric/date core, the gold labels are **derived programmatically from the parsed facts block**, then diffed against the matcher. This validates the matcher against a source *independent of the labeller* and breaks the solo-owner closed loop for the numeric core (see §10). Hand labelling is reserved for the subjective tasks (T2 catalyst real/noise, T5 substantiveness) with a 2-source triangulation trail.
- **Labelling cheap + rubric-driven** (subjective tasks only): binary / categorical labels, not free-text. A **written rubric IS the quality control** for the subjective tasks — solo owner, no inter-rater agreement available, so the rubric enforces self-consistency over time. Hard cases go to 2-source triangulation (Perplexity URL surface → human confirm).

## 5. Metrics

Reading (T1–T5) is a classification / extraction problem with asymmetric costs. For every task:

- Report **precision / recall / F1** with **Wilson confidence intervals** (small N).
- Report **absolute counts of the high-cost class**, not just rates (e.g. "3 state-media items slipped", not "97% clean").

T6 metric conventions are specified in §6.6; note in particular that `groundedness_rate` is a diagnostic denominator-check, never a headline pass/fail (§6.6, §10).

## 6. T6 faithfulness pilot — the first deliverable

The brief-faithfulness scorer is the first thing built. It has the highest leverage (a hallucinated fact actively misleads the reader) and it is measurable today against the frozen golden cassettes. It scores the LLM OUTPUT JSON fields (`tldr`, `supply_chain_reasoning`, `bear_summary`, `catalyst_failure_exit`), which map to the persisted columns `brief_tldr` / `brief_supply_chain_md` / `brief_bear_summary_md` / `brief_catalyst_failure_exit` when the live-corpus path reads the parquet.

### 6.1 Three failure classes

- **(a) Fabrication** — a number, date, name, or product **not present** in the `<facts>` block. Violates the `prompts.py` instruction (Pro `_PRO_TEMPLATE` ~L221–224, Flash `_FLASH_TEMPLATE` ~L259): *"Do NOT invent numbers, prices, dates, products, or names not present in `<facts>`."* **In v1 only NUMERIC and DATE fabrications are gating** (see §6.4); entity/product fabrications are non-gating review candidates deferred to a Phase-2 judge, because legitimate briefs routinely name world-knowledge entities that are not in the facts block (§6.4).
- **(b) Distortion** — the value **is** in facts but is rounded, converted, or paraphrased outside tolerance against the typed-fact "quote exactly" contract. The GROUNDED / DISTORTED boundary is a specified sign+rounding+tolerance policy (§6.4), not a loose intuition.
- **(c) Characterization violation** — forbidden framing, e.g. calling a 52-week drawdown "cheap" / "on sale" / "bargain" / "promotion", or forecasting an outcome from `next_earnings_date`. Lexical (keyword-proximity) detectable, with negation/quote handling; the lexicon is derived verbatim from `prompts.py` ~L231–235 (Pro) / ~L260–262 (Flash) and version-pinned (§6.4, §10).

### 6.2 Core design decision: score only checkable atoms

Score **only CHECKABLE ATOMS** — numbers, dates, and (Phase-2) named entities / specific products. Free reasoning and mechanism claims (the prose in `supply_chain_reasoning`) are **world-knowledge** and are marked **`OUT_OF_SCOPE`** — neither grounded nor fabricated — because they cannot be checked against the facts block. Without this rule the metric would penalize legitimate reasoning exactly as hard as it penalizes lies, and become useless.

**Critical scoping correction (gate blind spot).** OUT_OF_SCOPE applies to PROSE MECHANISM ONLY, not to the whole `supply_chain_reasoning` field. The numeric/date matcher **runs inside every field, including `supply_chain_reasoning`**, so a bare fabricated number or date sitting inside a mechanism sentence is still caught. This matters because the live cassettes put load-bearing derived claims (DFIN "Form S-1", QLYS "CrowdStrike, Tenable", mechanism chains) in `supply_chain_reasoning`, and `supply_chain_reasoning` is the top numeric-fabrication-risk field (§6.6). A blanket field-level exemption would make the gate blind exactly where risk concentrates. The residual blind spot — a *prose mechanism* that is plausible but wrong, carrying no checkable atom — is real and un-gated; it is stated as a hard limit in §6.7 and §10.

### 6.3 Claim-decomposition record

Each extracted atom produces one record. `field` uses the LLM SCHEMA field names (per `schema.py` ~L19–22), not the persisted-column names:

```
{
  field:           str,        # tldr | supply_chain_reasoning | bear_summary | catalyst_failure_exit
  span:            str,        # the literal text span
  kind:            enum,       # numeric | date | entity | product | characterization
  extracted_value: str,        # normalized/canonicalized value
  verdict:         enum,       # GROUNDED | FABRICATED | DISTORTED | VIOLATION | OUT_OF_SCOPE | DEFERRED
  gating:          bool,       # v1: True only for numeric/date FABRICATED|DISTORTED and characterization VIOLATION
  matched_fact:    str | None  # the fact index key that grounded it, if any
}
```

`DEFERRED` is the non-gating bucket for entity/product atoms with no fact-index coverage in v1 (routed to Phase-2, never red in v1).

### 6.4 Deterministic-first scoring (v1, NO LLM-judge)

v1 uses **no LLM-judge** — this sidesteps the "who evaluates the judge" problem and keeps the gate fully hermetic.

**Step 1 — build the fact index from the TYPED source, not the rendered string.** Where available, parse the persisted **`brief_template_facts_json`** column (`orchestrator.py` ~L309 / ~L519) or the score-stage typed facts dict into a normalized fact index. Use the rendered `<facts>` string inside the cassette `contents` **only as a fallback** for cassette-only runs where the typed JSON is not carried. Rationale: scoring the LLM against the same typed values the pipeline injected isolates *model* behaviour from *regex parser drift on a display string* (`$2.78B`, `4.2x`, `-34.0%`). Fields: `market_cap`, P/S, EV/Rev, FCF margin, ROIC, insider ($), 52-week high/low distances, MA200, ATR, catalyst title/url, ticker/company/theme/industry, `next_earnings_date`. Canonicalize units (`%`, `$`, `k`/`M`/`B`).

**Parser-correctness guard (separate from model-faithfulness).** A self-test asserts the parser round-trips the KNOWN typed facts for the 4 golden cassettes. A red gate must never be a regex bug misattributed to the model.

**Step 2 — extract atoms** from the four brief fields via regex + NER for numbers, dates, tickers, and multipliers (`4052.9`, `-34%`, `$180k`, `4.2x`).

**Step 3 — match, with an explicit sign/rounding/tolerance policy** (the GROUNDED ↔ DISTORTED ↔ FABRICATED boundary is entirely this policy, so it is specified, not left to "exact string"):

- **Numeric:**
  1. **Sign:** when the fact is a directional distance (e.g. `52w high -50.0%`) and the brief supplies its own direction word ("50% drawdown from 52w high", "40.5% below 52-week high"), strip the sign before comparison. So brief "50%" vs fact `-50.0%` → **GROUNDED**; brief "21% below MA200" vs fact `-21.0%` → **GROUNDED**.
  2. **Rounding tolerance:** round the fact to the brief's stated precision. Equal after rounding → **GROUNDED**.
  3. **Band:** if not equal after rounding but within ≤ 2× the stated precision band (or a small relative tolerance) → **DISTORTED**. So brief "50% drawdown" vs fact `-39.2%` → **DISTORTED** (real distortion). Beyond that band, and no other fact covers it → **FABRICATED** (gating in v1).
- **Date:** exact after canonicalization → GROUNDED; not present in fact index → **FABRICATED** (gating in v1).
- **Entity / product:** first attempt to ground against the fact index AND a token set parsed from the catalyst title (so catalyst-derived names like Walmart / Target / SpaceX ground instead of firing). If still unmatched → **DEFERRED** (non-gating in v1, routed to Phase-2). v1 does NOT fire FABRICATED on entity/product, because a correct supply-chain brief legitimately names competitors and derived products (QLYS "CrowdStrike, Tenable"; DFIN "Form S-1") that are not in the facts block. Flagging those would fail correct briefs or force whitelist churn over the real signal.
- **Characterization:** high-precision forbidden lexicon (`cheap`, `on sale`, `bargain`, `promotion`, + forecast verbs adjacent to `next_earnings_date`), keyword-proximity, **with negation/quote-context handling** so "not a bargain", "do not treat the drawdown as cheap", or a quote of the prompt's own guidance does NOT fire → **VIOLATION** (gating) only on the affirmative, un-negated, un-quoted match. Paraphrased framing ("attractive entry post-pullback") is lexically invisible and is a known evasion, deferred to the Phase-2 judge (§10).

**Unit tests pin the boundary over all 4 cassettes** so the rule cannot silently degrade to "everything grounded":
- QUBT "50% drawdown" vs fact `-50.0%` → **GROUNDED**
- DFIN "21% below MA200" vs fact `-21.0%` → **GROUNDED**; "40.5% below 52-week high" vs fact `-40.5%` → **GROUNDED**
- MANH "-39.2% from 52w high" vs fact `-39.2%` → **GROUNDED**
- QUBT P/S "4052.9" vs fact `4052.9` → **GROUNDED**
- Positive-control DISTORTED: a seeded "50% drawdown" vs a `-39.2%` fact → **DISTORTED** (so the DISTORTED branch is proven reachable)

**LLM-judge is PHASE 2 only**, restricted to *soft atoms* (entity paraphrase, product names, paraphrased characterization) in the DEFERRED bucket, and it **must report judge-vs-rubric-adjudicated-label agreement (single adjudicator)** — never a sole gate, and never presented as independent inter-rater reliability (§10).

### 6.5 Worked example (real cassette — MANH, 2026-05-24)

Fact index (from MANH `brief_template_facts_json`): P/S `7.5`, EV/Rev `7.2`, 52w-high distance `-39.2%`, MA200 distance `-18.3%`, catalyst title *"Walmart and Target are about to show just how much shopping habits have changed due to the Iran war"*.

MANH `supply_chain_reasoning` (real): *"…technicals show lagging momentum: -39.2% from 52w high, -18.3% below MA200 … sector percentile only 45, not a bargain."*

| Span | Field | Kind | Verdict |
|------|-------|------|---------|
| `-39.2%` | supply_chain_reasoning | numeric | GROUNDED (matches fact `-39.2%`) |
| `-18.3%` | supply_chain_reasoning | numeric | GROUNDED (matches fact `-18.3%`) |
| `Walmart` / `Target` | tldr/reasoning | entity | GROUNDED (parsed from catalyst title token set) |
| `not a bargain` | bear/reasoning | characterization | **not** VIOLATION (negation-guarded; "not a bargain" is the correct, compliant framing) |

The real MANH brief is clean — no fabrication, no violation. It is the **must-NOT-fire** case that proves the matcher does not over-fire on a correct, grounded brief.

**Contrast — the seeded positive-control** (synthetic, §6.7): a brief with `tldr` = *"…a cheap entry after only a 25% pullback…"* against the same MANH facts yields two gating hits at once: `25%` vs fact `-39.2%` → **DISTORTED/FABRICATED** (band-dependent), AND `cheap entry` → **VIOLATION**. This is the case the gate MUST flag red. Reporting both `fabricated/distorted` counts and `characterization_violations` is why the gate cannot be fooled by a brief that is numerically grounded but rhetorically misleading.

### 6.6 Metrics

Field names are the SCHEMA/output keys (§6.3), not persisted-column names.

- **PRIMARY (target 0, GATING on the golden set) — v1:**
  - `fabricated_numeric_date_atoms_per_brief` (numeric + date only in v1)
  - `characterization_violations_per_brief`
- **SECONDARY (measurement, Wilson CI over a corpus):**
  - `distorted_atoms_per_brief`
  - `deferred_entity_atoms_per_brief` (non-gating; Phase-2 input)
  - `groundedness_rate` — **diagnostic denominator-check only, never a headline** (see below and §10)
  - **`checkable_coverage`** = (checkable atoms) / (checkable + OUT_OF_SCOPE atoms) per brief — makes a shrinking checkable denominator visible
  - per-field breakdown (expect `supply_chain_reasoning` highest fabrication risk, `bear_summary` second)

**`groundedness_rate` is green-while-broken by construction.** OUT_OF_SCOPE atoms leave the denominator, and the top-risk failure (a wrong prose mechanism) is OUT_OF_SCOPE, so this rate can only trend to 100% and never falls as a brief gets *more* misleading. It is therefore NEVER shown without the paired per-brief `OUT_OF_SCOPE` count and `checkable_coverage`, and it is never a pass/fail. It is not a truth metric — it cannot detect a wrong mechanism.

### 6.7 Two artifacts (mirror the live-probes pattern: gating hermetic + non-gating measurement)

1. **Regression gate (CI, gating)** — a test asserts, over the **frozen golden cassettes**, that `fabricated_numeric_date_atoms == 0` AND `characterization_violations == 0`, AND that a **seeded positive-control brief fails** (a synthetic brief carrying a known fabricated number + a known VIOLATION MUST be flagged). The positive-control mirrors the repo's `test_no_raw_*_http` "positive-control so the regex cannot rot to empty" pattern: without it, an all-clean N=4 gate ships GREEN day one and a never-fired gate is indistinguishable from a no-op. **Failure semantics:** the gate re-runs on the CURRENT cassette content, so when a PR re-records cassettes (a deliberate, PR-reviewed act via `record_golden_brief.py`) a prompt/model change that introduces a fabricated number/date or a forbidden characterization goes **red in the same PR**; a legitimate prompt improvement that stays grounded stays green.

   **Gate blind spot (stated explicitly).** This red-on-hallucination guarantee holds for **checkable numeric/date atoms and lexical characterization only**. A fabricated *prose mechanism* in `supply_chain_reasoning` that carries no checkable atom is OUT_OF_SCOPE and **re-records GREEN** — the gate is blind there by construction. `supply_chain_reasoning` is simultaneously the highest fabrication-risk field, so this blind spot is material and is repeated in §10. The v1 mitigation is that the numeric/date matcher still runs inside `supply_chain_reasoning`, so a bare fabricated number there IS caught; only pure prose escapes.

2. **Measurement harness (research, non-gating)** — the same `score_brief` over a larger stratified sample of live briefs → rate with CI. The current golden set is only **4 cassettes** (DFIN / QLYS / QUBT / MANH, `_ASOF` = 2026-05-24): fine as a **gate**, too thin as a **measurement**.

## 7. Real code touchpoints

All to be re-verified against the repo at implementation time; line numbers are approximate.

| Concern | File | Location |
|---------|------|----------|
| Facts block builder (numerics injected as pre-formatted strings) | `alphalens_pipeline/thematic/argumentation/prompts.py` :: `_format_facts_block` | ~L140–193 |
| Typed facts (preferred fact-index source) persisted column | `argumentation/orchestrator.py` :: `brief_template_facts_json` | ~L309 / ~L519 |
| Groundedness instruction (verbatim "Do NOT invent numbers…") | `prompts.py` (`_PRO_TEMPLATE`, `_FLASH_TEMPLATE`) | ~L221–224 (Pro), ~L259 (Flash) |
| Characterization rules (52w drawdown ≠ cheap/on-sale/bargain/promotion; `next_earnings_date` factual-only, no forecast) — **v1 lexicon derives verbatim from here** | `prompts.py` | ~L231–235 (Pro), ~L260–262 (Flash) |
| Char limits (tldr 200 / supply_chain 400 / bear 250 / catalyst_failure_exit 200) + bear MANDATORY ≥ 2 risks — **prompt contract, NOT the JSON schema** | `prompts.py` (`_PRO_TEMPLATE` ~L210–215, `_FLASH_TEMPLATE` ~L253–256) | ~L210–215 / ~L253–256 |
| Output schema `BRIEF_RESPONSE_SCHEMA` — 4 field NAMES only (`tldr`, `supply_chain_reasoning`, `bear_summary`, `catalyst_failure_exit`), each `{type: string}`; no limits here | `argumentation/schema.py` | ~L16–30 |
| Persisted columns (`brief_tldr`, `brief_supply_chain_md`, `brief_bear_summary_md`, `brief_catalyst_failure_exit`, `brief_model_used`, `brief_template_id`, `brief_template_facts_json`) | `argumentation/orchestrator.py` | ~L299–309 / ~L508–524 |
| Golden cassettes dir (record shape `{key, model, contents, config, openrouter_response:{choices:[{message:{content,reasoning},finish_reason}],usage}}`) | `apps/alphalens-research/tests/golden/fixtures/brief_day/cassettes/*.json` | — |
| Cassette key (`sha256` over canonical JSON of `{model, contents, config}`) | `tests/golden/replay_client.py` :: `cassette_key` | ~L67–75 |
| `ReplayOpenRouter` (cassette replay client) | `tests/golden/replay_client.py` :: `ReplayOpenRouter` | ~L78–126 |
| Replay driver + `_ASOF` = 2026-05-24 | `tests/golden/test_golden_brief_replay.py` :: `_replay_briefs` (~L57–76), import at L30, `_ASOF` at L32 | — |
| Re-record script (`RecordingOpenRouter` writes `{key}.json`) | `apps/alphalens-research/scripts/record_golden_brief.py` | — |

## 8. Module placement

- **`apps/alphalens-research/alphalens_research/eval/`** — new package:
  - **`__init__.py`** declares `__status__ = "RESEARCH_ONLY"` at PACKAGE level. Note: `eval/` is **not** under `LAYER_ROOTS` in `apps/alphalens-research/tests/test_layer_status.py` (the eight roots are `screeners`, `gates`, `backtest`, `overlays`, `attribution`, `preaudit`, `diagnostics`, `retrospective_audit`), so `__status__` is **optional-but-validated-if-present** — declaring it is good hygiene, not a hard requirement today. If we later want `eval/` gated like a layer, add `PACKAGE_ROOTS["alphalens_research"] / "eval"` to `LAYER_ROOTS` in the same PR (test auto-discovers packages, so the package `__init__.py` is the discovered unit, not the module constant).
  - **`faithfulness.py`** — pure functions:
    - `parse_facts_index(typed_facts_json | contents)` → normalized fact index (typed source preferred; rendered `<facts>` fallback)
    - `extract_atoms(field, text)` → list of atoms
    - `score_brief(facts_index, brief_fields)` → `FaithfulnessResult`
- **Test**: `apps/alphalens-research/tests/golden/test_golden_brief_faithfulness.py` — reuse the `replay_client` cassette loader; includes the parser round-trip guard and the seeded positive-control.
- **CLI**: `alphalens eval faithfulness --corpus {golden|live}` — a research command, **lazy-imported** per the workspace DAG (`alphalens_pipeline.*` must not top-level-import `alphalens_research.*`).

## 9. Doctrine compliance

- **Display / telemetry only** — touches NO `selection_score`, NO ordering, and is NOT joined to any outcome ledger. The pre-registration clause in §2 forbids citing any reading score for a selection/exit decision.
- **LLM cutoff-blindness** — no numbers come from the LLM; the eval reads precomputed facts (preferably the typed `brief_template_facts_json`) + LLM output and matches deterministically.
- **No manufactured authority** — reading-correct is labelled necessary-not-sufficient and is never presented as validated alpha. The Phase-2 judge agreement metric is labelled "judge-vs-rubric-adjudicated-label (single adjudicator)", not inter-rater reliability.
- **Poolability** — the GATE inherits cassette-key poolability (prompt/model change → key change → PR-reviewed re-record). The non-gating MEASUREMENT corpus additionally stamps every record with `(brief_model_used, brief_template_id, gold_vN, faithfulness_scorer_version)` — where `faithfulness_scorer_version` covers the eval code + the forbidden-characterization lexicon version. Rates are partitioned/reported by this tuple and never pooled across a change. These keys already exist as persisted brief columns, so the join is free.
- **English-only repo doc**; **reuse the existing golden-cassette infra** rather than a parallel harness.

## 10. Honest limits

- **Gate is BLIND to prose-mechanism fabrication (material).** OUT_OF_SCOPE prose in `supply_chain_reasoning` — the highest fabrication-risk field — carries no checkable atom, so a fabricated or wrong *mechanism* (plausible-but-false causal story) re-records GREEN. v1 only guarantees red on fabricated numeric/date atoms (which the matcher DOES scan inside `supply_chain_reasoning`) and lexical characterization violations. This is the single biggest limit and it is by construction; catching wrong mechanisms is a different, harder, possibly-unfalsifiable eval.
- **Groundedness ≠ world-truth, and `groundedness_rate` is not a truth metric.** A faithful quote of a *wrong upstream fact* scores GROUNDED — catching a wrong fact is T1/T5's job. T6 measures fidelity LLM → facts only. `groundedness_rate` can only trend to 100% and never falls as a brief gets more misleading, so it is diagnostic-only and always paired with `checkable_coverage` + OUT_OF_SCOPE counts.
- **T6 is generation fidelity, not reading.** Its ground truth is the internal facts block, so it CANNOT be cited as evidence the structuring-latency reading edge (T1–T5) is real. Stated up front so a green T6 is never laundered into "the reading edge exists".
- **v1 entity/product fabrication is NOT gated.** Legitimate briefs name derived world-knowledge entities (competitors, filing forms) absent from the facts block; v1 routes these to a non-gating DEFERRED bucket (Phase-2 judge). So an *actually-fabricated* entity/product is not caught in v1 — a real coverage gap, accepted to avoid failing correct briefs.
- **Characterization detection is lexical, not semantic.** Paraphrased framing ("attractive entry post-pullback") evades the forbidden-lexicon gate; a drifted model could route around the one gating VIOLATION metric. Semantic paraphrase is deferred to the Phase-2 judge. Negation/quote handling reduces over-fire but is itself heuristic.
- **Integer-only numeric metrics are NOT gated.** A checkable atom needs a unit (`%`/`$`/`x`/magnitude) or a decimal point; a fabricated integer-valued metric with no symbol (`RSI 99`, an integer P/E, an integer percentile) is treated as a structural reference and is neither extracted nor gated (§6.2). Real false-negative surface, pinned by a documenting test (`test_integer_only_metric_is_not_gated_known_gap`) so a future change that closes it is noticed. Surfaced by the pre-merge review 2026-07-11.
- **4-cassette golden = gate, not measurement.** With N=4 all-clean briefs, the primary gate is GREEN day one; the seeded positive-control is what proves the matcher works. Verdicts on so few briefs are directional, not distributional.
- **Deterministic-matcher risk on legit paraphrase.** Numerics/dates are the trusted, machine-derivable core; entities and characterizations route to adjudication. The GROUNDED/DISTORTED boundary depends on the stated sign+rounding+tolerance policy — pinned by unit tests, but a mis-set band would silently mis-classify.
- **Solo-owner closed loop (partially broken, not eliminated).** For the numeric/date core the gold labels are machine-derived from the facts block and diffed against the matcher, so the labeller-writes-everything loop is broken there. For the subjective tasks (T2/T5) and Phase-2 soft atoms the loop remains: one owner writes the rubric, labels, and scores, and the Phase-2 "agreement" is judge-vs-that-same-rubric, i.e. self-consistency, not independent ground truth.
- **Reading-edge decays with LLM adoption** — the durable moat is the assembly (multi-source × PIT × universe), not the reading step.

## 11. Sequencing

1. **Pilot** — T6 deterministic v1: fabricated-numeric/date + characterization gate on the 4 golden cassettes, WITH the parser round-trip guard and the seeded positive-control. Unit tests pin the GROUNDED/DISTORTED boundary over all 4 cassettes.
2. T1 entity precision.
3. LLM-judge for soft atoms (entity/product paraphrase, paraphrased characterization) — **deferred to Phase 2**, agreement-reported, never a sole gate.
4. Broader corpus measurement (live briefs), stamped with the poolability tuple.
5. Only then consider T2–T4.

## 12. Open questions

- Is the $500M–$10B bracket actually slow enough for a structuring-latency edge, or is it already efficient? (Empirical — for the forward EDGE ledger and the T1–T5 reading tasks to inform; T6 cannot speak to this.)
- Rubric wording for "substantive vs boilerplate" (T5) and "real vs noise" catalyst (T2) — needs a first pass before those subjective tasks ship.
- Larger measurement-corpus source: re-record more cassettes vs read the live `thematic_briefs` parquet directly (and, when reading parquet, whether `brief_template_facts_json` is always populated for the typed-source fact index or whether the rendered-string fallback is needed).
