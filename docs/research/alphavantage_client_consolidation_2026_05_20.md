# Alpha Vantage client consolidation — 2026-05-20

Status: SHIPPED.

## Why

The 2026-05-19 vendor-client audit (precedent: `sec_edgar_client_consolidation_2026_05_19.md`)
asked the same question for every external HTTP vendor. For Alpha Vantage
the answer was **two shadow clients with forked rate-limit detection and
no shared quota tracker**:

| Shadow site | Endpoints | Rate-limit class | Throttle | Cache |
|---|---|---|---|---|
| `alphalens/data/fundamentals/fetcher.py` | OVERVIEW, BALANCE_SHEET, CASH_FLOW, INCOME_STATEMENT | `AlphaVantageRateLimitError` (detects `rate limit` + `api key`) | none | none |
| `alphalens/data/alt_data/av_earnings_client.py` | EARNINGS | `AVRateLimitError` (detects `rate limit` + `api key` + `premium`) | 1.5 s between calls | per-ticker JSON in `cache_dir` |

Both bypassed each other's state. AV's free-tier quota is **25 requests
per day per API key**. A scheduled `alphalens watchdog run-once` invoking
the fundamentals fetcher could burn quota that the VPS systemd EARNINGS
backfill needed three minutes later — neither side would know. The two
exception types diverged on the "premium endpoint" signal too, so the
fundamentals path would not have detected an AV plan downgrade until
operators noticed the silent `{}` returns.

PR #161 (just-merged EDGAR fundamentals migration) was the trigger to
finish the vendor-hygiene work before the AV duplication compounded
further.

## What changed

### New canonical `alphavantage_client.py`

```python
class AlphaVantageClient:
    def __init__(self, api_key, *, throttle_seconds=0.0,
                 timeout=30.0, urlopen_fn=urlopen, sleep_fn=time.sleep):
        ...
    @classmethod
    def from_env(cls, **kw) -> "AlphaVantageClient":
        """Resolves ALPHA_VANTAGE_API_KEY."""
    def query(self, function: str, **params) -> dict[str, Any]:
        """Raises AVRateLimitError on quota/auth/premium signals,
           AVSchemaError on non-JSON / non-dict / Error Message."""
```

Single rate-limit phrase set covers all known AV quota responses
(`"rate limit"`, `"api key"`, `"premium"`). The `query()` contract
**raises** on `Error Message` rather than silently returning `{}` — the
caller decides whether to soft-fail. Throttle is opt-in (default 0)
because the two consumers have very different cadence: fundamentals
fetcher is bursty (one ticker at a time, manual ad-hoc), EARNINGS bulk
cache is paced (1.5 s between calls, ~25 tickers/day).

Lazy singleton `get_default_av_client()` reads
`ALPHA_VANTAGE_API_KEY` once on first call. Subsequent callers share
the instance — same throttle state, same quota visibility (when /
if a future PR adds quota counting).

### Per-shadow-site migrations

| Site | Before | After |
|---|---|---|
| `alphalens/data/fundamentals/fetcher.py` | Module-level `_make_av_request` builds URL, calls `urlopen`, parses JSON, scans for `Information` / `Error Message`. Raises `AlphaVantageRateLimitError` or returns `{}`. | `_make_av_request(function, symbol, *, client=None)` delegates to `client or get_default_av_client()`. Catches `AVSchemaError` → returns `{}` (preserves bundle partial-fill semantics). `AlphaVantageRateLimitError` kept as alias for `AVRateLimitError`. |
| `alphalens/data/alt_data/av_earnings_client.py` | `_default_fetcher(ticker)` reads env, builds URL, calls `urlopen`, parses JSON, raises `AVRateLimitError` / `AVSchemaError`. | `_default_fetcher(ticker)` is one line: `get_default_av_client().query("EARNINGS", symbol=ticker)`. Cache + throttle + batch orchestration unchanged. `AVRateLimitError` / `AVSchemaError` re-exported via `__all__` so downstream importers (scripts/av_earnings_daily_backfill, screeners/event_drift) keep their imports stable. |

### Enforcement test — `tests/test_no_raw_av_http.py`

Mirror of `tests/test_no_raw_sec_http.py`. Fails red when any file
outside `alphalens/data/alt_data/alphavantage_client.py` contains both
an `alphavantage.co` URL fragment AND a raw HTTP call (`urlopen(`,
`urllib.request.*`, `requests.*(`, `httpx.*(`, `aiohttp.*`). Regex
uses word boundaries so docstring prose like "burning further
requests." (period in a sentence) does not false-positive.

The SEC enforcement test's substring scan let bare `urlopen(` slip
through historically — caught here when the same test pattern was
copied. Both enforcement files now use the word-boundary regex form.

## Behavioural deltas

| Behaviour | Before | After |
|---|---|---|
| Premium-endpoint signal in fundamentals path | Silently treated as `Error Message` → returns `{}` | Raises `AVRateLimitError`, propagates through `_safe` so the batch aborts. |
| `Error Message` in EARNINGS path | Raised `AVSchemaError` (no cache write) | Same. |
| `Error Message` in fundamentals path | Returned `{}` with warning log | Same (adapter catches `AVSchemaError` → `{}`). |
| Throttle for fundamentals fetcher | None | Still none by default. Operators can pass `throttle_seconds=` to `AlphaVantageClient(...)` for explicit pacing. |
| Quota tracking | None | None. Lazy singleton lays the groundwork; a future PR can add per-second + per-day counters in one place. |

Only the first row is a contract change. The previous behaviour was a
silent silent-fail under plan downgrade; the new behaviour aborts the
batch loudly, which is what operators want for a 25/day quota.

## Operator follow-ups

- None. `ALPHA_VANTAGE_API_KEY` env var is unchanged and still required.
- No `.env` cleanup needed.

## Out of scope

- Quota counting (per-second + per-day) — natural follow-up once a
  consumer hits the wall.
- Gemini consolidation — separate PR per the 2026-05-19 audit.
