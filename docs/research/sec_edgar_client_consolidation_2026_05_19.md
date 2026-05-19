# SEC EDGAR client consolidation — 2026-05-19

Status: SHIPPED.

## Why

A 2026-05-19 audit asked: does every external vendor in this repo have
exactly one HTTP client? For SEC EDGAR the answer was **no — 4 shadow
clients** alongside the canonical `SecEdgarClient`:

| Shadow site | Endpoints | UA env (forked) | Throttle | Retry |
|---|---|---|---|---|
| `alphalens/watchdog/sources/edgar.py` | `cgi-bin/browse-edgar` (atom), `Archives/edgar/data/{cik}/{acc}/{index.json,xml,FilingSummary.xml,primary.html}` | config dict (no env) | manual `time.sleep` per-ticker | bare `raise_for_status` |
| `alphalens/watchdog/sources/cik_loader.py` | `files/company_tickers.json` | constructor param | none | bare `raise_for_status` |
| `alphalens/thematic/verification/tenk_grep.py` | `files/company_tickers.json`, `data.sec.gov/submissions/CIK*.json`, `Archives/edgar/data/{cik}/{acc}/{primary_doc}` | `THEMATIC_USER_AGENT` | none | default urllib timeout 30 s |
| `alphalens/thematic/verification/etf_holdings.py` | `efts.sec.gov/LATEST/search-index`, `Archives/edgar/data/{cik}/{adsh}/primary_doc.xml` | `THEMATIC_USER_AGENT` | none | default urllib timeout 20 s |

Three forked UA strings, no coordinated 10 req/s budget. SEC enforces
fair-access at the IP level — a single 403 from any of these shadow paths
takes down EVERY SEC consumer in the repo at once: paradigm-14 PEAD
backfill, Layer 1 watchdog, thematic verification, EDGAR fundamentals.
PR #159 (just-merged EDGAR fundamentals) is the only path that already
went through `SecEdgarClient`. This change cleans up the pre-existing
fragmentation.

## What changed

### `SecEdgarClient` surface — extended, not redesigned

Promoted three private helpers to public:

```python
def get_json(url: str) -> dict[str, Any]:    # public _get_json
def get_bytes(url: str) -> bytes:            # public _get_bytes
def get_text(url: str, *, encoding="utf-8") -> str   # new — decodes bytes
```

All three share the existing throttle (10 req/s), retry/backoff (60 s on
429, 10/30 s exp on 5xx, 5/15 s on network errors, max 3 attempts), and
User-Agent contract (must contain `@` or `http`).

Added a module-level lazy singleton:

```python
ALPHALENS_DEFAULT_USER_AGENT = "AlphaLens pajakkamil@gmail.com"
USER_AGENT_ENV = "SEC_EDGAR_USER_AGENT"

def get_default_sec_client() -> SecEdgarClient:
    """Reads SEC_EDGAR_USER_AGENT once on first call;
    falls back to ALPHALENS_DEFAULT_USER_AGENT. Subsequent
    calls return the same instance — one shared throttle for
    every SEC consumer in the process."""
```

`_reset_default_client_for_tests()` clears the cache between tests.

### Per-shadow-site migrations

| Site | Before | After |
|---|---|---|
| `cik_loader.py` | `requests.get(SEC_TICKERS_URL, headers={"User-Agent": …})` | `sec_client.fetch_company_tickers()` via injected `sec_client` (falls back to `get_default_sec_client()`) |
| `watchdog/sources/edgar.py` | `requests.get(url, params=…, headers=…)` for atom / index.json / xml / FilingSummary / 8-K HTML | `sec_client.get_text(url)` with URL pre-built via `urlencode`; per-ticker `time.sleep(rate_limit_seconds)` dropped — global throttle subsumes it |
| `thematic/verification/tenk_grep.py` | `urllib.request.urlopen(_http_get(...))` for company_tickers + submissions + 10-K HTML | `get_default_sec_client().{fetch_company_tickers, fetch_submissions, get_text}` |
| `thematic/verification/etf_holdings.py` | `urllib.request.urlopen(_http_get(...))` for efts search + primary_doc.xml | `get_default_sec_client().{get_json, get_bytes, get_text}` |

File-based caches at each site (watchdog `SeenEventStore` +
`company_tickers.json` 7-day TTL, `~/.alphalens/thematic_tenk/*.txt`,
`~/.alphalens/thematic_etf_holdings/*.parquet`) are **unchanged** — only
the HTTP transport is centralised.

Caller-facing signatures (`CIKLoader.get_cik`, `SECEdgarSource.detect`,
`fetch_10k_text`, `fetch_holdings`, `is_in_thematic_etf`) are unchanged —
zero behaviour change for consumers including the launchd watchdog.

### UA env consolidation

| Before | After |
|---|---|
| `SEC_EDGAR_USER_AGENT` (canonical, used by EDGAR fundamentals + CLI) | `SEC_EDGAR_USER_AGENT` (the only SEC UA env in the repo) |
| `THEMATIC_USER_AGENT` (thematic verification, defaulted to a different string) | deleted — operator must remove from local `.env` |
| `WATCHDOG_USER_AGENT` (watchdog CLI) | deleted — operator must remove from local `.env` |

`deploy/docker/.env.example` documents the single SEC EDGAR UA env.

### Enforcement test

`tests/test_no_raw_sec_http.py` fails red when ANY production file
(`alphalens/`, `alphalens_cli/`, `scripts/`) contains BOTH a SEC URL
fragment (`data.sec.gov`, `www.sec.gov/Archives`, `efts.sec.gov`,
`cgi-bin/browse-edgar`, `files/company_tickers`) AND a raw HTTP call
(`urllib.request.urlopen(`, `urllib.request.Request(`, `requests.get(`).
The canonical client uses `self._session.get(...)` which doesn't trip
the pattern. Conjunction (URL + raw HTTP in the same file) avoids false
positives on legitimate URL-build-then-pass-to-client patterns
(`etf_holdings.SEC_SEARCH_URL` is fine because the file's only HTTP call
is through `get_default_sec_client().get_json`).

## Verification

- Per-site test suites green: `tests.test_alt_data_sec_edgar_client` (33),
  `tests.test_watchdog_cik_loader` (8), `tests.test_watchdog_sources_edgar`
  (8), `tests.test_watchdog_edgar_enhanced` (5), `tests.thematic.test_tenk_grep`
  (32), `tests.thematic.test_etf_holdings` (27), `tests.thematic.test_edgar_adapter`
  (10).
- Enforcement test `tests.test_no_raw_sec_http` green (no shadow callers
  remain).
- Full repo test suite green.
- VPS smoke: `alphalens watchdog run-once` from clean SEC_EDGAR_USER_AGENT
  env still detects the same set of EDGAR events end-to-end (manual gate
  before merge).

## Operator action items

1. Remove `THEMATIC_USER_AGENT` and `WATCHDOG_USER_AGENT` from local
   `.env` and `deploy/docker/.env` (operator-managed; out of scope of
   this commit).
2. Confirm `SEC_EDGAR_USER_AGENT` is set on the VPS before the next
   thematic pipeline run (otherwise the singleton falls back to
   `ALPHALENS_DEFAULT_USER_AGENT` — still valid per SEC contract, but
   the operator contact should be explicit for any production deploy).

## Follow-up consolidations (separate PRs)

The same vendor-client audit flagged two more fragmentations:

- **Alpha Vantage**: `alphalens/data/fundamentals/fetcher.py` runs a
  parallel `urllib` path to the same AV API that
  `alphalens/data/alt_data/av_earnings_client.py` already covers — no
  shared throttle or cache.
- **Gemini**: `alphalens/backtest/llm_scorers.py` and
  `alphalens/thematic/mapping/orchestrator.py` each instantiate
  `genai.Client()` directly — no unified API key resolution, retry, or
  usage instrumentation.

Polygon has two clients on non-overlapping endpoints (lower priority).
yfinance / GDELT / SimFin / FRED / Perplexity / Telegram /
iVolatility are clean per the audit.
