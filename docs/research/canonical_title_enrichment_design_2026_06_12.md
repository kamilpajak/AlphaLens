# Canonical publisher-title enrichment for brief events

**Status:** DRAFT — awaiting GO before implementation
**Date:** 2026-06-12
**Author:** session (thematic pipeline)
**Related:** [[project_multi_exchange_news_lake_design_2026_06_05]] (the bitemporal news-lake redesign; this is a smaller, orthogonal display-quality fix), PR #528 (HTML-entity decode at ingest — a different defect class)

## 1. Problem

GDELT's DOC API returns **mangled titles**: it strips em-dashes and apostrophes
(transliterates to ASCII). Observed on the `/edge`-adjacent brief dashboard
(brief 2026-06-09, NVAX/PCVX):

- Stored: `Scientists are fast-tracking 3 Ebola vaccines in hopes of shortening the outbreak when could they be ready?`
- Real publisher headline (livescience): `... the current outbreak — when could they be ready?`

The em-dash between `outbreak` and `when` is gone, so the title reads as broken
English. A second title in the same GDELT batch
(`Three New Ebola Vaccines Are in The Works. Here The Science Behind Them.`)
shows GDELT also dropped the apostrophe-s from `Here's` — systematic punctuation
loss.

**Root cause is upstream, NOT our code.** Evidence:
1. The raw per-source lake (`thematic_news_lake/session_date=2026-06-09`,
   `source=gdelt`) already carries the dash-less title with zero chars > U+007F.
2. The lake title only passes through `gdelt.transform`→`clean_title` and
   `news_ingest._decode_title_entities`. None can remove an em-dash
   (`clean_title` regexes target ASCII space-padding; `html.unescape` only
   decodes entities). So the dash was absent in GDELT's HTTP response.

The dash cannot be reconstructed from GDELT's data. The only correct fix is to
take the **canonical publisher title** (`og:title` / `<title>`) from the event's
own URL.

## 2. Goal & non-goals

**Goal:** When a brief event is finalized, replace its display title
(`source_event_title`) with the publisher's canonical `og:title` fetched from
`source_event_url`, falling back to the existing title on any failure. This
fixes GDELT-mangled titles AND gives the authoritative publisher headline
instead of GDELT's rewrite, for all sources uniformly.

**Non-goals:**
- Not a bitemporal/PIT redesign — that is [[project_multi_exchange_news_lake_design_2026_06_05]].
- Not enriching the 200-item news cache — only the ~14 selected brief events.
- Not changing theme matching / scoring — title is display + LLM-citation only
  (see §5, confirmed display-only at Phase E).

## 3. Why Phase E (brief generation), not Phase C/ingest

The displayed `source_event_title` in the brief parquet comes from the input
Phase D row (`verified`), carried through the final
`verified.merge(enrichment, on="ticker")` in
`argumentation/orchestrator.py::generate_briefs` (line ~471). The same value is
read by `_row_to_facts` (line 156) for the LLM catalyst citation.

Enriching at Phase E, on the `verified` DataFrame (the ~14 selected rows), is:
- **Cheap** — ~14 fetches/run × 6 runs/day ≈ 84/day, spread across many
  publisher domains (no single-domain hammering). The 200-item news cache is
  never fetched.
- **Single integration point** — one pass over `verified["source_event_title"]`
  near the top of `generate_briefs`, BEFORE the per-row loop. Covers both the
  LLM citation (`_row_to_facts`) and the output column (`merge`) at once.
- **Contemporaneous** — the pipeline runs same-day, so the first fetch is near
  publication; cached by URL thereafter (frozen, no drift).

## 4. Design

### 4.1 New module `thematic/sources/canonical_title.py`

```
DEFAULT_CACHE_DIR = ~/.alphalens/og_title_cache/
_CACHE_TTL_DAYS    = 180     # publisher titles are near-immutable
_TITLE_MAX_LEN     = 200     # mirror catalyst_resolver._TITLE_MAX_LEN
_TIMEOUT_S         = 12

def fetch_og_title(url, *, cache_dir=DEFAULT_CACHE_DIR, fetcher=_default_fetcher,
                   ttl_days=_CACHE_TTL_DAYS) -> str | None:
    """Return the publisher og:title/<title> for url, or None on miss/failure.
    Tri-state cache (mirrors verification/tenk_grep.fetch_10k_text):
      hit  -> read cached .txt
      miss -> fetch -> extract -> validate -> cache -> return
      fail -> return None (NOT cached as negative; retried next run)
    """

def canonical_title_for(url, fallback, **kw) -> str:
    """og:title if it passes validation (§4.3), else fallback. Never raises."""
```

- **Cache key:** `sha1(url)` → `{hash}.txt` under `og_title_cache/`. URL-keyed
  (not date/ticker) — the URL is the identity. Cached file is the validated
  title string; absence = not-yet-fetched. TTL via file mtime
  (re-fetch after 180d), mirroring `tenk_grep`.
- **Negative results are NOT cached** — a transient fetch failure must not
  poison the URL forever (lesson from the SEC 403 cache-poisoning incident,
  PR #386). On failure `canonical_title_for` just returns `fallback` this run
  and retries next run.

### 4.2 Fetch + extract

- `_default_fetcher(url) -> str`: `requests.get` with a descriptive UA, 12s
  timeout, `raise_for_status()` (mirrors `data/universes/ishares_refresher`).
  Injectable so tests never hit the network.
- Extraction order (BeautifulSoup, `html.parser` — already a dep via
  `verification/tenk_grep`):
  1. `<meta property="og:title" content="...">`
  2. `<meta name="twitter:title" content="...">`
  3. `<title>...</title>`
- `html.unescape` + whitespace-collapse the result; truncate to `_TITLE_MAX_LEN`.

### 4.3 Validation guard (critical — avoids replacing with junk)

`og:title` from a bot-challenge / paywall page is junk ("Just a moment...",
"Are you a robot?", a bare site name). Replace ONLY when the candidate:

1. is non-empty, stripped length ∈ [12, 300];
2. does NOT match a junk denylist (case-insensitive substring): `just a moment`,
   `are you a robot`, `access denied`, `attention required`, `403 forbidden`,
   `please enable`, `bot detection`, `captcha`;
3. shares **≥ 2 content tokens** (lowercased, length > 3, non-stopword) with the
   original GDELT/source title — confirms it is the same article even when the
   publisher reworded it.

> Guard #3 rationale: the publisher headline can be a full reword
> (livescience `3 new Ebola vaccines are being fast-tracked amid the current
> outbreak` vs GDELT `Scientists are fast-tracking 3 Ebola vaccines in hopes of
> shortening the outbreak`). Jaccard ≥ 0.6 (`text_similarity.titles_similar`)
> would reject this true match. A ≥2 shared-content-token rule accepts the
> reword (shares `ebola`, `vaccines`, `outbreak`, `fast`, `tracked/tracking`,
> `ready`) while rejecting an unrelated junk page (shares nothing).

If any check fails → keep `fallback`. The guard makes uniform application across
all sources safe: EDGAR press-release URLs (sec.gov index pages, no real
`og:title`) and clean Polygon/RSS titles naturally fall back to their existing
(good) title.

### 4.4 Integration in `generate_briefs`

Near the top of `generate_briefs`, after `verified` is materialized and before
the per-row loop:

```python
verified = _enrich_event_titles(verified)   # in-place column replace, best-effort
```

`_enrich_event_titles(df)`: for each row with a non-empty `source_event_url`,
`df.at[i, "source_event_title"] = canonical_title_for(url, fallback=current)`.
Empty-url rows and empty df are no-ops. Wrapped so one row's failure never
aborts the batch (per-call try/except already inside `canonical_title_for`).

A module-level flag `ENABLE_CANONICAL_TITLE` (default True) lets the operator
disable network enrichment without a code change (env `ALPHALENS_CANONICAL_TITLE=0`).

## 5. Safety / blast radius

- **Display + citation only.** Title does not feed theme matching or scoring
  (those run upstream on `rationale`/`theme`/score). Confirmed: the only
  consumers of `source_event_title` are `_row_to_facts` (LLM catalyst citation)
  and the output parquet column. Changing it cannot move which tickers surface.
- **Best-effort, fail-closed to current behavior.** Any fetch/parse/validation
  failure keeps today's title. With `ENABLE_CANONICAL_TITLE=0` or no network,
  the pipeline is byte-identical to pre-change.
- **No vendor-quota impact.** Fetches hit arbitrary publisher domains via a
  generic `requests` call — NOT routed through (and not counted against) the
  SEC/AV/OpenRouter/Polygon canonical clients. The `test_no_raw_*_http`
  enforcement tests are vendor-URL-specific and will not flag generic publisher
  fetches (verify during impl).
- **Cache poisoning guarded** — negatives not cached; only validated titles
  written.

## 6. Risks & open questions

- **R1 — publisher reword diverges from the cited event.** The LLM prompt says
  "cite the catalyst factually"; if og:title rewords heavily, the citation text
  shifts but stays the same article (guard #3). Acceptable: the URL is
  unchanged and authoritative.
- **R2 — fetch latency in the 6×/day pipeline.** ~14 sequential 12s-timeout
  fetches worst-case ≈ 3 min added; typical < 20s. Bounded by `TimeoutStartSec=45min`
  on the unit. Could parallelize later if needed; start sequential + cached.
- **R3 — politeness / robots.** Low volume, many domains. Use a clear UA; do
  not retry aggressively (one attempt per run, cached). No robots.txt parser in
  v1 — revisit if a publisher complains (single-user tool).
- **R4 — paywalled pages** return a generic/teaser og:title. Guard #3 keeps the
  original when tokens don't overlap; otherwise the teaser title is still the
  publisher's canonical headline. Acceptable.

## 7. Test plan (TDD)

New `apps/alphalens-research/tests/thematic/test_canonical_title.py`:
- `fetch_og_title`: og:title extracted; twitter:title fallback; `<title>`
  fallback; entity-decode + whitespace-collapse; truncation at 200.
- cache: hit returns cached without calling fetcher; miss writes cache;
  failure returns None and writes NO cache file (no poisoning); TTL re-fetch.
- validation guard: junk denylist rejected; too-short rejected; <2 shared
  tokens rejected (→ fallback); reworded same-article accepted (the Ebola case
  as a pinned fixture).
- `canonical_title_for`: returns fallback on fetcher raising; never raises.

Extend `tests/thematic/argumentation/test_orchestrator.py`:
- `_enrich_event_titles` replaces title when og:title valid; keeps original on
  empty url / fetch fail; no-op on empty df; `ENABLE_CANONICAL_TITLE=0` disables.
- end-to-end `generate_briefs` with an injected fetcher: output parquet
  `source_event_title` carries the canonical title AND the LLM facts see it.

All tests inject the fetcher — no live HTTP. One opt-in live probe under
`tests/live/` (`CANONICAL_TITLE_LIVE_TEST`) hitting a stable URL, shape-only.

## 8. Rollout

1. Implement + TDD green; ruff + no-polish; zen pre-merge (deepseek-v4-pro,
   thinking=high — shared pipeline surface).
2. Merge → rebuild VPS-local `alphalens-pipeline:latest` image (forward path).
3. Backfill the already-stored mangled titles is **out of scope for v1** — the
   og:title cache is empty, and re-running `generate_briefs` for past dates
   would re-fetch + re-LLM. If desired later, a one-shot `enrich-titles`
   command can rewrite `source_event_title` in existing brief parquets using
   `canonical_title_for` (no LLM re-run) + `rebuild_briefs_cache`. Note this as
   a follow-up.
```
