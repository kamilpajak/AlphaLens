# Discover-lane Experiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a parallel "Discover-style" Perplexity candidate list for a few recent brief dates and render it side-by-side against the real brief, as a one-shot research experiment.

**Architecture:** Testable pure logic in a new RESEARCH_ONLY package `alphalens_research/discover_lane/`; a thin CLI script does I/O + orchestration. The canonical `PerplexityClient` gains one additive method returning citations. No changes to the live pipeline, Django, web, or EDGE.

**Tech Stack:** Python 3.13, unittest, requests (via existing client), pandas (parquet read), canonical `PerplexityClient` + `YFinanceClient`.

**Spec:** `docs/research/discover_lane_experiment_design_2026_06_24.md`

## Global Constraints

- All numeric values (market cap) come from yfinance via `YFinanceClient`, NEVER from the LLM (LLM training-cutoff doctrine).
- No market-cap / P/E / volume bracket constraints in any Perplexity prompt; filter in Python post-hoc.
- One canonical client per vendor: every Perplexity call goes through `PerplexityClient`; every yfinance call through `YFinanceClient`. No shadow clients (enforced by `test_no_raw_perplexity_http.py` / `test_no_raw_yfinance_http.py`).
- New package must declare `__status__ = "RESEARCH_ONLY"` (enforced by `test_layer_status.py`).
- Dependency direction: `alphalens_research.*` may import `alphalens_pipeline.{data,thematic.config,literature_scanner}`; never the reverse at top level (enforced by `test_module_dependencies.py`).
- English-only in code/comments/identifiers (enforced by `test_no_polish_chars.py`).
- TDD: red → green → commit per task.
- Test invocation (run from repo-relative app dir):
  `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.<module> -v`

---

### Task 1: Package scaffold + data models

**Files:**
- Create: `apps/alphalens-research/alphalens_research/discover_lane/__init__.py`
- Create: `apps/alphalens-research/alphalens_research/discover_lane/models.py`
- Create: `apps/alphalens-research/tests/discover_lane/__init__.py`
- Test: `apps/alphalens-research/tests/discover_lane/test_models.py`

**Interfaces:**
- Produces: `DiscoverCandidate`, `BriefCandidate`, `ComparisonResult`, `DateBlock` dataclasses (see code below); `__status__ = "RESEARCH_ONLY"`.

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-research/tests/discover_lane/test_models.py
import unittest

from alphalens_research.discover_lane import __status__
from alphalens_research.discover_lane.models import (
    BriefCandidate,
    ComparisonResult,
    DateBlock,
    DiscoverCandidate,
)


class TestModels(unittest.TestCase):
    def test_status_is_research_only(self):
        self.assertEqual(__status__, "RESEARCH_ONLY")

    def test_discover_candidate_defaults(self):
        c = DiscoverCandidate(
            ticker="NVDA",
            company="NVIDIA",
            theme="AI chips",
            rationale="benefits from AI demand",
            citation_count=29,
            citation_urls=["https://example.com/a"],
            source_event_title="AI chip prices double",
            source_event_url="https://example.com/a",
        )
        self.assertIsNone(c.mcap)
        self.assertFalse(c.resolved)
        self.assertFalse(c.in_pipeline_universe)

    def test_comparison_and_dateblock_construct(self):
        cmp = ComparisonResult(
            shared=["NVDA"], perplexity_only=[], brief_only=["ABC"],
            discover_median_mcap=1.0, brief_median_mcap=2.0,
        )
        block = DateBlock(date="2026-06-23", discover=[], brief=[], comparison=cmp)
        self.assertEqual(block.date, "2026-06-23")
        self.assertEqual(cmp.shared, ["NVDA"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_models -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'alphalens_research.discover_lane'`

- [ ] **Step 3: Write minimal implementation**

```python
# apps/alphalens-research/alphalens_research/discover_lane/__init__.py
"""One-shot research experiment: parallel Perplexity-driven candidate generation.

See docs/research/discover_lane_experiment_design_2026_06_24.md.
"""

__status__ = "RESEARCH_ONLY"
```

```python
# apps/alphalens-research/alphalens_research/discover_lane/models.py
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DiscoverCandidate:
    ticker: str
    company: str
    theme: str
    rationale: str
    citation_count: int
    citation_urls: list[str]
    source_event_title: str
    source_event_url: str
    mcap: float | None = None
    resolved: bool = False
    in_pipeline_universe: bool = False


@dataclass(frozen=True)
class BriefCandidate:
    ticker: str
    company: str
    theme: str
    source_event_title: str
    mcap: float | None


@dataclass(frozen=True)
class ComparisonResult:
    shared: list[str]
    perplexity_only: list[str]
    brief_only: list[str]
    discover_median_mcap: float | None
    brief_median_mcap: float | None


@dataclass(frozen=True)
class DateBlock:
    date: str
    discover: list[DiscoverCandidate]
    brief: list[BriefCandidate]
    comparison: ComparisonResult
```

Create empty `apps/alphalens-research/tests/discover_lane/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_models -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/alphalens_research/discover_lane apps/alphalens-research/tests/discover_lane
git commit -m "feat(discover-lane): package scaffold + data models"
```

---

### Task 2: PerplexityClient.ask_with_citations (canonical client extension)

**Files:**
- Modify: `apps/alphalens-pipeline/alphalens_pipeline/literature_scanner/perplexity_client.py`
- Test: `apps/alphalens-research/tests/discover_lane/test_perplexity_citations.py`

**Interfaces:**
- Produces: `AskResult(content: str, citations: list[str], search_results: list[dict])` and
  `PerplexityClient.ask_with_citations(query, *, search_context_size="medium", search_recency_filter=None, search_after_date_filter=None, search_before_date_filter=None) -> AskResult`.
- Note: date filters are strings in `MM/DD/YYYY` format (Perplexity Search/Sonar convention).

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-research/tests/discover_lane/test_perplexity_citations.py
import unittest
from unittest import mock

from alphalens_pipeline.literature_scanner.perplexity_client import (
    AskResult,
    PerplexityClient,
)

_FAKE_RESPONSE = {
    "choices": [{"message": {"content": '{"stories": []}'}}],
    "citations": ["https://a.com", "https://b.com"],
    "search_results": [
        {"title": "A", "url": "https://a.com", "date": "2026-06-23"},
        {"title": "B", "url": "https://b.com", "date": "2026-06-22"},
    ],
}


class TestAskWithCitations(unittest.TestCase):
    def test_parses_content_and_sources(self):
        client = PerplexityClient(api_key="k")
        fake = mock.Mock()
        fake.json.return_value = _FAKE_RESPONSE
        fake.raise_for_status.return_value = None
        with mock.patch(
            "alphalens_pipeline.literature_scanner.perplexity_client.requests.post",
            return_value=fake,
        ) as post:
            result = client.ask_with_citations(
                "q", search_after_date_filter="06/16/2026",
                search_before_date_filter="06/23/2026",
            )
        self.assertIsInstance(result, AskResult)
        self.assertEqual(result.content, '{"stories": []}')
        self.assertEqual(result.citations, ["https://a.com", "https://b.com"])
        self.assertEqual(len(result.search_results), 2)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["search_after_date_filter"], "06/16/2026")
        self.assertEqual(sent["search_before_date_filter"], "06/23/2026")

    def test_missing_sources_default_empty(self):
        client = PerplexityClient(api_key="k")
        fake = mock.Mock()
        fake.json.return_value = {"choices": [{"message": {"content": "x"}}]}
        fake.raise_for_status.return_value = None
        with mock.patch(
            "alphalens_pipeline.literature_scanner.perplexity_client.requests.post",
            return_value=fake,
        ):
            result = client.ask_with_citations("q")
        self.assertEqual(result.content, "x")
        self.assertEqual(result.citations, [])
        self.assertEqual(result.search_results, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_perplexity_citations -v`
Expected: FAIL — `ImportError: cannot import name 'AskResult'`

- [ ] **Step 3: Write minimal implementation**

Add to `perplexity_client.py` (after the existing imports add `from dataclasses import dataclass`; place `AskResult` above the class, and the method inside `PerplexityClient`):

```python
@dataclass(frozen=True)
class AskResult:
    content: str
    citations: list[str]
    search_results: list[dict]
```

```python
    def ask_with_citations(
        self,
        query: str,
        *,
        search_context_size: SearchContextSize = "medium",
        search_recency_filter: str | None = None,
        search_after_date_filter: str | None = None,
        search_before_date_filter: str | None = None,
    ) -> "AskResult":
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": query}],
            "web_search_options": {"search_context_size": search_context_size},
        }
        if search_recency_filter:
            payload["search_recency_filter"] = search_recency_filter
        if search_after_date_filter:
            payload["search_after_date_filter"] = search_after_date_filter
        if search_before_date_filter:
            payload["search_before_date_filter"] = search_before_date_filter

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        citations = data.get("citations") or []
        search_results = data.get("search_results") or []
        return AskResult(content=content, citations=list(citations), search_results=list(search_results))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_perplexity_citations -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-pipeline/alphalens_pipeline/literature_scanner/perplexity_client.py apps/alphalens-research/tests/discover_lane/test_perplexity_citations.py
git commit -m "feat(perplexity): ask_with_citations returns content + sources"
```

---

### Task 3: Discover-style prompt builder

**Files:**
- Create: `apps/alphalens-research/alphalens_research/discover_lane/prompt.py`
- Test: `apps/alphalens-research/tests/discover_lane/test_prompt.py`

**Interfaces:**
- Produces: `build_discover_prompt(date_iso: str) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-research/tests/discover_lane/test_prompt.py
import unittest

from alphalens_research.discover_lane.prompt import build_discover_prompt


class TestPrompt(unittest.TestCase):
    def test_contains_date_and_required_fields(self):
        p = build_discover_prompt("2026-06-23")
        self.assertIn("2026-06-23", p)
        for token in ("ticker", "company", "reason", "event", "JSON"):
            self.assertIn(token, p)

    def test_no_numeric_bracket_constraints(self):
        p = build_discover_prompt("2026-06-23").lower()
        for banned in ("market cap", "market-cap", "small-cap", "mid-cap", "p/e", "valuation"):
            self.assertNotIn(banned, p)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_prompt -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# apps/alphalens-research/alphalens_research/discover_lane/prompt.py
from __future__ import annotations

_TEMPLATE = """\
As of {date}, identify the most significant market-moving news stories affecting \
US-listed equities. Focus on concrete events (deals, regulation, product, supply, \
earnings surprises), not generic commentary.

For each story, name the specific US-listed beneficiary companies. Output ONLY a \
JSON object with this exact shape and nothing else:

{{
  "stories": [
    {{
      "event_title": "<short headline of the triggering event>",
      "event_url": "<one representative source URL>",
      "beneficiaries": [
        {{"ticker": "<US-listed ticker>", "company": "<company name>", "reason": "<one sentence why this company benefits>"}}
      ]
    }}
  ]
}}

Rules: only US-listed equities with a real ticker; give your best-known ticker \
symbol; do not include any market cap, valuation, price, or numeric estimates; \
reason must be a single sentence.
"""


def build_discover_prompt(date_iso: str) -> str:
    return _TEMPLATE.format(date=date_iso)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_prompt -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/alphalens_research/discover_lane/prompt.py apps/alphalens-research/tests/discover_lane/test_prompt.py
git commit -m "feat(discover-lane): Discover-style prompt builder"
```

---

### Task 4: Parse Perplexity response into candidates

**Files:**
- Create: `apps/alphalens-research/alphalens_research/discover_lane/parse.py`
- Test: `apps/alphalens-research/tests/discover_lane/test_parse.py`

**Interfaces:**
- Consumes: `DiscoverCandidate` (Task 1).
- Produces: `parse_discover_response(content: str, search_results: list[dict]) -> list[DiscoverCandidate]`.
- Note: `citation_count`/`citation_urls` are taken from the whole-response `search_results` (the synthesis drew on all of them — this is the "N źródeł" signal); `mcap`/`resolved`/`in_pipeline_universe` are left at defaults here and filled by Task 5.

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-research/tests/discover_lane/test_parse.py
import unittest

from alphalens_research.discover_lane.parse import parse_discover_response

_SOURCES = [{"url": "https://a.com"}, {"url": "https://b.com"}]


class TestParse(unittest.TestCase):
    def test_parses_well_formed(self):
        content = (
            '{"stories": [{"event_title": "AI chips", "event_url": "https://a.com",'
            ' "beneficiaries": ['
            '{"ticker": "nvda", "company": "NVIDIA", "reason": "AI demand"},'
            '{"ticker": "AMD", "company": "AMD", "reason": "GPU share"}]}]}'
        )
        out = parse_discover_response(content, _SOURCES)
        self.assertEqual([c.ticker for c in out], ["NVDA", "AMD"])
        self.assertEqual(out[0].citation_count, 2)
        self.assertEqual(out[0].theme, "AI chips")
        self.assertEqual(out[0].source_event_url, "https://a.com")

    def test_skips_malformed_entries(self):
        content = (
            '{"stories": [{"event_title": "x", "event_url": "u", "beneficiaries": ['
            '{"ticker": "", "company": "no ticker", "reason": "r"},'
            '"notadict",'
            '{"ticker": "GOOD", "company": "Good Co", "reason": "r"}]}]}'
        )
        out = parse_discover_response(content, _SOURCES)
        self.assertEqual([c.ticker for c in out], ["GOOD"])

    def test_handles_code_fenced_json(self):
        content = '```json\n{"stories": [{"event_title": "t", "event_url": "u", "beneficiaries": [{"ticker": "X", "company": "X Co", "reason": "r"}]}]}\n```'
        out = parse_discover_response(content, _SOURCES)
        self.assertEqual([c.ticker for c in out], ["X"])

    def test_non_json_returns_empty(self):
        self.assertEqual(parse_discover_response("sorry, no JSON here", _SOURCES), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_parse -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# apps/alphalens-research/alphalens_research/discover_lane/parse.py
from __future__ import annotations

import json
import logging

from .models import DiscoverCandidate

logger = logging.getLogger(__name__)


def _extract_json(content: str) -> object:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    candidates = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not candidates:
        raise ValueError("no JSON object found")
    return json.loads(text[min(candidates):])


def parse_discover_response(content: str, search_results: list[dict]) -> list[DiscoverCandidate]:
    try:
        data = _extract_json(content)
    except (ValueError, json.JSONDecodeError):
        logger.warning("discover_lane: response was not parseable JSON")
        return []

    stories = data.get("stories") if isinstance(data, dict) else data
    if not isinstance(stories, list):
        return []

    citation_urls = [str(r.get("url", "")) for r in search_results if isinstance(r, dict)]
    citation_count = len(citation_urls)

    out: list[DiscoverCandidate] = []
    for story in stories:
        if not isinstance(story, dict):
            continue
        event_title = str(story.get("event_title", "")).strip()
        event_url = str(story.get("event_url", "")).strip()
        for b in story.get("beneficiaries") or []:
            if not isinstance(b, dict):
                continue
            ticker = str(b.get("ticker", "")).strip().upper()
            company = str(b.get("company", "")).strip()
            reason = str(b.get("reason", "")).strip()
            if not ticker or not company:
                continue
            out.append(
                DiscoverCandidate(
                    ticker=ticker,
                    company=company,
                    theme=event_title,
                    rationale=reason,
                    citation_count=citation_count,
                    citation_urls=citation_urls,
                    source_event_title=event_title,
                    source_event_url=event_url,
                )
            )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_parse -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/alphalens_research/discover_lane/parse.py apps/alphalens-research/tests/discover_lane/test_parse.py
git commit -m "feat(discover-lane): parse Perplexity response into candidates"
```

---

### Task 5: Enrich candidates with mcap + universe flag

**Files:**
- Create: `apps/alphalens-research/alphalens_research/discover_lane/enrich.py`
- Test: `apps/alphalens-research/tests/discover_lane/test_enrich.py`

**Interfaces:**
- Consumes: `DiscoverCandidate` (Task 1). `yf_client` is any object with `market_cap(ticker: str) -> float | None` (the canonical `YFinanceClient` satisfies this).
- Produces: `enrich_candidates(candidates, *, yf_client, universe: set[str]) -> list[DiscoverCandidate]`. Dedups by ticker (first occurrence wins). Sets `mcap`, `resolved = mcap is not None`, `in_pipeline_universe = ticker in universe`. Unresolved candidates are kept, not dropped.

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-research/tests/discover_lane/test_enrich.py
import unittest

from alphalens_research.discover_lane.enrich import enrich_candidates
from alphalens_research.discover_lane.models import DiscoverCandidate


def _cand(ticker):
    return DiscoverCandidate(
        ticker=ticker, company=ticker, theme="t", rationale="r",
        citation_count=1, citation_urls=["u"], source_event_title="t", source_event_url="u",
    )


class _FakeYf:
    def __init__(self, mcaps):
        self._mcaps = mcaps

    def market_cap(self, ticker):
        return self._mcaps.get(ticker)


class TestEnrich(unittest.TestCase):
    def test_flags_and_mcap(self):
        yf = _FakeYf({"NVDA": 3.0e12, "ZZZZ": None})
        out = enrich_candidates(
            [_cand("NVDA"), _cand("ZZZZ")], yf_client=yf, universe={"NVDA"}
        )
        by = {c.ticker: c for c in out}
        self.assertEqual(by["NVDA"].mcap, 3.0e12)
        self.assertTrue(by["NVDA"].resolved)
        self.assertTrue(by["NVDA"].in_pipeline_universe)
        self.assertIsNone(by["ZZZZ"].mcap)
        self.assertFalse(by["ZZZZ"].resolved)
        self.assertFalse(by["ZZZZ"].in_pipeline_universe)

    def test_dedups_by_ticker(self):
        yf = _FakeYf({"NVDA": 1.0})
        out = enrich_candidates([_cand("NVDA"), _cand("NVDA")], yf_client=yf, universe=set())
        self.assertEqual(len(out), 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_enrich -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# apps/alphalens-research/alphalens_research/discover_lane/enrich.py
from __future__ import annotations

import dataclasses

from .models import DiscoverCandidate


def enrich_candidates(
    candidates: list[DiscoverCandidate],
    *,
    yf_client,
    universe: set[str],
) -> list[DiscoverCandidate]:
    out: list[DiscoverCandidate] = []
    seen: set[str] = set()
    for c in candidates:
        if c.ticker in seen:
            continue
        seen.add(c.ticker)
        mcap = yf_client.market_cap(c.ticker)
        out.append(
            dataclasses.replace(
                c,
                mcap=mcap,
                resolved=mcap is not None,
                in_pipeline_universe=c.ticker in universe,
            )
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_enrich -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/alphalens_research/discover_lane/enrich.py apps/alphalens-research/tests/discover_lane/test_enrich.py
git commit -m "feat(discover-lane): enrich candidates with mcap + universe flag"
```

---

### Task 6: Compare discover candidates vs brief

**Files:**
- Create: `apps/alphalens-research/alphalens_research/discover_lane/compare.py`
- Test: `apps/alphalens-research/tests/discover_lane/test_compare.py`

**Interfaces:**
- Consumes: `DiscoverCandidate`, `BriefCandidate`, `ComparisonResult` (Task 1).
- Produces: `compare_candidates(discover: list[DiscoverCandidate], brief: list[BriefCandidate]) -> ComparisonResult`.

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-research/tests/discover_lane/test_compare.py
import unittest

from alphalens_research.discover_lane.compare import compare_candidates
from alphalens_research.discover_lane.models import BriefCandidate, DiscoverCandidate


def _disc(ticker, mcap):
    return DiscoverCandidate(
        ticker=ticker, company=ticker, theme="t", rationale="r",
        citation_count=1, citation_urls=["u"], source_event_title="t",
        source_event_url="u", mcap=mcap, resolved=mcap is not None,
    )


def _brief(ticker, mcap):
    return BriefCandidate(ticker=ticker, company=ticker, theme="t", source_event_title="t", mcap=mcap)


class TestCompare(unittest.TestCase):
    def test_overlap_and_medians(self):
        discover = [_disc("NVDA", 3.0e12), _disc("AMD", 2.0e11)]
        brief = [_brief("AMD", 2.0e11), _brief("SMCI", 2.0e10)]
        res = compare_candidates(discover, brief)
        self.assertEqual(res.shared, ["AMD"])
        self.assertEqual(res.perplexity_only, ["NVDA"])
        self.assertEqual(res.brief_only, ["SMCI"])
        self.assertEqual(res.discover_median_mcap, 1.6e12)  # median(3e12, 2e11)
        self.assertEqual(res.brief_median_mcap, 1.1e11)     # median(2e11, 2e10)

    def test_handles_missing_mcap(self):
        res = compare_candidates([_disc("X", None)], [])
        self.assertIsNone(res.discover_median_mcap)
        self.assertIsNone(res.brief_median_mcap)
        self.assertEqual(res.perplexity_only, ["X"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_compare -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# apps/alphalens-research/alphalens_research/discover_lane/compare.py
from __future__ import annotations

import statistics

from .models import BriefCandidate, ComparisonResult, DiscoverCandidate


def _median_mcap(cands) -> float | None:
    vals = [c.mcap for c in cands if c.mcap is not None]
    return statistics.median(vals) if vals else None


def compare_candidates(
    discover: list[DiscoverCandidate],
    brief: list[BriefCandidate],
) -> ComparisonResult:
    d = {c.ticker for c in discover}
    b = {c.ticker for c in brief}
    return ComparisonResult(
        shared=sorted(d & b),
        perplexity_only=sorted(d - b),
        brief_only=sorted(b - d),
        discover_median_mcap=_median_mcap(discover),
        brief_median_mcap=_median_mcap(brief),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_compare -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/alphalens_research/discover_lane/compare.py apps/alphalens-research/tests/discover_lane/test_compare.py
git commit -m "feat(discover-lane): compare discover vs brief candidates"
```

---

### Task 7: HTML side-by-side report renderer

**Files:**
- Create: `apps/alphalens-research/alphalens_research/discover_lane/render.py`
- Test: `apps/alphalens-research/tests/discover_lane/test_render.py`

**Interfaces:**
- Consumes: `DateBlock` (Task 1).
- Produces: `render_report(blocks: list[DateBlock], generated_stamp: str) -> str` returning a complete self-contained HTML document (inline CSS, no framework).

- [ ] **Step 1: Write the failing test**

```python
# apps/alphalens-research/tests/discover_lane/test_render.py
import unittest

from alphalens_research.discover_lane.models import (
    BriefCandidate,
    ComparisonResult,
    DateBlock,
    DiscoverCandidate,
)
from alphalens_research.discover_lane.render import render_report


class TestRender(unittest.TestCase):
    def test_renders_html_with_tickers_and_sources(self):
        disc = DiscoverCandidate(
            ticker="NVDA", company="NVIDIA", theme="AI chips", rationale="AI demand",
            citation_count=29, citation_urls=["https://a.com"],
            source_event_title="AI chip prices double", source_event_url="https://a.com",
            mcap=3.0e12, resolved=True, in_pipeline_universe=False,
        )
        brief = BriefCandidate(
            ticker="SMCI", company="Super Micro", theme="AI servers",
            source_event_title="server demand", mcap=2.0e10,
        )
        cmp = ComparisonResult(
            shared=[], perplexity_only=["NVDA"], brief_only=["SMCI"],
            discover_median_mcap=3.0e12, brief_median_mcap=2.0e10,
        )
        block = DateBlock(date="2026-06-23", discover=[disc], brief=[brief], comparison=cmp)
        html = render_report([block], generated_stamp="2026-06-24T10:00:00Z")
        self.assertIn("<html", html.lower())
        self.assertIn("NVDA", html)
        self.assertIn("SMCI", html)
        self.assertIn("29", html)          # citation count
        self.assertIn("2026-06-23", html)  # date header
        self.assertIn("2026-06-24T10:00:00Z", html)  # generated stamp

    def test_escapes_html_in_text(self):
        disc = DiscoverCandidate(
            ticker="X", company="A & B <Co>", theme="t", rationale="r",
            citation_count=1, citation_urls=[], source_event_title="t", source_event_url="u",
        )
        cmp = ComparisonResult(shared=[], perplexity_only=["X"], brief_only=[],
                               discover_median_mcap=None, brief_median_mcap=None)
        block = DateBlock(date="2026-06-23", discover=[disc], brief=[], comparison=cmp)
        html = render_report([block], generated_stamp="s")
        self.assertIn("A &amp; B &lt;Co&gt;", html)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_render -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# apps/alphalens-research/alphalens_research/discover_lane/render.py
from __future__ import annotations

from html import escape

from .models import BriefCandidate, DateBlock, DiscoverCandidate

_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem;background:#0f1115;color:#e6e6e6}
h1{font-size:1.3rem}h2{font-size:1.05rem;border-bottom:1px solid #333;padding-bottom:.3rem;margin-top:2rem}
.cols{display:flex;gap:1.5rem;align-items:flex-start}.col{flex:1}
.col h3{font-size:.85rem;text-transform:uppercase;letter-spacing:.05em;color:#9aa}
.card{background:#1a1d24;border:1px solid #2a2e38;border-radius:8px;padding:.7rem .85rem;margin:.5rem 0}
.card.unresolved{opacity:.5}
.tk{font-weight:700}.mcap{color:#7fd1b9}.src{color:#8a8fa3;font-size:.8rem}
.badge{display:inline-block;font-size:.7rem;padding:.05rem .4rem;border-radius:4px;margin-left:.3rem;background:#2a2e38}
.bar{color:#9aa;font-size:.85rem;margin:.3rem 0 .6rem}
"""


def _fmt_mcap(mcap: float | None) -> str:
    if mcap is None:
        return "—"
    if mcap >= 1e12:
        return f"${mcap / 1e12:.1f}T"
    if mcap >= 1e9:
        return f"${mcap / 1e9:.1f}B"
    return f"${mcap / 1e6:.0f}M"


def _discover_card(c: DiscoverCandidate, shared: set[str]) -> str:
    badges = ""
    if c.in_pipeline_universe:
        badges += '<span class="badge">in-universe</span>'
    if c.ticker in shared:
        badges += '<span class="badge">also-in-brief</span>'
    if not c.resolved:
        badges += '<span class="badge">unresolved</span>'
    src = (
        f'<a class="src" href="{escape(c.source_event_url)}">{escape(c.source_event_title)}</a>'
        if c.source_event_url
        else f'<span class="src">{escape(c.source_event_title)}</span>'
    )
    cls = "card unresolved" if not c.resolved else "card"
    return (
        f'<div class="{cls}"><span class="tk">{escape(c.ticker)}</span> '
        f'{escape(c.company)} <span class="mcap">{_fmt_mcap(c.mcap)}</span>{badges}<br>'
        f'<span class="src">{escape(c.theme)}</span><br>{escape(c.rationale)}<br>'
        f'{src} · <span class="src">{c.citation_count} sources</span></div>'
    )


def _brief_card(c: BriefCandidate, shared: set[str]) -> str:
    badge = '<span class="badge">also-in-perplexity</span>' if c.ticker in shared else ""
    return (
        f'<div class="card"><span class="tk">{escape(c.ticker)}</span> '
        f'{escape(c.company)} <span class="mcap">{_fmt_mcap(c.mcap)}</span>{badge}<br>'
        f'<span class="src">{escape(c.theme)}</span><br>'
        f'<span class="src">{escape(c.source_event_title)}</span></div>'
    )


def _block_html(block: DateBlock) -> str:
    shared = set(block.comparison.shared)
    bar = (
        f"Perplexity {len(block.discover)} · brief {len(block.brief)} · "
        f"shared {len(block.comparison.shared)} · "
        f"median mcap P={_fmt_mcap(block.comparison.discover_median_mcap)} "
        f"vs B={_fmt_mcap(block.comparison.brief_median_mcap)}"
    )
    disc = "".join(_discover_card(c, shared) for c in block.discover) or '<div class="card">—</div>'
    brf = "".join(_brief_card(c, shared) for c in block.brief) or '<div class="card">—</div>'
    return (
        f"<h2>{escape(block.date)}</h2><div class='bar'>{escape(bar)}</div>"
        f"<div class='cols'><div class='col'><h3>Perplexity-Discover</h3>{disc}</div>"
        f"<div class='col'><h3>Brief</h3>{brf}</div></div>"
    )


def render_report(blocks: list[DateBlock], generated_stamp: str) -> str:
    body = "".join(_block_html(b) for b in blocks)
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Discover-lane experiment</title><style>{_CSS}</style></head><body>"
        f"<h1>Discover-lane experiment</h1>"
        f"<p class='src'>generated {escape(generated_stamp)}</p>{body}</body></html>"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.discover_lane.test_render -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/alphalens-research/alphalens_research/discover_lane/render.py apps/alphalens-research/tests/discover_lane/test_render.py
git commit -m "feat(discover-lane): HTML side-by-side report renderer"
```

---

### Task 8: CLI orchestration script

**Files:**
- Create: `apps/alphalens-research/scripts/discover_lane_experiment.py`

**Interfaces:**
- Consumes: every package module (Tasks 1–7), `PerplexityClient.ask_with_citations` (Task 2), `get_default_yfinance_client()` from `alphalens_pipeline.data.alt_data.yfinance_client`, `load_input_universe()` from `alphalens_pipeline.thematic.config.universe`.
- Produces: a runnable script writing an HTML report. Not unit-tested (scripts are excluded from the diff-coverage gate); verified by a `--help` smoke run and an operator run.

- [ ] **Step 1: Write the script**

```python
# apps/alphalens-research/scripts/discover_lane_experiment.py
"""One-shot experiment: parallel Perplexity-driven candidate generation.

Renders a side-by-side HTML report (Perplexity-Discover vs the real brief) for the
latest N brief dates. RESEARCH_ONLY. See
docs/research/discover_lane_experiment_design_2026_06_24.md.

Usage:
    PERPLEXITY_API_KEY=... .venv/bin/python \
        apps/alphalens-research/scripts/discover_lane_experiment.py --last 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
from pathlib import Path

import pandas as pd

from alphalens_pipeline.data.alt_data.yfinance_client import get_default_yfinance_client
from alphalens_pipeline.literature_scanner.perplexity_client import PerplexityClient
from alphalens_pipeline.thematic.config.universe import load_input_universe
from alphalens_research.discover_lane.compare import compare_candidates
from alphalens_research.discover_lane.enrich import enrich_candidates
from alphalens_research.discover_lane.models import BriefCandidate, DateBlock
from alphalens_research.discover_lane.parse import parse_discover_response
from alphalens_research.discover_lane.prompt import build_discover_prompt
from alphalens_research.discover_lane.render import render_report

logger = logging.getLogger("discover_lane_experiment")

DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"
DEFAULT_OUT_DIR = Path.home() / ".alphalens" / "discover_lane_experiment"


def _brief_dates(briefs_dir: Path, last: int) -> list[str]:
    dates = sorted(p.stem for p in briefs_dir.glob("*.parquet"))
    return dates[-last:]


def _load_brief(briefs_dir: Path, date_iso: str) -> list[BriefCandidate]:
    df = pd.read_parquet(briefs_dir / f"{date_iso}.parquet")
    out: list[BriefCandidate] = []
    for _, row in df.iterrows():
        mcap = row.get("market_cap")
        out.append(
            BriefCandidate(
                ticker=str(row["ticker"]).upper(),
                company=str(row.get("company_name", "")),
                theme=str(row.get("theme", "")),
                source_event_title=str(row.get("source_event_title", "")),
                mcap=float(mcap) if pd.notna(mcap) else None,
            )
        )
    return out


def _cached_ask(client: PerplexityClient, date_iso: str, cache_dir: Path):
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{date_iso}.json"
    if cache_path.exists():
        raw = json.loads(cache_path.read_text())
        return raw["content"], raw["search_results"]
    d = dt.date.fromisoformat(date_iso)
    after = (d - dt.timedelta(days=7)).strftime("%m/%d/%Y")
    before = d.strftime("%m/%d/%Y")
    result = client.ask_with_citations(
        build_discover_prompt(date_iso),
        search_context_size="high",
        search_after_date_filter=after,
        search_before_date_filter=before,
    )
    cache_path.write_text(
        json.dumps({"content": result.content, "search_results": result.search_results})
    )
    return result.content, result.search_results


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Discover-lane experiment")
    ap.add_argument("--last", type=int, default=3, help="number of latest brief dates")
    ap.add_argument("--briefs-dir", type=Path, default=DEFAULT_BRIEFS_DIR)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        raise SystemExit("PERPLEXITY_API_KEY is required")

    client = PerplexityClient(api_key=api_key)
    yf = get_default_yfinance_client()
    universe = {t.upper() for t in load_input_universe()}
    cache_dir = args.out_dir / "cache"

    blocks: list[DateBlock] = []
    for date_iso in _brief_dates(args.briefs_dir, args.last):
        brief_path = args.briefs_dir / f"{date_iso}.parquet"
        if not brief_path.exists():
            logger.warning("no brief parquet for %s; skipping", date_iso)
            continue
        logger.info("processing %s", date_iso)
        content, search_results = _cached_ask(client, date_iso, cache_dir)
        discover = enrich_candidates(
            parse_discover_response(content, search_results),
            yf_client=yf,
            universe=universe,
        )
        brief = _load_brief(args.briefs_dir, date_iso)
        blocks.append(
            DateBlock(
                date=date_iso, discover=discover, brief=brief,
                comparison=compare_candidates(discover, brief),
            )
        )

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"report_{stamp.replace(':', '').replace('-', '')}.html"
    out_path.write_text(render_report(blocks, generated_stamp=stamp))
    logger.info("wrote %s (%d dates)", out_path, len(blocks))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-run --help to verify imports + argparse**

Run: `cd /Users/jacoren/Developer/Personal/AlphaLens/.claude/worktrees/experiment+discover-lane && .venv/bin/python apps/alphalens-research/scripts/discover_lane_experiment.py --help`
Expected: argparse help text prints (no ImportError).

- [ ] **Step 3: Commit**

```bash
git add apps/alphalens-research/scripts/discover_lane_experiment.py
git commit -m "feat(discover-lane): CLI orchestration script"
```

---

### Task 9: Full suite green + regression guards

**Files:** none (verification only).

- [ ] **Step 1: Run the new package's tests together**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest discover -s tests/discover_lane -t . -v`
Expected: PASS (all tests across Tasks 1–7).

- [ ] **Step 2: Run the doctrine guard tests touched by this work**

Run: `cd apps/alphalens-research && ../../.venv/bin/python -m unittest tests.test_layer_status tests.test_module_dependencies tests.test_no_polish_chars tests.test_no_raw_perplexity_http tests.test_no_raw_yfinance_http -v`
Expected: PASS (the new package is RESEARCH_ONLY, imports respect the DAG, no raw vendor HTTP, no Polish chars).

- [ ] **Step 3: If any guard fails, fix inline and re-run**

Common fixes: add `__status__` if `test_layer_status` complains; route any stray HTTP through the canonical client.

---

## Operator run (post-merge, not part of TDD)

After merge, generate the report on the VPS or Mac (wherever `~/.alphalens/thematic_briefs/` and `PERPLEXITY_API_KEY` are present):

```bash
PERPLEXITY_API_KEY=... .venv/bin/python \
    apps/alphalens-research/scripts/discover_lane_experiment.py --last 3
open ~/.alphalens/discover_lane_experiment/report_*.html
```

Cost: ~a few cents to ~$0.10 per date (sonar-pro, high context); re-runs hit the raw cache and are free.

## Self-Review notes

- **Spec coverage:** §3 architecture → Tasks 1–8; §4 components → Tasks 1,3,4,5,6 + render; client extension (§3) → Task 2; PIT date filters (§5) → Task 8 `_cached_ask`; raw cache (§5) → Task 8; HTML report (§6) → Task 7 + 8; error handling (§7) → parse (Task 4), enrich-keeps-unresolved (Task 5), missing-parquet + missing-key (Task 8); non-goals (§8) honored (no gates/score/EDGE/Django/web); testing (§9) → Tasks 1–7 + Task 9.
- **Type consistency:** `DiscoverCandidate`/`BriefCandidate`/`ComparisonResult`/`DateBlock` defined in Task 1 and reused verbatim; `AskResult` defined in Task 2; `parse_discover_response(content, search_results)` signature matches Task 8 usage; `enrich_candidates(..., yf_client=, universe=)` matches; `market_cap(ticker)` matches the real `YFinanceClient` surface.
- **Known limitation:** if chat-API date filters prove too coarse for strict PIT, run on the latest dates only (look-ahead minimal) and note it — recorded in spec §5.
