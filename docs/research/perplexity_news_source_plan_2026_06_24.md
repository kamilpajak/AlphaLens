# Perplexity News Source — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Perplexity edited "top-stories" as a 5th, flag-gated news source feeding the existing thematic `ingest → map-themes` pipeline.

**Architecture:** A new source adapter (`thematic/sources/perplexity.py`) emits `NEWS_COLUMNS` rows from a broad Perplexity top-stories call (via the canonical `PerplexityClient.ask_with_citations`), with `tickers=[]` so the existing `map-themes` proposes beneficiaries from the extracted theme. `news_ingest.py` registers it behind an env flag (default off) so production is unchanged until enabled.

**Tech Stack:** Python 3.13, unittest, pandas, the canonical `PerplexityClient` (already merged).

**Spec:** `docs/research/perplexity_news_source_design_2026_06_24.md`

## Global Constraints

- Numbers never come from the LLM: the prompt requests only `headline/summary/url`; tickers/themes come from the existing pipeline. `tickers` is always `[]`.
- Canonical client only: all Perplexity HTTP goes through `PerplexityClient` (`alphalens_pipeline.literature_scanner.perplexity_client`). No raw `requests` to `api.perplexity.ai`. Enforced by `test_no_raw_perplexity_http.py`.
- Do NOT interfere with Perplexity's sources: no `search_domain_filter`, no "avoid blogs/Reddit/prefer reputable" language in the prompt.
- Pipeline must not import research (`alphalens_research.*`) — the adapter is self-contained (its own defensive JSON parse; do NOT import `alphalens_research.discover_lane`). Enforced by `test_module_dependencies.py`.
- Flag `ALPHALENS_PERPLEXITY_SOURCE` (env): the source is fetched ONLY when it equals `"1"`. Default/unset = not fetched, client never constructed.
- PIT date filters: `search_after = date - 1 day`, `search_before = date + 1 day`, format `MM/DD/YYYY`. `search_context_size="high"`.
- Raw response cached per date at `~/.alphalens/thematic_news_perplexity/{date}.json` before processing.
- `NEWS_COLUMNS` order is fixed (see `thematic/sources/schema.py`): `id, source, timestamp, tickers, title, body, url, keywords, extra, ingested_at`.
- English-only in code/comments.
- Test invocation: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.<module> -v`

---

### Task 1: Perplexity source — pure helpers (prompt, parse, id)

**Files:**
- Create: `apps/alphalens-pipeline/alphalens_pipeline/thematic/sources/perplexity.py`
- Test: `apps/alphalens-research/tests/thematic/test_perplexity_source.py`

**Interfaces:**
- Produces: `build_prompt(date_iso: str) -> str`; `parse_stories(content: str) -> list[dict]` (each dict has `headline`, `summary`, `url`); `_stable_id(s: str) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-research/tests/thematic/test_perplexity_source.py
import unittest

from alphalens_pipeline.thematic.sources import perplexity


class TestPerplexityHelpers(unittest.TestCase):
    def test_prompt_has_date_and_no_source_steering(self):
        p = perplexity.build_prompt("2026-06-12")
        self.assertIn("2026-06-12", p)
        self.assertIn("JSON", p)
        low = p.lower()
        for banned in ("reuters", "bloomberg", "avoid blog", "reddit", "reputable", "price target", "market cap"):
            self.assertNotIn(banned, low)

    def test_parse_well_formed(self):
        content = (
            '{"stories": [{"headline": "SpaceX IPO", "summary": "Debut.", "url": "https://a.com"},'
            '{"headline": "Iran deal", "summary": "Oil falls.", "url": "https://b.com"}]}'
        )
        out = perplexity.parse_stories(content)
        self.assertEqual([s["headline"] for s in out], ["SpaceX IPO", "Iran deal"])
        self.assertEqual(out[0]["url"], "https://a.com")

    def test_parse_tolerates_trailing_prose_and_fence(self):
        content = '```json\n{"stories": [{"headline": "H", "summary": "S", "url": "u"}]}\n```\nHope this helps!'
        out = perplexity.parse_stories(content)
        self.assertEqual([s["headline"] for s in out], ["H"])

    def test_parse_skips_malformed_and_nonjson(self):
        self.assertEqual(perplexity.parse_stories("sorry, no json"), [])
        content = '{"stories": ["notadict", {"headline": "", "summary": "s", "url": "u"}, {"headline": "OK", "summary": "s", "url": "u"}]}'
        self.assertEqual([s["headline"] for s in perplexity.parse_stories(content)], ["OK"])

    def test_stable_id_deterministic(self):
        self.assertEqual(perplexity._stable_id("https://a.com"), perplexity._stable_id("https://a.com"))
        self.assertNotEqual(perplexity._stable_id("https://a.com"), perplexity._stable_id("https://b.com"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.test_perplexity_source -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'alphalens_pipeline.thematic.sources.perplexity'`

- [ ] **Step 3: Write minimal implementation**

```python
# apps/alphalens-pipeline/alphalens_pipeline/thematic/sources/perplexity.py
"""Perplexity "top-stories" news source (5th thematic source).

Fetches the day's most significant market-moving stories via the canonical
PerplexityClient and emits NEWS_COLUMNS rows with tickers=[] — the downstream
map-themes stage proposes beneficiaries from the extracted theme. Source
curation is left entirely to Perplexity (no domain filter). Flag-gated in
news_ingest (ALPHALENS_PERPLEXITY_SOURCE); see
docs/research/perplexity_news_source_design_2026_06_24.md.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path

import pandas as pd

from alphalens_pipeline.literature_scanner.perplexity_client import PerplexityClient
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS, empty_news_frame

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".alphalens" / "thematic_news_perplexity"

_PROMPT = """\
As of {date}, list the most significant market-moving news stories affecting \
US-listed equities that day (deals/M&A, regulation, geopolitics, supply chain, \
major product or sector moves, index changes). Output ONLY a JSON object, nothing else:

{{"stories": [{{"headline": "<concise headline>", "summary": "<1-2 sentence neutral summary>", "url": "<one representative source URL>"}}]}}

Rules: 8-12 distinct stories (not the same event rephrased); concrete events only; \
no price targets or numeric estimates; no investment advice."""


def build_prompt(date_iso: str) -> str:
    return _PROMPT.format(date=date_iso)


def _stable_id(s: str) -> str:
    # Content-addressing; sha256 to satisfy Sonar S4790.
    return "perplexity:" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _extract_json(content: str) -> object:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("no JSON object found")
    obj, _end = json.JSONDecoder().raw_decode(text[min(starts) :])
    return obj


def parse_stories(content: str) -> list[dict]:
    try:
        data = _extract_json(content)
    except (ValueError, json.JSONDecodeError):
        logger.warning("perplexity source: response was not parseable JSON")
        return []
    stories = data.get("stories") if isinstance(data, dict) else data
    if not isinstance(stories, list):
        return []
    out: list[dict] = []
    for s in stories:
        if not isinstance(s, dict):
            continue
        headline = str(s.get("headline", "")).strip()
        url = str(s.get("url", "")).strip()
        if not headline or not url:
            continue
        out.append({"headline": headline, "summary": str(s.get("summary", "")).strip(), "url": url})
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.test_perplexity_source -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/thematic/sources/perplexity.py apps/alphalens-research/tests/thematic/test_perplexity_source.py
git commit -s -m "feat(thematic): Perplexity source — prompt + defensive parse"
```

---

### Task 2: Perplexity source — fetch_daily_news (client, cache, NEWS_COLUMNS)

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/thematic/sources/perplexity.py`
- Test: `apps/alphalens-research/tests/thematic/test_perplexity_source.py`

**Interfaces:**
- Consumes: `build_prompt`, `parse_stories`, `_stable_id` (Task 1); `PerplexityClient.ask_with_citations(query, *, search_context_size=, search_after_date_filter=, search_before_date_filter=) -> AskResult(content, citations, search_results)`.
- Produces: `fetch_daily_news(*, date: dt.date, client=None, cache_dir: Path = DEFAULT_CACHE_DIR, force: bool = False) -> pd.DataFrame` conforming to `NEWS_COLUMNS`; `_default_client() -> PerplexityClient`.

- [ ] **Step 1: Write the failing test**

```python
# append to apps/alphalens-research/tests/thematic/test_perplexity_source.py
import datetime as dt
import json as _json
import tempfile
from pathlib import Path
from unittest import mock

from alphalens_pipeline.literature_scanner.perplexity_client import AskResult
from alphalens_pipeline.thematic.sources.schema import NEWS_COLUMNS


class TestFetchDailyNews(unittest.TestCase):
    def _client(self):
        c = mock.Mock()
        c.ask_with_citations.return_value = AskResult(
            content='{"stories": [{"headline": "SpaceX IPO", "summary": "Debut.", "url": "https://a.com"}]}',
            citations=["https://a.com", "https://b.com"],
            search_results=[{"url": "https://a.com"}, {"url": "https://b.com"}],
        )
        return c

    def test_maps_to_news_columns(self):
        with tempfile.TemporaryDirectory() as d:
            df = perplexity.fetch_daily_news(date=dt.date(2026, 6, 12), client=self._client(), cache_dir=Path(d))
        self.assertEqual(list(df.columns), NEWS_COLUMNS)
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertEqual(row["source"], "perplexity")
        self.assertEqual(row["title"], "SpaceX IPO")
        self.assertEqual(row["body"], "Debut.")
        self.assertEqual(row["url"], "https://a.com")
        self.assertEqual(list(row["tickers"]), [])
        self.assertEqual(_json.loads(row["extra"])["citation_count"], 2)
        self.assertTrue(str(row["id"]).startswith("perplexity:"))
        self.assertEqual(row["timestamp"], pd.Timestamp("2026-06-12", tz="UTC"))

    def test_passes_pit_date_filters(self):
        c = self._client()
        with tempfile.TemporaryDirectory() as d:
            perplexity.fetch_daily_news(date=dt.date(2026, 6, 12), client=c, cache_dir=Path(d))
        kw = c.ask_with_citations.call_args.kwargs
        self.assertEqual(kw["search_after_date_filter"], "06/11/2026")
        self.assertEqual(kw["search_before_date_filter"], "06/13/2026")
        self.assertEqual(kw["search_context_size"], "high")

    def test_caches_raw_and_skips_second_call(self):
        c = self._client()
        with tempfile.TemporaryDirectory() as d:
            perplexity.fetch_daily_news(date=dt.date(2026, 6, 12), client=c, cache_dir=Path(d))
            self.assertTrue((Path(d) / "2026-06-12.json").exists())
            perplexity.fetch_daily_news(date=dt.date(2026, 6, 12), client=c, cache_dir=Path(d))
        self.assertEqual(c.ask_with_citations.call_count, 1)  # second run hit the cache

    def test_empty_stories_returns_empty_frame(self):
        c = mock.Mock()
        c.ask_with_citations.return_value = AskResult(content="no json here", citations=[], search_results=[])
        with tempfile.TemporaryDirectory() as d:
            df = perplexity.fetch_daily_news(date=dt.date(2026, 6, 12), client=c, cache_dir=Path(d))
        self.assertEqual(list(df.columns), NEWS_COLUMNS)
        self.assertEqual(len(df), 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.test_perplexity_source -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'fetch_daily_news'`

- [ ] **Step 3: Write minimal implementation**

Append to `perplexity.py`:

```python
def _default_client() -> PerplexityClient:
    key = os.environ.get("PERPLEXITY_API_KEY")
    if not key:
        raise RuntimeError("PERPLEXITY_API_KEY not set")
    return PerplexityClient(api_key=key)


def fetch_daily_news(
    *,
    date: dt.date,
    client: PerplexityClient | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    force: bool = False,
) -> pd.DataFrame:
    """Fetch the day's Perplexity top-stories as NEWS_COLUMNS rows.

    Raw response cached per date; ``tickers`` is always ``[]`` so the
    downstream map-themes stage proposes beneficiaries from the theme.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{date.isoformat()}.json"
    if cache_path.exists() and not force:
        raw = json.loads(cache_path.read_text())
        content, search_results = raw["content"], raw["search_results"]
    else:
        client = client or _default_client()
        after = (date - dt.timedelta(days=1)).strftime("%m/%d/%Y")
        before = (date + dt.timedelta(days=1)).strftime("%m/%d/%Y")
        result = client.ask_with_citations(
            build_prompt(date.isoformat()),
            search_context_size="high",
            search_after_date_filter=after,
            search_before_date_filter=before,
        )
        content, search_results = result.content, result.search_results
        cache_path.write_text(json.dumps({"content": content, "search_results": search_results}))

    stories = parse_stories(content)
    citation_urls = [str(r.get("url", "")) for r in search_results if isinstance(r, dict)]
    extra = json.dumps({"citation_count": len(citation_urls), "citations": citation_urls})
    ts = pd.Timestamp(date, tz="UTC")
    now = pd.Timestamp.now(tz="UTC")

    rows = [
        {
            "id": _stable_id(s["url"] or s["headline"]),
            "source": "perplexity",
            "timestamp": ts,
            "tickers": [],
            "title": s["headline"],
            "body": s["summary"],
            "url": s["url"],
            "keywords": [],
            "extra": extra,
            "ingested_at": now,
        }
        for s in stories
    ]
    if not rows:
        return empty_news_frame()
    df = pd.DataFrame(rows, columns=NEWS_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["ingested_at"] = pd.to_datetime(df["ingested_at"], utc=True)
    return df
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.test_perplexity_source -v`
Expected: PASS (9 tests total)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/thematic/sources/perplexity.py apps/alphalens-research/tests/thematic/test_perplexity_source.py
git commit -s -m "feat(thematic): Perplexity source fetch_daily_news (cache + NEWS_COLUMNS)"
```

---

### Task 3: Register the source in news_ingest (flag-gated)

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/thematic/news_ingest.py` (lines 57, 66-71, after 91, 407-428)
- Test: `apps/alphalens-research/tests/thematic/test_news_ingest.py`

**Interfaces:**
- Consumes: `perplexity.fetch_daily_news` (Task 2).
- Produces: `_fetch_perplexity(*, date: dt.date) -> pd.DataFrame` (flag-gated); `_SOURCE_PRIORITY["perplexity"] == 4`; `_SOURCE_QUOTA_WEIGHTS["perplexity"] == 0.15`.

- [ ] **Step 1: Write the failing test**

```python
# append to apps/alphalens-research/tests/thematic/test_news_ingest.py
import datetime as dt
from unittest import mock

from alphalens_pipeline.thematic import news_ingest
from alphalens_pipeline.thematic.sources.schema import empty_news_frame


class TestPerplexitySourceRegistration(unittest.TestCase):
    def test_quota_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(news_ingest._SOURCE_QUOTA_WEIGHTS.values()), 1.0, places=9)

    def test_perplexity_registered_in_priority_and_quota(self):
        self.assertEqual(news_ingest._SOURCE_PRIORITY["perplexity"], 4)
        self.assertIn("perplexity", news_ingest._SOURCE_QUOTA_WEIGHTS)

    def test_fetch_perplexity_off_by_default_does_not_call_adapter(self):
        import os
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALPHALENS_PERPLEXITY_SOURCE", None)
            with mock.patch(
                "alphalens_pipeline.thematic.sources.perplexity.fetch_daily_news"
            ) as adapter:
                df = news_ingest._fetch_perplexity(date=dt.date(2026, 6, 12))
        adapter.assert_not_called()
        self.assertEqual(len(df), 0)

    def test_fetch_perplexity_on_calls_adapter(self):
        import os
        with mock.patch.dict(os.environ, {"ALPHALENS_PERPLEXITY_SOURCE": "1"}):
            with mock.patch(
                "alphalens_pipeline.thematic.sources.perplexity.fetch_daily_news",
                return_value=empty_news_frame(),
            ) as adapter:
                news_ingest._fetch_perplexity(date=dt.date(2026, 6, 12))
        adapter.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.test_news_ingest.TestPerplexitySourceRegistration -v`
Expected: FAIL — `KeyError: 'perplexity'` / `AttributeError: ... '_fetch_perplexity'`

- [ ] **Step 3: Write minimal implementation**

In `news_ingest.py`:

(a) line 57 — add perplexity to priority:
```python
_SOURCE_PRIORITY = {"edgar_press_release": 0, "polygon": 1, "gdelt": 2, "rss": 3, "perplexity": 4}
```

(b) lines 66-71 — rebalance quota weights to sum 1.0:
```python
_SOURCE_QUOTA_WEIGHTS = {
    "edgar_press_release": 0.25,  # richest — 8-K EX-99.1 issuer-direct
    "polygon": 0.25,  # curated / ticker-tagged
    "gdelt": 0.20,  # high-volume, lower signal-to-noise
    "rss": 0.15,  # high-volume, lower signal-to-noise
    "perplexity": 0.15,  # edited multi-source top-stories (flag-gated)
}
```

(c) after `_fetch_rss` (line 91) — add the flag-gated wrapper (ensure `import os` and `from alphalens_pipeline.thematic.sources import ... perplexity` are present at the top of the file; add them if missing):
```python
def _fetch_perplexity(*, date: dt.date) -> pd.DataFrame:
    # Flag-gated: off by default so production is unchanged. When off, the
    # client is never constructed. Routed through the canonical PerplexityClient.
    if os.environ.get("ALPHALENS_PERPLEXITY_SOURCE") != "1":
        return empty_news_frame()
    return perplexity.fetch_daily_news(date=date)
```

(d) line 410 area — add the safe call:
```python
    rss_df = _safe_call("rss", _fetch_rss, date=date)
    perplexity_df = _safe_call("perplexity", _fetch_perplexity, date=date)
```

(e) lines 417-422 — add perplexity to the source_frames dict (so the `_SOURCE_PRIORITY` loop at 423 finds it):
```python
        source_frames = {
            "edgar_press_release": edgar_df,
            "polygon": polygon_df,
            "gdelt": gdelt_df,
            "rss": rss_df,
            "perplexity": perplexity_df,
        }
```

(f) lines 426-428 — include perplexity_df in the concat list:
```python
    frames = [
        _decode_title_entities(df)
        for df in (edgar_df, polygon_df, gdelt_df, rss_df, perplexity_df)
        if len(df) > 0
    ]
```

Verify the top of `news_ingest.py` imports `os` and the `perplexity` source module; add `import os` and extend the `from alphalens_pipeline.thematic.sources import (...)` group with `perplexity` if not already present.

- [ ] **Step 4: Run the new test + the existing ingest suite**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.test_news_ingest tests.thematic.test_news_ingest_bitemporal -v`
Expected: PASS — new registration tests pass AND existing tests still pass (flag defaults off, so perplexity contributes an empty frame and existing aggregation/allocation assertions are unchanged). If an existing allocation assertion shifted because the quota weights changed, that is a real interaction: re-derive the expected allocation for the changed weights and update that assertion in the same commit, noting it in the commit body.

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/thematic/news_ingest.py apps/alphalens-research/tests/thematic/test_news_ingest.py
git commit -s -m "feat(thematic): register flag-gated Perplexity source in news_ingest"
```

---

### Task 4: Full thematic suite + doctrine guards green

**Files:** none (verification only).

- [ ] **Step 1: Run the Perplexity + ingest tests together**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.thematic.test_perplexity_source tests.thematic.test_news_ingest tests.thematic.test_news_ingest_bitemporal -v`
Expected: PASS (all).

- [ ] **Step 2: Run the doctrine guards touched by this work**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_no_raw_perplexity_http tests.test_module_dependencies tests.test_no_polish_chars -v`
Expected: PASS — Perplexity HTTP only via the client (no raw `requests` in the adapter); pipeline does not import research (the adapter has its own `_extract_json`, does not import `alphalens_research.discover_lane`); no Polish characters.

- [ ] **Step 3: Run the broader thematic source suite to catch regressions**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest discover -s tests/thematic -t . 2>&1 | tail -5`
Expected: OK. If a failure traces to the quota-weight rebalance (Task 3), fix the affected assertion by re-deriving the expected allocation and note it; do not change the weights (they are the spec's values).

---

## Self-Review notes

- **Spec coverage:** §3 location → Task 1/2 file; §4 prompt+PIT → Task 1 (`build_prompt`, no steering) + Task 2 (date filters); §5 NEWS_COLUMNS mapping → Task 2; §6 registration+flag → Task 3; §7 raw cache → Task 2 (`test_caches_raw_and_skips_second_call`); §8 provenance (`source` field) → Task 2 sets `source="perplexity"` (measurement script out of scope, per spec); §9 doctrine → Task 4 guards; §10 known issues (no behaviour to build); §12 testing → Tasks 1-4. §13 deployment is ops, not code.
- **Type consistency:** `build_prompt`, `parse_stories`, `_stable_id`, `_extract_json`, `fetch_daily_news(*, date, client=None, cache_dir=, force=)`, `_default_client`, `_fetch_perplexity(*, date)` are used identically across tasks. `AskResult(content, citations, search_results)` matches the merged client. `NEWS_COLUMNS` order matches `schema.py`.
- **Deliberate:** `tickers=[]` (beneficiaries from map-themes); flag defaults off (existing ingest tests unaffected); adapter carries its own `_extract_json` (no research import).
