# Discover-lane experiment — parallel Perplexity-driven candidate generation

**Status:** LOCKED 2026-06-24
**Type:** one-shot research experiment (RESEARCH_ONLY), no live-system changes
**Author:** session brainstorm 2026-06-24

## 1. Motivation

The user likes Perplexity **Discover** (`perplexity.ai/discover`): editorial market-news
cards, each synthesized from 20–46 sources ("29 źródeł"). Discover is a **consumer
feature with no public API** — those pre-curated cards cannot be fetched. The same
multi-source synthesis is, however, reproducible through Perplexity's **Sonar chat
completions** API (the engine `PerplexityClient` already calls).

The thematic pipeline today discovers candidates from GDELT / RSS / Polygon / EDGAR.
On 2026-06-23 the selection-edge diagnosis found the current screener has **no
conditional edge** and the lever is *which names get picked*. That makes a
**parallel challenger lane** — "what would a Perplexity-Discover-style process pick?" —
genuinely useful, not just a UI toy.

This experiment answers one question: **for recent brief dates, how does a free
Discover-style Perplexity candidate list compare, side-by-side, to the real brief?**
We expect to *see* whether Perplexity skews to mega-caps (NVDA-class) while the
pipeline finds small/mid-caps, and how much the two lists overlap.

It is a one-shot look over a few dates. It does **not** touch the live pipeline,
Django, the web app, or the EDGE attribution machinery, and it does **not** feed
selection. Those are explicit non-goals (§8).

## 2. Decisions locked during brainstorm

| Decision | Choice |
|---|---|
| Role of Perplexity | Generate its **own** candidates in a parallel lane (not score/gate the existing ones) |
| Longevity | **One-shot experiment** over a few recent dates; no standing daily lane, no EDGE logging |
| Generation mechanism | **Free Discover-style** — Perplexity surfaces hot stories freely, then names beneficiaries |
| Output | Self-contained **side-by-side HTML report** (Perplexity column vs real brief column per date) |
| Hosting | Research-side; zero changes to live pipeline / Django / web |

## 3. Architecture & location

Testable logic lives in a package so the diff-coverage ≥80% gate can see it
(scripts are excluded from coverage); the thin CLI orchestration + HTML rendering
lives in the script.

```
apps/alphalens-research/alphalens_research/discover_lane/   # __status__ = "RESEARCH_ONLY"
    __init__.py        # status constant
    models.py          # DiscoverCandidate, AskResult shapes used here
    prompt.py          # builds the Discover-style Perplexity prompt
    parse.py           # Perplexity JSON content -> list[DiscoverCandidate] (defensive)
    enrich.py          # ticker resolve + mcap via canonical YFinanceClient; flags
    compare.py         # discover candidates x real brief candidates -> overlap stats

apps/alphalens-research/scripts/discover_lane_experiment.py  # CLI glue + HTML render
```

**Canonical-client extension (pipeline side):**
`alphalens_pipeline/literature_scanner/perplexity_client.py` gains a new **additive**
method `ask_with_citations(query, ...) -> AskResult(content, citations, search_results)`.
The Sonar response already returns `citations` / `search_results`; the current
`ask()` discards them. `ask()` is left unchanged (literature_scanner depends on it).
No shadow client — the `test_no_raw_perplexity_http` seam is preserved.

**Dependency direction:** `research -> pipeline` only (`YFinanceClient`,
`PerplexityClient`). No import of live thematic/Django/web. Conforms to ADR 0011 DAG;
`test_module_dependencies.py` and `test_layer_status.py` both stay green.

## 4. Components

### 4.1 `models.py`
```python
@dataclass(frozen=True)
class DiscoverCandidate:
    ticker: str
    company: str
    theme: str
    rationale: str                 # Perplexity one-liner, web-grounded TEXT only
    citation_count: int            # the "N źródeł" trust signal
    citation_urls: list[str]
    source_event_title: str
    source_event_url: str
    mcap: float | None             # from yfinance, NEVER from the LLM
    resolved: bool                 # ticker resolved to a real US-listed equity
    in_pipeline_universe: bool     # ticker is in the thematic pipeline universe
```
`AskResult` (content, citations, search_results) mirrors what the client returns so
`parse.py` is independently testable.

### 4.2 `prompt.py`
Builds a Discover-style prompt: "As of {date}, what were the most significant
market-moving news stories affecting US-listed equities? For each story, name the
specific US-listed beneficiary companies (ticker + name), a one-sentence reason, and
the triggering event." Output constrained to a JSON schema (array of stories →
beneficiaries).

**Doctrine guards:**
- No numeric/bracket constraints in the prompt (no market-cap / P/E / volume bands) —
  filtering happens in Python post-hoc (LLM-training-cutoff doctrine).
- The prompt asks only for names + reasoning + event, never for numbers.

### 4.3 `parse.py`
Parses the JSON content into `list[DiscoverCandidate]` (mcap/resolved/in_universe left
unset at this stage). Defensive: non-JSON output, missing fields, or a malformed entry
→ that entry is skipped and logged; the run continues.

### 4.4 `enrich.py`
For each parsed candidate: resolve the ticker and fetch `mcap` via the canonical
`YFinanceClient`. Sets `resolved` (yfinance returned a real instrument) and
`in_pipeline_universe` (ticker ∈ thematic pipeline universe). Unresolvable tickers are
kept with `resolved=False` (rendered greyed) — we do **not** guess or drop silently,
so we can see exactly what Perplexity proposed.

### 4.5 `compare.py`
Given the enriched discover candidates and the real brief candidates for a date,
computes: shared tickers, Perplexity-only, brief-only, and the median/quartile mcap
distribution per side. Pure function over two lists — no I/O.

## 5. Data flow + PIT pinning

For each date `D` (default: the latest `N` dates that have a brief on disk):

1. Load the real candidates from `~/.alphalens/thematic_briefs/{D}.parquet` (disk read,
   no Django). Fields used: ticker, company_name, theme, source_event_title, market_cap.
2. Call `ask_with_citations` with **date filters** `search_after_date_filter = D − 7d`
   and `search_before_date_filter = D`, to cut look-ahead. Without PIT pinning,
   Perplexity would reason from *today's* knowledge about a past date and the comparison
   would be meaningless.
3. `parse` → `enrich` (mcap from yfinance, never from the LLM) → `compare`.
4. **Cache the raw Perplexity response** to
   `~/.alphalens/discover_lane_experiment/cache/{D}.json` **before** processing, so
   iterating on the HTML never re-pays the API (project caching doctrine).

**Known limitation (to validate at implementation):** if the chat-completions date
filters prove too coarse for strict PIT, the fallback is to run only on the *latest*
brief dates (look-ahead minimal) and document it. This is recorded here rather than
discovered later.

## 6. Output — HTML report

A single self-contained HTML file (inline CSS, no framework) at
`~/.alphalens/discover_lane_experiment/report_{generated_stamp}.html`
(runtime data, outside the repo). The CLI script stamps the filename with the
generation time; the pure logic modules take any timestamp as an argument so they
stay deterministic under test.

Per date, two columns: **Perplexity-Discover** vs **Brief**. Each card shows
`ticker · company · mcap · theme · rationale · "N źródeł"` (with citation links) and
badges `[in-universe?] [also-in-brief?] [unresolved?]`. Each date header carries an
overlap bar, e.g. "Perplexity 8 · brief 3 · shared 1 · median mcap P=42B vs B=2.1B".

## 7. Error handling

| Failure | Behaviour |
|---|---|
| Perplexity output not JSON / entry malformed | skip the entry, log, continue |
| yfinance fail / timeout | `mcap=None`, `resolved=False`, card rendered greyed |
| no brief parquet for a date | skip that date with a warning |
| `PERPLEXITY_API_KEY` missing | fail loud at startup |

## 8. Non-goals (YAGNI / explicit scope cuts)

Out of scope: gates, buffett/oneil/score, EDGE logging, Django changes, web/SPA
changes, a standing daily lane, capital decisions. The experiment is purely:
*discover → mcap → side-by-side HTML, a few dates, once.*

## 9. Testing (TDD, red → green)

- `parse.py`: a recorded Sonar JSON content (+ malformed/partial variants) → expected
  `DiscoverCandidate` list.
- `compare.py`: two candidate lists → correct shared / only-sets / mcap stats.
- `PerplexityClient.ask_with_citations`: a cassette of a Sonar response →
  `AskResult` with citations/search_results parsed correctly.
- `enrich.py`: mocked `YFinanceClient` → correct `resolved` / `in_pipeline_universe`
  flags; unresolved ticker kept, not dropped.
- HTML render: smoke test (output is valid HTML and contains the expected tickers).
- `__init__.py` `__status__` is auto-discovered by the existing `test_layer_status.py`.

## 10. Cost

Sonar-pro: per-request fee + tokens, roughly a few cents to ~$0.10 per date at high
context. `N` small (3–5). Raw-response cache means re-renders are free. Negligible.
