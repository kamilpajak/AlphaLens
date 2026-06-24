# Perplexity as a 5th thematic news source

**Status:** LOCKED 2026-06-24
**Type:** live-pipeline feature (flag-gated, default off), forward-only
**Author:** session brainstorm 2026-06-24

## 1. Motivation

The user values Perplexity's edited, multi-source news (the `perplexity.ai/discover`
feel — a story synthesized from 20-40 sources). Those stories often centre on
mega-caps and macro/geopolitical events. We want them as **input** to the existing
thematic pipeline, exactly like GDELT / RSS / Polygon / EDGAR, and processed by the
**existing** DeepSeek `map-themes` logic — no new beneficiary-naming prompt.

A prior one-shot experiment (merged, `discover_lane`) asked Perplexity to name
beneficiaries directly; that skewed to mega-caps and produced near-zero overlap with
the brief (median mcap $50-206B vs $2-4B). The decisive follow-up finding explains why
that does not matter for this design: the thematic pipeline proposes candidates from
the extracted **theme string**, not from the article's tickers or text
(`theme_mapper.propose_candidates(theme)` sees only the theme label). So a Perplexity
mega-cap article can surface small/mid-cap beneficiaries — the existing mcap-filter and
theme-mapper do that. Perplexity's job is only **event/theme detection**.

A two-day spike confirmed the editorial value is real on content-rich days: a broad
"top-stories" prompt for a weekday (2026-06-12) returned 10 distinct, reputable,
net-new events (M&A, a Chapter 11 + merger, an exchange delisting, sector moves) drawn
across 29 sources — events the brief's narrower themes (fda/gold/ios/space) missed. A
weekend day (2026-06-14) returned one macro story rephrased from weaker sources; that
thin-day behaviour is accepted, not engineered away (see §10).

## 2. Decisions locked during brainstorm

| Decision | Choice |
|---|---|
| Role of Perplexity | Event/theme **detection** only; beneficiaries come from existing `map-themes` |
| Integration | A 5th news **source** in the live ingest, like GDELT/RSS/Polygon/EDGAR |
| Source scope | Broad "top-stories" of the day |
| Source curation | **Do not interfere with Perplexity's sources** — no domain filter, no "avoid blogs" steering |
| Rollout | Live + **flag-gated (default off)** + provenance via the existing `source` field |
| Cost control | Cache the raw response per date (6×/day reruns hit cache) |
| Provenance depth | Approximate (via catalyst `source`); exact per-candidate provenance deferred |

## 3. Architecture & location

Live-pipeline code, NOT the research `discover_lane` package (that stays the merged
research artifact).

```
apps/alphalens-pipeline/alphalens_pipeline/thematic/sources/perplexity.py   # new adapter
apps/alphalens-pipeline/alphalens_pipeline/thematic/news_ingest.py          # register + flag-gate
```

The adapter implements the same contract as the other sources:
`fetch_daily_news(*, date: dt.date, client=None) -> pd.DataFrame` conforming to
`NEWS_COLUMNS`. It calls the canonical `PerplexityClient.ask_with_citations` (merged in
the discover-lane work). DI seam: an optional `client` parameter (for tests) defaults
to a module-level factory that reads `PERPLEXITY_API_KEY` — consistent with the
"injected client, else `get_default_*`" doctrine.

## 4. Prompt & fetch (no source interference)

A broad top-stories prompt asking for 8-12 **distinct** stories of the day (deals/M&A,
regulation, geopolitics, supply chain, sector moves, index changes), each as
`{headline, summary, url}`. It contains **no** beneficiary request, **no** numeric/price
targets, and **no** source filtering or "avoid blogs/Reddit" language — Perplexity
curates its own sources.

PIT date filters: `search_after = date - 1d`, `search_before = date + 1d` (MM/DD/YYYY),
`search_context_size="high"`. Defensive parse reuses the discover-lane pattern
(code-fence strip + `json.JSONDecoder().raw_decode`, malformed entries skipped, never
raises).

## 5. NEWS_COLUMNS mapping

| Column | Value |
|---|---|
| `id` | `perplexity:{sha1(url or headline)}` (stable) |
| `source` | `"perplexity"` |
| `timestamp` | `date` at a fixed UTC time (valid-time) |
| `tickers` | `[]` — beneficiaries come from `map-themes`, not from Perplexity |
| `title` | headline |
| `body` | summary (the synthesis) |
| `url` | representative url |
| `keywords` | `[]` |
| `extra` | JSON `{"citation_count": N, "citations": [...]}` (carries the "N sources" signal) |
| `ingested_at` | now (UTC) |

## 6. Registration in `news_ingest.py` + flag

- `_SOURCE_PRIORITY`: add `perplexity = 4` (lowest for dedup-representative — when a
  primary source carries the same story, the primary URL wins; Perplexity mainly
  contributes stories no other source had).
- `_SOURCE_QUOTA_WEIGHTS`: rebalance to sum 1.0 —
  `edgar 0.25 / polygon 0.25 / gdelt 0.20 / rss 0.15 / perplexity 0.15`.
- **Flag** `ALPHALENS_PERPLEXITY_SOURCE` (env, default off): when off, the source is not
  fetched at all (zero production change). Enabling is a separate ops step.
- The fetch is wrapped in the existing `_safe_call`, so a source error is swallowed (the
  day gets no Perplexity rows; the pipeline continues).

## 7. Cache (cost bound under 6×/day cadence)

The raw Perplexity response is cached per date at
`~/.alphalens/thematic_news_perplexity/{date}.json` **before** processing. The VPS
`thematic-build` runs 6×/day; reruns for the same date read the cache (free). Cost ≈ one
Perplexity call per calendar date.

## 8. Provenance / measurement (no schema change)

The `source="perplexity"` field already flows into the news lake and into
`catalyst_resolver` → `source_event`. Net-new contribution is measurable from existing
data by a separate read-only diagnostic: per day, themes contributed only by Perplexity
vs other sources, and candidates whose `source_event` source is `perplexity`. No parquet
or Django schema change (that was Approach 2, deferred).

## 9. Doctrine compliance

- Canonical client (`ask_with_citations`); no shadow client (`test_no_raw_perplexity_http`
  stays green).
- No numbers from the LLM — the prompt asks only for headline/summary/url; themes and
  candidate tickers come from the existing pipeline.
- Catalyst gate: Perplexity is a non-GDELT source → entity-less events pass
  unconditionally (no `domain`/`sourcecountry` state-media arm applies).
- English-only in code; emits to the news lake like the other sources.
- `PERPLEXITY_API_KEY` is already present in the VPS container (used by buffett
  qual-enrich).

## 10. Error handling

| Failure | Behaviour |
|---|---|
| Perplexity call fails / times out | `_safe_call` swallows it; the day has no Perplexity rows |
| Malformed / non-JSON response | defensive parse returns an empty DataFrame |
| Flag off | source not fetched; client never constructed |
| `PERPLEXITY_API_KEY` missing while flag on | log a warning, return empty; do not break ingest |

## 11. Known issues / deliberate scope cuts

- Thin / weekend days yield little and may draw weaker sources (the 2026-06-14 case);
  accepted — we do not interfere with Perplexity's sources.
- Provenance is approximate (via the catalyst `source`), not exact per-candidate;
  deferred (Approach 2).
- No `search_domain_filter` (explicit decision: do not interfere with sources).
- The macro→small-cap inferential leap remains a selection-quality risk; it is measured
  forward via EDGE, and does not block this integration.

## 12. Testing (TDD, red → green)

- `fetch_daily_news` with a mocked `PerplexityClient` → correct `NEWS_COLUMNS` rows
  (stable `id`, `source="perplexity"`, `tickers=[]`, `body=summary`,
  `extra.citation_count`).
- Defensive parse: malformed / empty response → empty DataFrame, no exception.
- Registration: flag on → the Perplexity source is fetched and its rows appear; flag off
  → the source is not fetched (the mocked client is never called) and no rows appear.
- Date-filter arithmetic (MM/DD/YYYY, brackets the date).
- `_SOURCE_QUOTA_WEIGHTS` sums to 1.0.
- `test_no_raw_perplexity_http` stays green (routed through the client).

## 13. Deployment

Code ships in the `alphalens-pipeline` image. The flag defaults off, so deploy is a
no-op behaviourally. Enabling is a separate ops step: set `ALPHALENS_PERPLEXITY_SOURCE=1`
in the VPS thematic-build unit environment, then the next `thematic-build` slot begins
fetching Perplexity stories. `PERPLEXITY_API_KEY` is already in the container.
