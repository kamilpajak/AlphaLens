# Polygon Canonical-Client Consolidation — 2026-05-22

**Status:** COMPLETED
**PR:** feature/polygon-canonical-client
**Trigger:** `docs/research/thematic_verification_gate_audit_2026_05_22.md` flagged Polygon 429 flakiness as the principal cause of `kept 0 vs kept 3` candidate-yield variance in the daily thematic brief; this was the third "rate limit hit during press verification" incident in two weeks.

## §1 Problem statement

Polygon had **three shadow HTTP clients** in the production codebase prior to this consolidation, plus the modern `PolygonShortInterestClient`:

| File | HTTP stack | Auth | Retry | Rate budget |
|---|---|---|---|---|
| `alphalens/thematic/sources/polygon_news.py` | stdlib `urllib.request` | `apiKey=` query param | none | 13s sleep between pages (local) |
| `alphalens/thematic/verification/recent_press.py` | stdlib `urllib.request` | `apiKey=` query param | none | **zero** (root cause of the 429 cascade) |
| `scripts/build_optionable_universe.py` | `requests.get` | `apiKey=` query param | partial (2s sleep on 429, no max) | none |
| `alphalens/data/alt_data/polygon_short_interest.py` | `requests.Session` | Bearer header | exponential backoff | none |

The Polygon Starter tier ($29/mo) enforces **5 req/min globally per API key**. With four uncoordinated clients (and an undeclared `requests` dependency that worked only via yfinance's transitive pull), the 5-req window was contended even within a single daily pipeline run: ingest could exhaust the budget before verification's first per-ticker fallback, triggering the silent `gates_unknown` degradation the 2026-05-22 audit caught.

The doctrine in `CLAUDE.md ## Workflow conventions` already mandates **"One canonical HTTP client per external vendor"** for SEC EDGAR (`sec_edgar_client.py`), Alpha Vantage (`alphavantage_client.py`), and Gemini (`gemini_client.py`), enforced by `tests/test_no_raw_{sec,av,gemini}_*.py`. Polygon was the only major data vendor still without canonical consolidation.

## §2 Architectural decisions

1. **Mirror the SecEdgarClient shape exactly.** Flat public methods (`get_news_range`, `get_short_interest`, `get_options_contracts`) + escape hatches (`get_json`, `get_bytes`) + dual-layer retry + module-level lazy singleton via `get_default_polygon_client()` + `_reset_default_client_for_tests()` hook. Same structural template as the three existing canonical clients; consistency is the goal, not novelty.

2. **Bearer auth only.** `Authorization: Bearer <key>` header on every request. The API key never appears in URLs, `next_url` pagination cursors, log lines, or cache filenames. The previously-modern `PolygonShortInterestClient` already used Bearer; news modules migrate from query-param to Bearer. Defensive: `_strip_apikey_from_url()` removes any `apiKey` query param the server might echo in `next_url` responses (Polygon's pagination sometimes carries credentials from older clients).

3. **`PolygonShortInterestClient` becomes a thin domain wrapper.** Retains the per-ticker parquet cache, FINRA dissemination-lag PIT contract (`_is_available_at`), and `ShortInterestRecord` dataclass surface. Internal HTTP delegates to `PolygonClient.get_short_interest()` via DI. The wrapper's `PolygonShortInterestError` / `PolygonShortInterestAuthError` exception names are preserved as aliases of `PolygonError` / `PolygonAuthError` so existing `except` clauses keep working.

4. **Defer archive callers.** `alphalens/archive/screeners/lean/polygon_client.py` and its three live script consumers (`survivorship_probe.py`, `backfill_delisted_2021_2024.py`, `fetch_survivorship_ohlcv.py`) stay on their current shadow client. The enforcement test (`tests/test_no_raw_polygon_http.py`) excludes `alphalens/archive/` per ADR 0005 — same exemption pattern as `tests/test_no_raw_gemini_sdk.py`. Archive is the frozen anti-pattern catalog; migrating it would defeat the catalog's purpose.

5. **Declare `requests` as a top-level dep.** Previously undeclared but used by `polygon_short_interest.py` + `archive/screeners/lean/polygon_client.py` + `archive/guru/polygon_fundamentals.py`. Worked only because yfinance transitively pulled it. Adding `requests>=2.32.0` to `[project.dependencies]` removes a latent packaging-fragility footgun.

## §3 Rate limit semantics

PolygonClient enforces 5 req/min Starter tier via two layers:

- **Proactive throttle** (`_throttle()`): spacing between requests measured against `time.monotonic`; sleeps to enforce `_min_interval_s = 60 / rate_limit_per_min` (default 12s for 5 req/min). First call after construction does not sleep.
- **Reactive retry** (`_request()`): on 429, parse `Retry-After` header, clamp into `[13s floor, 60s ceiling]`, sleep that long, retry. The floor protects against pathologically-low server signals; the ceiling protects against misconfigured proxies returning multi-hour values.

`_MAX_REQUEST_ATTEMPTS = 4` (1 + 3 retries). 5xx errors use exponential backoff `(5, 15, 30s)`. Transient network errors (`requests.RequestException` superclass — catches SSL, timeout, ConnectionError, TooManyRedirects) retry up to 3 times with `(5, 15s)` backoffs.

Distinct exception types let callers soft-fail intelligently:
- `PolygonAuthError` (401) — no retry, immediate raise; configuration problem
- `PolygonRateLimitError` (persisted 429) — caller can record `gates_unknown` instead of crashing
- `PolygonError` (other permanent 4xx, exhausted network retries) — non-transient

## §4 Test strategy

**Unit tests** (`tests/test_polygon_client.py`, 29 tests): cover auth (Bearer header always sent, api_key never in URL/params, empty key rejected, `from_env` reads env), throttle (spacing enforced across calls), retry (429+Retry-After floor/honor/ceiling, persisted 429, 5xx backoff, 401 immediate raise, permanent 4xx, transient network, exhausted network), pagination (next_url follow + apiKey strip, max_pages, max_items, ticker param, naive datetime rejected), short interest pagination, options contracts param passthrough, get_json escape hatch, lazy singleton identity + reset hook.

**Enforcement test** (`tests/test_no_raw_polygon_http.py`, 2 tests): mirror of `test_no_raw_av_http.py`. Conjunction logic — file flagged only if it contains BOTH a polygon.io URL fragment AND a raw HTTP call pattern (`urlopen(`, `urllib.request.\w+`, `requests.\w+(`, `httpx.\w+(`, `aiohttp.\w+`). Word-boundary negative lookbehind exempts `self._session.get(...)` (DI pattern) and docstring prose. Positive-control test asserts both shadow samples (must match) and safe samples (must not) — prevents regex rot from silently letting shadow clients re-enter.

**Existing test migration** (3 files): `tests/thematic/test_polygon_news.py`, `tests/thematic/test_recent_press.py`, `tests/test_polygon_short_interest_client.py`. All moved from mocking `_http_get_json` (function-level) / `requests.Session.get` (library-level) to mocking `PolygonClient.get_news_range` / `PolygonClient.get_short_interest` (client-level). This is the supported post-consolidation mock layer and matches the SecEdgar / AlphaVantage / Gemini test patterns. URL-construction assertions were dropped — those characteristics live in `tests/test_polygon_client.py` where they're tested once authoritatively.

## §5 Migration scope

| File | Change | LOC delta |
|---|---|---|
| `alphalens/data/alt_data/polygon_client.py` (NEW) | Canonical client | +330 |
| `tests/test_polygon_client.py` (NEW) | Unit tests | +350 |
| `tests/test_no_raw_polygon_http.py` (NEW) | Enforcement | +175 |
| `alphalens/thematic/sources/polygon_news.py` | Thin wrapper over PolygonClient | -45 |
| `alphalens/thematic/verification/recent_press.py` | Thin wrapper; `api_key` → `client` DI | -55 |
| `alphalens/data/alt_data/polygon_short_interest.py` | Domain wrapper; HTTP via DI | -50 |
| `alphalens/thematic/mapping/orchestrator.py` | `polygon_key: str` → `polygon_client: PolygonClient | None` | ~30 |
| `alphalens/thematic/news_ingest.py` | Drop `api_key` parameter | ~5 |
| `scripts/build_optionable_universe.py` | direct `requests.get` → `client.get_options_contracts` | -25 |
| `tests/thematic/test_polygon_news.py` | Mock at client boundary | ~140 (rewrite) |
| `tests/thematic/test_recent_press.py` | Mock at client boundary | ~160 (rewrite) |
| `tests/test_polygon_short_interest_client.py` | Mock at client boundary | ~120 (rewrite) |
| `pyproject.toml` | Declare `requests>=2.32.0` | +1 |
| `CLAUDE.md` | Add Polygon to canonical-client pinned list | ~5 |

## §6 Deferred follow-ups

- **Archive client migration**: `alphalens/archive/screeners/lean/polygon_client.py` + 3 script consumers. Per ADR 0005 frozen.
- **`alphalens/archive/guru/polygon_fundamentals.py`**: same.
- **Polygon Developer tier upgrade** ($79/mo, unlimited rate): pure config change later — `PolygonClient(api_key, rate_limit_per_min=999)` removes the proactive throttle without code edits.
- **Quota tracking middleware** (per-day request budgeting, observability hooks): future PR once daily usage data accumulates.
- **`web/tests/fixtures/api-mock/`** regeneration: out of scope for backend HTTP refactor.

## §7 References

- `docs/research/sec_edgar_client_consolidation_2026_05_19.md` — structural template (4 shadow clients → 1)
- `docs/research/alphavantage_client_consolidation_2026_05_20.md` — opt-in throttle pattern + caller-owned retry orchestration
- `docs/research/gemini_client_consolidation_2026_05_20.md` — escape-hatch properties + lazy SDK load
- `docs/research/thematic_verification_gate_audit_2026_05_22.md` — incident that triggered this consolidation
- `tests/test_no_raw_sec_http.py`, `tests/test_no_raw_av_http.py`, `tests/test_no_raw_gemini_sdk.py` — enforcement-test templates
- ADR 0005 — closed-layer policy (`alphalens/archive/` exemption rationale)
