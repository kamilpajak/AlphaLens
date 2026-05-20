# Gemini client consolidation — 2026-05-20

Status: SHIPPED.

## Why

Third and final part of the 2026-05-19 vendor-client audit (precedents:
`sec_edgar_client_consolidation_2026_05_19.md`,
`alphavantage_client_consolidation_2026_05_20.md`). For Gemini the answer
was **seven shadow sites**, each duplicating the same SDK-load helper,
the same `genai.Client(api_key=...)` construction, and the same
hand-rolled "install google-genai" error message:

| Shadow site | Use | Hoisting pattern |
|---|---|---|
| `alphalens/backtest/llm_scorers.py` | gemini_flash_tractability_scorer | one client per call |
| `alphalens/thematic/extraction/gemini_flash.py` | event extraction from news | hoisted once per daily batch |
| `alphalens/thematic/mapping/gemini_mapper.py` | theme → beneficiary candidates | hoisted by orchestrator across themes |
| `alphalens/thematic/mapping/orchestrator.py` | map orchestration | builds the hoisted client |
| `alphalens/thematic/argumentation/generator.py` | brief generation (Pro + Flash routed) | hoisted by argumentation orchestrator |
| `alphalens/thematic/argumentation/orchestrator.py` | brief orchestration | builds two clients (same instance for Pro + Flash) |
| `scripts/analyze_rejections.py` | one-off rejection classifier | one client per script run |

Five separate copies of `_load_genai_sdk()`. Three different patterns
for passing the SDK client + types module around (some carry both,
some only client + lazy types, generator's partial-hoist guard could
silently discard a passed client). One actionable error message
("`uv add google-genai`") in five places — diverged in wording.

This is the textbook vendor-fragmentation problem PR #160 (SEC) and
PR #162 (AV) just solved. Same pattern applied here.

## What changed

### New canonical `gemini_client.py`

```python
class GeminiClient:
    def __init__(self, api_key): ...
    @classmethod
    def from_env(cls) -> "GeminiClient": ...

    @property
    def sdk_client(self): ...  # underlying genai.Client escape hatch
    @property
    def types(self): ...        # google.genai.types module escape hatch

    def generate_content(self, *, model, contents, config) -> Any:
        return self._sdk_client.models.generate_content(...)
    def build_config(self, **kwargs) -> Any:
        return self._types.GenerateContentConfig(**kwargs)
```

- Lazy SDK import happens **once per process** (cached in module
  globals). Five copies of `_load_genai_sdk` collapse to one.
- `genai.Client(api_key=...)` builds at construction so a bad key is
  surfaced immediately, not on first call.
- `build_config(**kwargs)` saves call sites from importing the SDK's
  types module just to spell `types.GenerateContentConfig(...)`.
- `get_default_gemini_client()` lazy singleton shares one underlying
  SDK client across all adapters in the process — one HTTP keepalive
  pool, one place to inject quota tracking later.
- `_reset_default_client_for_tests()` + `_reset_sdk_cache_for_tests()`
  hooks so tests can pin a fake SDK in `sys.modules` without
  cross-contaminating later tests.

### Per-shadow-site migrations

Every site now follows the same pattern:

```python
# Old: 4 lines of SDK load + 2 lines of client construction
def _load_genai_sdk(): ...
genai, types = _load_genai_sdk()
client = genai.Client(api_key=key)
client.models.generate_content(
    model=m, contents=p,
    config=types.GenerateContentConfig(response_schema=..., ...),
)

# New: one line of resolution + canonical methods
gemini_client = gemini_client or get_default_gemini_client()
gemini_client.generate_content(
    model=m, contents=p,
    config=gemini_client.build_config(response_schema=..., ...),
)
```

The `(client, types_mod)` parameter pair in `generate_brief` and
`extract_one` collapses to a single `gemini_client=` argument. The
partial-hoist guard in the brief generator (which raised
`ValueError: hoisted clients require types_mod`) is gone — the
canonical client encapsulates both pieces so partial hoisting is no
longer possible by construction.

### Enforcement test — `tests/test_no_raw_gemini_sdk.py`

Mirror of `tests/test_no_raw_{sec,av}_http.py`. Catches:

- `from google import genai`
- `from google.genai import ...`
- `from google.generativeai import ...`
- `import google.generativeai`
- `genai.Client(...)` — bare shadow construction
- `langchain_google_genai` (legacy LangChain chain — used in the
  `archive/guru` closed module which is exempt via path prefix)

Word-boundary regex (negative lookbehind on `\w.`) so canonical
`self._sdk_client.models.generate_content` doesn't match. Positive
control test pins both directions (shapes that MUST match, shapes that
MUST NOT) so a regex that rots to empty cannot silently let shadow
clients through.

Exempt paths:
- `alphalens/data/alt_data/gemini_client.py` (the canonical client)
- `alphalens/archive/` (ADR 0005 frozen anti-pattern catalog —
  `archive/guru/llm_scorer.py` uses the legacy `langchain_google_genai`
  chain; not refactored per project policy)

## Behavioural deltas

| Behaviour | Before | After |
|---|---|---|
| Number of `genai.Client` instances per process | 1 per call site that lazy-built (4+) | 1 (lazy singleton) — adapters with their own `api_key=` argument still build a fresh client on demand |
| Partial-hoist defensive guard in `generate_brief` | raised `ValueError: hoisted clients require types_mod` | gone — `gemini_client_pro` / `gemini_client_flash` are independent `GeminiClient` instances; partial hoist is no longer expressible |
| SDK-missing error message | five copies, slightly diverged in wording | one canonical message in `_load_genai_sdk()` |
| Test seam | each site had its own `_call_gemini` wrapper that took `(client, types_mod, ...)` as positional + keyword | each `_call_gemini` now takes `(gemini_client, ...)` — one fewer parameter; tests update lambdas accordingly |
| Pro vs Flash routing | `client_pro` + `client_flash` + `types_mod` triple | `gemini_client_pro` + `gemini_client_flash` pair (same instance by default; can be split for tests) |

None of these are user-observable. All adapter-level return shapes,
log messages, and external API contracts are unchanged. The test suite
went green without any production-path behavioural assertion changes.

## Operator follow-ups

- None. `GOOGLE_API_KEY` env var is unchanged and still required.

## Out of scope

- Quota counting (per-second + per-day across all consumers). Lazy
  singleton lays the groundwork; natural follow-up once a consumer
  actually hits the wall.
- Migrating `alphalens/archive/guru/llm_scorer.py` off
  `langchain_google_genai`. ADR 0005 closed-layer policy: archive
  modules are frozen anti-pattern catalog. The enforcement test exempts
  the `archive/` prefix.
