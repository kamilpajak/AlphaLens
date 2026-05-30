# Structured Event Templates — Design Memo (Issue #143)

**Date:** 2026-05-30
**Status:** LOCKED — ready for PR-1 implementation
**Branch:** `docs/template-engine-design-2026-05-30`
**Issue:** #143 (Ravenpack-style template-matching catalyst filter, deferred 2026-05-18)
**Roadmap slot:** Track H sub-feature (foundation layer for Tracks D + G + H)
**Reviewers consulted:** DeepSeek v4 Pro (zen, thinking=high) + Perplexity Research (sonar deep, medium effort)

> Replaces the deferral note on issue #143. Both reviewers converged independently on the same architecture (hybrid mode + YAML+predicate DSL). This memo captures the locked decisions and the 5-PR sequence.

---

## §0. Context — what changed since deferral

Issue #143 was deferred 2026-05-18 (post PR #141/#142 noise-filter session) with the explicit gate "revisit when option-C noise-filter precision stabilizes below ~70%". The cross-reference note on the issue (post PR #185 two-tier clustering) confirmed the gate had **not** been met: the Surfshark-class miscall had not recurred.

What changed:

1. **User velocity correction (2026-05-30):** estimated 5+ days for full implementation now treats as 1-2 sessions. Removes the primary deferral reason (engineering opportunity cost vs paper-trade harness + feedback ledger work).
2. **Ideal-shape doc (`docs/research/alphalens_ideal_shape_2026_05_29.md`)** clarified that #143 is **not** a new track but a foundation layer feeding three named tracks:
   - **Track D** (evidence panel polish) — typed facts for sentence-level citations without LLM paraphrase risk
   - **Track G** (multi-data corroboration) — compound catalyst sequences (M&A → financing → analyst) require typed entities
   - **Track H** (GDELT pipeline) — multi-source dedup via `{acquirer, target, amount, date}` tuple
3. **PR-G #318 swap to DeepSeek v4** cut LLM cost ~$66/mo (issue #316), weakening one of the original motivators (cost-driven pre-filter) but the precision/auditability/replay-ability motivators stand untouched.

---

## §1. Architecture decisions (LOCKED)

### §1.1 Extraction mode — **Hybrid** (templates first, LLM fallback)

**Both reviewers converged independently.**

- Strict template-match-or-drop architecture (Ravenpack, GDELT, ICEWS) is appropriate for **vendor-grade feeds powering automated trading**, where false positives cost real money. It is **inappropriate for buy-side decision support** where missed catalysts cost more than dismissable noise.
- Flash @ 0.91 mean confidence is not noise — the problem to fix is **determinism + auditability + replay-ability for canonical events** (M&A, earnings, financing, guidance, regulatory_action), not global extraction quality.
- Downstream verification gates (4 independent) absorb noise but **do not restore recall** that was lost at extraction.
- Long-tail / narrative events (ESG, crypto-regulation, SPAC variants, "renewable momentum") are extracted well by Flash and would suffer under strict template-only mode.

**Hybrid pipeline contract:**

```
article
  ↓
Template engine attempts match
  ↓
  ├── MATCH → typed event {template_id, event_type, entities, attributes}
  │           extraction_method = "template"
  │
  └── NO MATCH → Flash LLM extract (existing behavior)
                 extraction_method = "flash"
                 ↓
                 IF Flash returns event_type ∈ NOISE_EVENT_TYPES OR confidence < 0.5
                   → DROP to holdout queue with reason=low_confidence_no_template
                 ELSE
                   → emit event with extraction_method="flash"
```

**Precedence rule (DeepSeek-recommended, locked):** if both a template event and a Flash event exist for the same `(primary_entity_ticker, event_type)` within a 24-hour window, the template event wins. The Flash event is dropped to holdout (not emitted, not deleted — visible for audit). This eliminates the "two truths" problem in the catalyst resolver.

**Deprecation path (DeepSeek-recommended):** hybrid is a **coexistence phase**, not a permanent architecture. The metric `template_match_rate = matched_articles / (articles_not_blocked_by_pre_filters)` is tracked from PR-1. Threshold gates:

| match_rate | Action |
|---|---|
| ≥ 60% on rolling 30d | Eligible to switch to strict mode (separate decision; not automatic) |
| 40-60% | Hybrid stable; consider expanding template library |
| < 40% | Stay hybrid; expand templates or accept gap |

### §1.2 Template DSL — **YAML + named Python predicates**

**Both reviewers converged independently.**

- Pure code (Python regex / pattern objects scattered across modules) blocks the analyst-in-the-loop workflow. The user authors templates themselves; iteration speed matters more than engineering ceremony.
- Pure YAML (with embedded conditional logic, control flow) becomes an ad-hoc mini-language with weak debugging tools.
- YAML for declarative parts (which event_type, which required entities, which regex patterns, which fields to extract) **plus** named Python predicate functions referenced from YAML (for boolean checks the analyst cannot reasonably express in pure data) gives the best of both.

**Precedent in this codebase:** `apps/alphalens-pipeline/alphalens_pipeline/thematic/config/catalyst_noise_filters.yaml` already encodes URL blocklist regex in YAML. This memo extends that pattern from flat-list regex to structured event templates.

**Schema example (illustrative — final schema codified in PR-1):**

```yaml
template_id: m_and_a_press_release
event_type: m_and_a
description: "Acquirer announces acquisition of target with stated consideration"

# Article-level predicates — ALL must pass for template to fire
article_predicates:
  - is_press_release
  - not_listicle
  - amount_mentioned

# Required entities — at least one ticker from each role must resolve
entity_requirements:
  acquirer:
    role: company
    required: true
  target:
    role: company
    required: true

# Field extraction — patterns operate on article body, capture named groups
extraction:
  - field: acquirer_ticker
    source: entity:acquirer

  - field: target_ticker
    source: entity:target

  - field: consideration_usd
    patterns: |
      \$(?P<amount>[\d.]+)\s*(?P<unit>billion|million|B|M)
    post_process: [normalize_amount_usd]

  - field: announcement_date
    source: article.published_at
```

**Engineering contract:**

- YAML files live under `apps/alphalens-pipeline/alphalens_pipeline/thematic/extraction/templates/*.yaml`.
- Each YAML file = exactly one template. Filename = `template_id`.
- Parser compiles YAML → `TemplateSpec` dataclass at engine startup. **The extraction engine never operates on raw YAML or dict.** All downstream code consumes `TemplateSpec` objects. Tests construct `TemplateSpec` in code without touching YAML.
- A registry function `available_predicates()` exposes the named Python predicate library. Templates reference predicates by name; an unknown name fails YAML validation at load time, not at runtime.
- Block scalars (`|` / `>`) are the canonical way to write regex (avoids YAML's backslash-escape hell).
- New CLI: `alphalens templates validate` — JSON Schema validation + regex compile-check + sample-text dry-run. Suitable as pre-commit hook AND analyst-iteration tool.

**20% escape clause (DeepSeek-recommended):** if after writing ~10-15 templates more than 20% of them require custom Python that does not fit the named-predicate pattern, the domain has outgrown YAML and the system should migrate to Python-only templates. This is a tripwire, not a hard rule — re-examine after PR-1 telemetry.

---

## §2. The four design questions (LOCKED)

These came out of the Q&A iteration in the 2026-05-30 session and are answered through the lens of the ideal-shape doctrine (kotwice § "No black-box scoring", "Augmentation, not execution", "Buy-side retail").

### §2.1 Entity resolution sequencing — **Pre-template**

Resolver runs **before** template matching, against the article's recognised entity mentions. Resolution uses the existing `~/.alphalens/edgar-detect/company_tickers.json` (CIK → ticker map) plus an alias table seeded with common variants ("the iPhone maker" → AAPL, "Tesla" → TSLA-resolved-by-context).

Three resolution outcomes per article:

| Outcome | Action |
|---|---|
| All recognised entities resolved | Proceed to template matching with typed entity set |
| Some resolved, some unresolved | Proceed with resolved subset; unresolved tracked as `partial` in telemetry |
| Zero resolved | Drop to holdout queue with `reason=entity_unresolved` |

Templates author against resolved tickers (`acquirer: AAPL`), not raw strings (`acquirer: "Apple Inc."`). This makes YAML authoring simpler for the analyst and enables Track G compound-catalyst detection (same ticker across multiple templates in a window) trivially.

**Why pre-template wins over post-template:**

- Aligns with "No black-box scoring" — drop decisions are logged with explicit reason
- Aligns with "Buy-side retail (analyst authors templates)" — typed entities are easier to write against
- Enables Track G — same-ticker detection across templates becomes O(1) instead of O(post-extraction-resolver-pass)
- Cleaner audit trail — every captured field is traceable to a resolved entity OR a regex group

### §2.2 Initial template set — **5 templates, tightly aligned with "Done looks like"**

Ideal-shape §4 ("Done looks like") explicitly names M&A leak push and earnings (`NVDA +12%`) as the two real-time push catalysts that define the product. Initial 5:

| template_id | event_type | Why in PR-1 |
|---|---|---|
| `m_and_a_press_release` | m_and_a | L1 push primary (ideal-shape §4); Track G primary |
| `earnings_surprise` | earnings | L1 push primary (ideal-shape §4); high-volume catalyst type |
| `financing_announcement` | financing | Material capital structure change; equity dilution / debt raise both covered |
| `guidance_update` | guidance | Forward-looking asymmetric (raise / cut / withdrawal); standard 8-K trigger |
| `regulatory_action` | regulatory_action | High-impact binary outcomes (FDA, antitrust, enforcement) |

**Not in PR-1, candidates for PR-1.5 / follow-up if telemetry warrants:** `product_launch`, `contract_award`, `bankruptcy`, `executive_change`, `analyst_action`.

**Why 5 and not 10:** the ideal-shape near-term roadmap (§8) competes — PR #292 feedback ledger is critical path, paper-submit ExecStartPost (Track F) and L3 weekly review stub (Track C) are ahead in the queue. 5 templates well-tested + tightly integrated with hybrid pipeline > 10 templates rushed. Per `quality over speed` doctrine.

### §2.3 Predicate library scope — **6 named predicates dnia jeden**

Drop one redundant from the original suggestion, add two that the ideal-shape anti-features filter argues for:

| Predicate | Purpose | Notes |
|---|---|---|
| `any_sentence_contains(words: list[str])` | Lexical anchor for trigger phrases | Tokenizer-aware; not raw substring |
| `amount_mentioned()` | Article contains at least one currency amount with magnitude | Regex: `\$\d+\s*(billion|million|B|M)` plus locale variants |
| `entity_type_present(type: str)` | Required entity type appears in resolved entity set | `type ∈ {company, person, regulator, currency}` |
| `not_in_blocklist(list: str)` | URL/domain not in named blocklist | Re-uses existing `catalyst_noise_filters.yaml` patterns; named lists for reusability |
| `is_press_release()` | Distinguishes company-issued primary source from third-party commentary | Heuristic: URL on issuer's IR domain OR title contains "(press release)" OR source = `prnewswire/businesswire/globenewswire` |
| `not_listicle()` | Filters Surfshark/Wired pattern that triggered #143 | Title regex `(top\|best\|cheapest\|guide to)\s+\d*` OR body bullet-list density > threshold |

**Dropped from original draft:** `published_recently(hours)` — redundant with ingest-stage clustering + cap. Time filtering belongs in catalyst resolver window logic, not per-template predicate.

**Why `is_press_release` matters operationally:** primary-source articles carry deterministic facts (amounts, tickers, dates) in normalized form. Third-party commentary paraphrases — exactly the surface where LLM extraction is on safer ground. The split aligns hybrid mode with the strength of each path.

**Why `not_listicle` is in dnia jeden:** the original #143 deferral was triggered by a listicle/promo page firing as catalyst. Predicate is the direct fix to the originating concern.

Every predicate emits Prometheus counter on call: `alphalens_template_predicate_total{name=..., outcome="pass|fail"}` (no black-box doctrine).

### §2.4 Holdout queue surface — **Telemetry-only in PR-1; SPA dashboard deferred**

The "No black-box scoring" kotwica makes silent drop doctrinally unacceptable. Holdout must be observable. But VPS observability stack just shipped (PRs #310-#314: Prometheus textfile + Alertmanager + Telegram + Grafana). That infra is the right surface for PR-1.

**PR-1 surface:**

```
alphalens_template_holdout_total{
  reason="no_template_match" | "entity_unresolved" |
         "all_predicates_failed" | "low_confidence_no_template"
}

alphalens_template_match_rate{template_id="..."}

alphalens_template_predicate_total{name="...", outcome="pass|fail"}
```

Plus one Grafana dashboard panel (auto-pickup via the existing provisioning) and one alert rule:

```yaml
- alert: AlphalensTemplateMatchRateLow
  expr: |
    (sum(rate(alphalens_template_match_rate[7d])) by (template_id))
    < 0.20
  for: 7d
  labels: {severity: warning, route: telegram}
  annotations:
    summary: "Template {{ $labels.template_id }} match rate < 20% for 7 days"
```

**Deferred to PR-4+ (or Track C):** SPA route `/review/holdout/<date>` with manual-promote workflow. Holdout dashboard naturally folds into Track C (L3 weekly review) — same SQLite + Django + SvelteKit substrate as feedback ledger.

---

## §3. Five-PR sequence

Sequenced for review-friendly chunks. Each PR is independently mergeable.

### PR-1 — Template engine + 5 templates + holdout telemetry

**New code (under `alphalens_pipeline/thematic/extraction/templates/`):**

- `engine.py` — `TemplateEngine` class: loads YAML files, compiles to `TemplateSpec`, exposes `match(article, entities) -> TemplateEvent | None`
- `spec.py` — `TemplateSpec` + `TemplateEvent` dataclasses
- `predicates.py` — named predicate registry (6 dnia jeden) with telemetry hooks
- `yaml_schema.py` — JSON Schema for YAML validation
- `templates/*.yaml` — 5 template files
- `entity_resolver.py` — wraps `company_tickers.json` lookup + alias table; pre-template resolution stage
- `holdout.py` — Prometheus counter emission for drop reasons

**New CLI command:**

- `alphalens templates validate [path]` — JSON Schema + regex compile + sample-text dry-run. Exits non-zero on any failure; suitable as pre-commit hook.

**Tests** (TDD red→green):

- `test_template_engine.py` — match / no-match / partial entity / multi-template precedence
- `test_predicates.py` — each predicate in isolation, all 6
- `test_yaml_schema.py` — valid templates pass, malformed templates raise with line numbers
- `test_entity_resolver.py` — known ticker, alias, ambiguous, unresolved
- `test_holdout_telemetry.py` — Prometheus counter increments on each drop class

**No pipeline integration.** Engine exists as standalone module. PR-2 wires it in.

**Memory + docs:**

- `deploy/monitoring/prometheus/rules/alphalens.yaml` — add `AlphalensTemplateMatchRateLow`
- Grafana panel JSON for template match rates

### PR-2 — Hybrid pipeline integration

**Modified:**

- `event_extractor.py` — entry point now calls `TemplateEngine.match()` first; falls back to existing DeepSeek Flash extract on no-match
- Parquet schema additions: `extraction_method ∈ {template, flash}`, `template_id` (nullable)
- `catalyst_resolver.py` — precedence rule (template > flash for same `(ticker, event_type)` in 24h)

**Tests:**

- `test_hybrid_extraction.py` — template-match path, flash-fallback path, precedence rule, audit-trail columns
- `test_catalyst_resolver_precedence.py` — template+flash co-existence resolution

**No new templates.** Pipeline now uses PR-1's 5 templates end-to-end.

### PR-3 — Structured facts → generator

**Modified:**

- `argumentation/generator.py` — generator receives `template_facts: dict | None` field; when present, prompt instructs LLM to cite facts deterministically (extends `feedback_llm_training_cutoff_numerical_data` doctrine to article-derived facts)
- Brief schema additions: `template_facts` rendered as inline citations in SPA evidence panel

**Tests:**

- `test_generator_typed_facts.py` — generator never invents amounts when `template_facts` provided; Pro / Flash both respect contract
- Playwright smoke for SPA panel rendering typed facts vs free-text fallback

### PR-4 — Multi-source dedup via template tuples

**New:**

- `dedup.py` — same `{template_id, primary_entity_set, key_attributes}` across multiple sources within 24h → single event with `dedup_count` + `source_urls: list`
- Ingest-stage clustering (today: same-day lexical Jaccard) extended with semantic-tuple dedup for template-extracted events

**Tests:**

- `test_template_dedup.py` — same M&A reported by 10 outlets → 1 event with `dedup_count=10`

**This is where Track H "multi-source dedup" lands.**

### PR-5 (stretch) — Compound catalyst sequences

**New:**

- `compound.py` — detects sequences like M&A→financing→analyst on same ticker in 30d window; emits `CompoundCatalystEvent` with constituent events
- Scoring hook: `compound_catalyst_strength` signal added to `screening/scorer.py`

**This is where Track G "validated paradigm scorer reuse" extends to news-driven compound catalysts** — Cohen-Malloy insider scorer + FCFF yield scorer + compound catalyst sequence become a 3-signal corroboration.

**Decision gate:** ship PR-5 only after ≥1 month of PR-2 telemetry confirms that ≥5% of articles trigger same-ticker template matches within 30d windows. If lower, the compound-catalyst hypothesis is data-starved and PR-5 stays deferred.

---

## §4. Risks + open questions

| Risk | Mitigation |
|---|---|
| Template authoring drift — templates get fork'd by analyst without re-testing | `alphalens templates validate` as pre-commit hook; CI fails on invalid YAML |
| Entity resolver false positives ("Apple" → fruit) | Alias table seeded with common gotchas; resolution outcome telemetry tracks ambiguous mentions |
| Hybrid pipeline complexity creep | 20% rule: if > 20% templates need custom Python escape, migrate to Python-only templates entirely |
| Pre-template entity resolution becomes bottleneck | Resolver cached LRU; benchmark in PR-1 tests |
| Template-match rate stays low forever (< 40%) | Acceptable — hybrid handles. If under 20% on a template for 7d, alert fires; analyst either fixes pattern or deletes template |
| Compound-catalyst PR-5 ships then never fires | Decision gate at PR-5 launch: ≥5% same-ticker window-match rate required |

**Open questions to revisit at PR-5 decision:**

- Does the 24h precedence window suffice, or does some event_type (e.g. financing announcements followed by analyst rerating) need a longer overlap window?
- Should `template_facts` flow into the trade_setup calculation (e.g. acquisition premium → entry ladder offset)? Currently trade_setup is a pure deterministic ladder; adding template-derived bias is a separate design discussion.
- Does NL→DSL workflow (analyst describes event in prose, DeepSeek v4 Pro generates YAML draft) add value, or is direct YAML authoring fast enough? Decision gated on template-library size growth velocity.

---

## §5. Why this is NOT a v2 rewrite

Per ideal-shape doc: "To **nie** jest 'v2 rewrite'. To kierunek w którym AlphaLens już idzie, PR po PR."

This memo describes a foundation feature that fits the existing pipeline structure:

- News ingest stage — unchanged
- Extract stage — gains a deterministic path; LLM path retained for fallback
- Catalyst resolver — gains a precedence rule; algorithm otherwise unchanged
- Verification gates — unchanged
- Scoring — gains optional `compound_catalyst_strength` in PR-5; otherwise unchanged
- Brief generation — gains `template_facts` injection in PR-3; LLM contract otherwise unchanged

The existing 39-class event_type enum stays. The existing 4-signal scorer stays. The existing brief schema stays. Templates **augment** Flash extraction — they do not replace the broader pipeline.

---

## §6. References

- **Issue #143** (this design memo replaces the deferral note)
- **Companion PR-G #318** (DeepSeek v4 swap) — required dependency: hybrid mode uses the canonical OpenRouter client for the Flash fallback path
- **Ideal-shape doc** `docs/research/alphalens_ideal_shape_2026_05_29.md` — Track H extension in §6 + roadmap insert in §8
- **`feedback_llm_training_cutoff_numerical_data_2026_05_17`** — extended doctrine: typed facts come from authoritative sources; templates extend this to article-derived facts
- **`feedback_validated_paradigm_scorer_reuse_2026_05_16`** — Track G enabler for PR-5
- **DeepSeek v4 Pro zen review** (continuation id `4e44c29d-2447-4903-98a4-df6f959d1218`) — full architectural critique
- **Perplexity Research** (45 sources, see `/tmp/perplexity_research.md`) — RavenPack / Refinitiv / GDELT / ICEWS / FANAL / ASEE / Plumber / Liberal Event Extraction industry survey

## §7. Edits log

| Date | Change | Reason |
|---|---|---|
| 2026-05-30 | Memo created + LOCKED | Replaces #143 deferral; both reviewers converged on hybrid + YAML+predicates; user-affirmed velocity supports immediate PR-1 work |
