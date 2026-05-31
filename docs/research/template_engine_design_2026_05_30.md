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

**Important — don't panic early on aggregate rate:** with only 5 templates dnia jeden against a 39-class event_type enum on a 200-article/day feed, the aggregate `template_match_rate` will sit in the **20-35% range** for the first several months. That is the **expected coexistence baseline**, not failure. The aggregate threshold table becomes directional only after the template library reaches ~10+ templates. The actionable alert is per-template (§2.4): `AlphalensTemplateMatchRateLow` fires when a *specific* template drops below 20% over 7d — that catches pattern rot in one template without burning attention on the expected steady-state aggregate.

---

**EMPIRICAL CORRECTION (2026-05-31) — corpus assumption falsified.**

The 20-35% projection above rested on an **implicit assumption** that the existing news ingestion stream (GDELT + RSS + Polygon News API) would carry direct issuer press releases at a meaningful rate. First production deploy of the full epic (PRs #322 / #323 / #324 / #325) + a cache-bust re-extraction of the 2026-05-30 corpus measured:

- **200 articles ingested** (gdelt: 77, rss: 75, polygon: 48)
- **0/200 matched any template** (vs 20-35% projected)
- **0 articles from press-wire sources** (businesswire, prnewswire, globenewswire, accesswire)
- **0 IR-domain URLs** (`/investors/`, `/ir/`)
- **0 titles containing "press release"**
- **6 URLs cited a press-wire domain** as a link inside the article body (not the source itself)

Direct TemplateEngine trace on the two `event_type="m_and_a"` rows in this corpus confirmed the engine itself works correctly — it loaded all 5 ship YAMLs, EntityResolver resolved the named tickers (BRK.B/GOOG/GOOGL on a Motley Fool listicle), `match()` returned `None` *for the right reason* (the `is_press_release` predicate rightly rejects third-party commentary on M&A activity, which is the surface the predicate was designed to gate).

**Root cause:** the current ingest stream is composed entirely of *news aggregators* (GDELT indexes Reuters/Bloomberg/AP type sites; RSS pulls general financial news like Fool / MarketWatch; Polygon News API redistributes that same aggregator content). None of the three sources pull *direct issuer press releases*. The template engine is architecturally correct and production-deployed but **dormant** because of an upstream corpus gap, not an engine defect.

**Resolution path (LOCKED post-review):** ingest SEC EDGAR 8-K Exhibit 99.1 as the press-release source. Rationale:

1. Every materially important US public-company press release is *legally required* to be filed as Exhibit 99.1 to an 8-K under Items 1.01 / 2.01 / 2.02 / 7.01 / 8.01. Coverage of M&A / earnings / financing / regulatory_action events for the S&P 1500 + Russell 2000 universe is ~92-95% per cross-source survey.
2. The `edgar_detector` layer (live in production via `alphalens-edgar-detect.{service,timer}`) already polls EDGAR every 15 min for filing detection — zero new vendor surface, zero marginal cost, zero ToS risk (vs commercial press-wire RSS at >$3,000/yr minimum).
3. Press releases filed as 8-K exhibits are the issuer's own verbatim text, which is exactly the authoritative-source contract the template engine + `<template_facts>` prompt block was designed to enforce.
4. Both reviewers (zen `deepseek/deepseek-v4-pro` thinking=high + Perplexity Research sonar deep with 50 cited sources) converged independently on this path. Both explicitly rejected relaxing `is_press_release` to accept third-party financial commentary (would recreate the #143 Surfshark-listicle origin bug).

**Implementation slot:** new follow-up issue, scoped as `apps/alphalens-pipeline/alphalens_pipeline/thematic/sources/edgar_press_release.py` adapter emitting `Article` records with `source="edgar_press_release"`, plus a one-line extension to `is_press_release` allowlist treating that source as equivalent to the press-wire allowlist. Volume estimate: 35-45 earnings releases + 5-15 M&A announcements per day during active periods, well within existing ingestion capacity.

**Updated forecast:** after the EDGAR 8-K source lands, measure the new aggregate match rate over 7d. The 20-35% projection is provisionally retained as the target band, but with the understanding it is now a *post-EDGAR-integration* forecast, not an as-shipped baseline. The current 0% measurement is the correct as-shipped baseline given the ingest gap. Per-template `AlphalensTemplateMatchRateLow` alert (§2.4) is intentionally NOT firing today, because every template's denominator (articles that passed `is_press_release`) is zero — the alert is gated on receiving any press-release input at all.

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

Measured as: `(templates requiring ≥ 1 custom Python predicate or post-process function) / (total templates)`. **Count-based, not line-of-code-based** — a template with a 40-line custom post-process counts the same as a template with a 2-line one. The metric is computable from the predicate registry: any predicate not in the canonical 6 (§2.3) flags its template as "custom-dependent". Templates referencing only canonical predicates contribute 0; templates referencing any ad-hoc predicate contribute 1. With 10-15 templates, 3 custom-dependent templates crosses the 20% line — bright-line trigger, not a debate.

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

**New CLI commands:**

- `alphalens templates validate [path]` — JSON Schema + regex compile + sample-text dry-run. Exits non-zero on any failure; suitable as pre-commit hook.
- `alphalens templates evaluate <corpus-parquet>` — runs the engine over an existing `~/.alphalens/thematic_news/*.parquet` corpus, emits holdout counters end-to-end, prints per-template match-rate summary to stdout. Makes PR-1 **independently demonstrable** (analyst can iterate on YAML + see effect on real corpus without waiting for PR-2 pipeline integration). Wires the telemetry path live so PR-2 inherits a known-good metric flow.

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
- **Same-window dedup guard at injection time** (10-line guard, NOT a dependency on PR-4): when multiple template events exist for the same `(ticker, template_id)` within the brief's 24h window, the generator receives only the richest-fields instance. Prevents the brief from being flooded with 6 duplicate `template_facts` payloads when M&A gets reported by 6 outlets. Full multi-source dedup is PR-4's job; this guard is the minimum needed to keep PR-3 brief quality intact without reordering.

**Tests:**

- `test_generator_typed_facts.py` — generator never invents amounts when `template_facts` provided; Pro / Flash both respect contract
- `test_generator_dedup_at_injection.py` — same `(ticker, template_id)` in 24h window collapses to richest-fields instance before reaching prompt
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

**Decision gate (revised per zen review):** ship PR-5 only after ≥ 1 month of PR-2 telemetry confirms BOTH:

1. **Quantitative:** ≥ **2%** of template-extracted articles share a ticker with at least one other template-extracted article within a 30d window. (5% was too high — at 200 articles/day × 30d × ~30% template match rate, hitting 5% same-ticker co-occurrence requires ~75 events sharing tickers, which the AAPL/NVDA/TSLA concentration might satisfy but is a fragile bar.)
2. **Qualitative:** ≥ **3 observed instances of distinct-event-type sequences** (e.g., `m_and_a → financing`, `earnings → guidance`, `guidance → analyst_action`) across ≥ 3 unique tickers within the same 30d window.

The qualitative gate matters because raw same-ticker co-occurrence is mostly "popular ticker gets multiple news items" (AAPL earnings + AAPL guidance = two independent events, not a sequence). Distinct-event-type sequences are the actual signal compound catalysts target. If the qualitative gate trips but quantitative does not, the data structure exists — ship PR-5 even if rare, because the scoring impact of a confirmed M&A → financing on one ticker outweighs 50 popular-ticker co-occurrences.

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

- Should the **30d compound detection window** in PR-5 be tuned per event-type pair? E.g., financing → analyst rerating typically spans 2-3 days, while M&A → regulatory_action (antitrust review) can span 6+ months. The 24h precedence window in §1.1 is unrelated — that resolves `(ticker, event_type)` duplicates regardless of source path, and different `event_type` values never collide on it.
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
| 2026-05-30 | Zen pre-merge review applied (5 clarifications) | (1) §1.1 added "don't panic early" note on aggregate match rate; (2) §1.2 clarified 20% rule is count-based formula; (3) PR-1 scope added `alphalens templates evaluate` CLI for standalone analyst iteration; (4) PR-3 scope added same-window dedup-at-injection guard so PR-3 doesn't need PR-4 to land first; (5) PR-5 gate revised — quantitative threshold dropped 5% → 2%, qualitative cross-event-type-sequence gate added; (6) §4 open-question reframed — 24h precedence and 30d compound are different windows |
| 2026-05-31 | §1.1 EMPIRICAL CORRECTION block added — corpus assumption falsified | First production deploy of all 4 PRs (#322/#323/#324/#325) + cache-bust re-extraction measured 0/200 template matches on 2026-05-30 corpus (vs 20-35% projected). Direct TemplateEngine trace confirmed the engine works correctly; root cause is upstream ingest gap (GDELT + RSS + Polygon News carry zero press releases). Both reviewers (zen deepseek-v4-pro thinking=high + Perplexity Research with 50 cited sources) converged on SEC EDGAR 8-K Exhibit 99.1 as resolution path — leverages existing `edgar_detector` infrastructure, zero marginal cost, ~92-95% coverage of M&A/earnings/financing/regulatory events for S&P 1500 + R2000. Both reviewers explicitly rejected relaxing `is_press_release` (recreates #143 Surfshark-listicle origin bug). Follow-up implementation scope locked: new `thematic/sources/edgar_press_release.py` adapter + one-line `is_press_release` extension. Updated forecast: 20-35% target band is now *post-EDGAR-integration*, not as-shipped baseline. |
