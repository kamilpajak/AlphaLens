# REST API for thematic briefs — design memo

**Status:** LOCKED
**Date:** 2026-05-21
**Author:** Kamil + Claude
**Implements:** `alphalens/api/` (Python package), `deploy/docker/{Dockerfile.api, docker-compose.yml}`, `deploy/cloudflare/access_setup.md`
**Plan:** `/Users/jacoren/.claude/plans/distributed-puzzling-eagle.md`

## 1. Problem

Thematic briefs are produced as parquet files at `~/.alphalens/thematic_briefs/{date}.parquet` (16 candidates × 70 columns per file as of 2026-05-18). A daily systemd job runs `scripts/export_briefs_to_json.py` to materialise `web-data/days.json` + `web-data/days/{date}.json`, which the SvelteKit dashboard mounts read-only via nginx. That works for a single in-house web consumer but does not generalise:

- Future consumers (Telegram bot, mobile client, external scripts) would each need their own parquet reader or scrape JSON files shaped for one UI.
- Static JSON exports race against the daily timer — a consumer hitting the file mid-write would see partial data without atomic guarantees (mitigated today by tmp-file + rename, but not a stable contract).
- No versioned, documented contract: any change to the export script silently breaks downstream consumers.
- No filtering / pagination / per-resource fetch — every consumer downloads the full day payload.

## 2. Goal

A single read-only REST API (`alphalens-api`) serves as the source of truth for brief data. Browser users (web/) and programmatic consumers (bots, scripts) consume the same OpenAPI-documented contract. Cloudflare Access fronts the API for auth (Google SSO for browser; Service Tokens for machines), so the API itself stays auth-free.

## 3. Architecture

```
~/.alphalens/thematic_briefs/*.parquet            (producer: alphalens thematic brief)
            │  rebuild_from_parquet() — incremental by mtime, transaction per date
            ▼
~/.alphalens/api/briefs.db (SQLite, WAL, indexed)
            │
            ▼
FastAPI (uvicorn, sync handlers in threadpool) :8000 in container, 127.0.0.1:8081 on host
            │
            ▼
Cloudflare Tunnel ──► Cloudflare Access (Google SSO + Service Tokens)
            │
            ├──► web/ SvelteKit (CSR fetch via nginx /api/ proxy)
            ├──► Telegram bot (CF-Access-Client-{Id,Secret} headers)
            └──► External scripts (Service Token)
```

## 4. Decisions

| Decision | Choice | Why |
|---|---|---|
| **Stack** | FastAPI + uvicorn (new container) | Native pyarrow/pandas access; OpenAPI/Swagger auto-generated; Pydantic v2 already in repo. |
| **Source of truth** | SQLite cache built from parquet | Indexed filter/pagination without scanning parquet per request; parquet remains the canonical write target; SQLite is ephemeral and reproducible. |
| **DB engine** | SQLite (WAL mode) | Tiny volume (dozens × hundreds rows); embedded eliminates Postgres ops. WAL lets read traffic continue during rebuild. |
| **Async** | Sync handlers in FastAPI threadpool | SQLite is blocking; aiosqlite is overkill at this scale. Upgrade later if profiling demands. |
| **Versioning** | `/v1/` prefix from day-one | Breaking changes → `/v2/`. |
| **Pagination** | `limit/offset` + `meta.{total,limit,offset}` envelope | Cursor pagination over-engineered for dozens of items/day. |
| **Auth** | Cloudflare Access (free Zero Trust tier, ≤50 users) | Zero auth code in API; identity-aware; Service Tokens cover programmatic consumers. Vendor lock-in to Cloudflare is already paid (Tunnel fronts web/). |
| **Response envelope** | `{data: [...], meta: {total, limit, offset}}` for lists; bare object for single resource | Stable shape for pagination; bare object reduces nesting noise for single fetches. |
| **CORS** | Allowlist `web/` origin + deployed hostname | Cloudflare Access redirect domain (`*.cloudflareaccess.com`) added to allowlist. |
| **Cache rebuild trigger** | `alphalens api rebuild-cache` appended to `deploy/docker/run_thematic_day.sh` | Same systemd timer as brief generation; one extra step, no separate scheduling. |

## 5. Alternatives rejected

| Alternative | Rejected because |
|---|---|
| **SvelteKit `+server.ts` endpoints (Node, in existing web/)** | Web container is static-only (nginx serving SvelteKit prerendered + static JSON). Switching to Node adapter forces SSR everywhere; Python-side parquet access disappears unless we mirror logic in TS. Mixes consumer (web/) with producer (data API). |
| **FastAPI inside existing pipeline container** | Pipeline container runs cron jobs and exits; making it always-on mixes batch CLI concerns with long-running API. Single-responsibility violation; harder to scale/restart independently. |
| **Parquet direct (no SQLite cache)** | Per-request parquet scan + pandas filter is wasteful at API latency budget; no native indexed lookups. SQLite cache amortises parsing once per daily rebuild. |
| **Parquet + Redis cache** | Redis adds an external service for marginal benefit at this volume; SQLite already serves as read-optimised cache. |
| **Bearer token auth in FastAPI** | Single static token in `.env` is weaker than identity-aware Cloudflare Access (no SSO, no audit, no revocation UX, no MFA path). Reusable later as fallback if multi-channel needs emerge. |
| **GraphQL** | No client needs ad-hoc field projection; REST + OpenAPI is sufficient and friendlier for curl/scripts. |
| **WebSocket / SSE for push** | Daily cadence; HTTP polling on `/v1/stats` or `/v1/days?limit=1` suffices. |

## 6. Endpoint surface

OpenAPI auto-generated at `/openapi.json`; Swagger UI at `/docs`; ReDoc at `/redoc`.

| Method | Path | Query | Returns |
|---|---|---|---|
| GET | `/healthz` | — | `{status: "ok"}` (no DB) |
| GET | `/readyz` | — | `{status, db_path, last_rebuild_at, n_days, n_candidates}` |
| GET | `/v1/days` | `from`, `to`, `limit≤200`, `offset` | `{data: DayMeta[], meta: PageMeta}` |
| GET | `/v1/days/{date}` | — | `DayBrief` (metadata + `candidates[]`) |
| GET | `/v1/days/{date}/candidates` | `theme`, `min_score`, `limit≤200`, `offset` | `{data: Candidate[], meta: PageMeta}` |
| GET | `/v1/candidates/{date}/{ticker}` | — | `Candidate` (full 70-col schema) |
| GET | `/v1/themes` | `from`, `to` | `{data: ThemeSummary[]}` (distinct themes + counts) |
| GET | `/v1/themes/{theme}/candidates` | `from`, `to`, `limit≤200`, `offset` | `{data: Candidate[], meta: PageMeta}` |
| GET | `/v1/tickers/{ticker}/history` | `from`, `to`, `limit≤200` | `{data: Candidate[], meta: PageMeta}` |
| GET | `/v1/stats` | — | `{n_days, latest_date, total_candidates, top_themes: [{theme, n_days, n_candidates}]}` |

**Status codes:** `200` OK, `404` for missing date/ticker, `422` for Pydantic validation errors, `503` if SQLite cache is missing or unbuildable.

## 7. SQLite cache schema

```sql
CREATE TABLE briefs (
  date           TEXT NOT NULL,
  ticker         TEXT NOT NULL,
  -- 68 remaining columns mirroring parquet (TEXT / REAL / INTEGER)
  -- List arrays (gates_passed, theme_search_keywords, also_in_themes) stored as
  -- JSON-encoded TEXT; decoded back to list[str] in Pydantic deserialization.
  PRIMARY KEY (date, ticker)
);
CREATE INDEX idx_briefs_theme  ON briefs(theme);
CREATE INDEX idx_briefs_ticker ON briefs(ticker);
CREATE INDEX idx_briefs_score  ON briefs(layer4_weighted_score);

CREATE TABLE days_meta (
  date           TEXT PRIMARY KEY,
  n_candidates   INTEGER NOT NULL,
  n_themes       INTEGER NOT NULL,
  top_theme      TEXT,
  theme_counts_json TEXT NOT NULL,
  parquet_mtime  REAL NOT NULL,           -- incremental rebuild gate
  rebuilt_at     TEXT NOT NULL            -- ISO 8601 UTC
);

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);  -- schema_version, last_full_rebuild_at
```

`schema_version` starts at `1`. Bumping it triggers a full rebuild (drop + recreate).

## 8. Incremental rebuild contract

1. Open SQLite with `PRAGMA journal_mode=WAL`.
2. For each `*.parquet` in `~/.alphalens/thematic_briefs/`:
   - Compare file `st_mtime` to `days_meta.parquet_mtime` for that date.
   - If equal → skip.
   - Else → `BEGIN TRANSACTION; DELETE FROM briefs WHERE date=?; INSERT rows; UPSERT days_meta; COMMIT;`
3. Delete `days_meta` rows whose parquet file no longer exists (so the cache mirrors the directory).
4. UPSERT `meta.last_full_rebuild_at` only if a full rebuild was forced (schema bump or `--force`).

NaN floats are coerced to `None` before insert; lists are JSON-encoded; pandas `Timestamp` becomes ISO 8601 string.

## 9. Web migration (PR 3)

`web/src/routes/briefs/+page.ts`:
```
- const r = await fetch('/data/days.json');
- const days: DayIndexEntry[] = await r.json();
+ const r = await fetch('/api/v1/days?limit=200');
+ const { data: days } = await r.json();
```

`web/src/routes/brief/[date]/+page.ts`:
```
- const r = await fetch(`/data/days/${date}.json`);
- const brief: DayBrief = await r.json();
+ const r = await fetch(`/api/v1/days/${date}`);
+ const brief: DayBrief = await r.json();
```

nginx reverse proxy (added to web/ container config):
```
location /api/ { proxy_pass http://api:8000/; }
```

The static JSON exporter and `web-data/` volume mount are removed in PR 4 after a stabilization window.

## 10. Cloudflare Access setup (operator manual — `deploy/cloudflare/access_setup.md`)

1. Add Tunnel ingress: `hostname: api.briefs.alphalens.example, service: http://localhost:8081`.
2. Zero Trust → Access → Applications → "Self-hosted" → hostname.
3. Policy: `Include: Emails = pajakkamil@gmail.com` (browser SSO via Google IdP).
4. Service Token: `alphalens-bot-token`; save `CF-Access-Client-Id` + `CF-Access-Client-Secret` to `.env` (gitignored).
5. Smoke test (no token → 302 to Access login; with token → 200).

## 11. MVP exclusions

- ❌ Write endpoints (POST/PUT/DELETE) — read-only by design.
- ❌ WebSocket / SSE — daily cadence.
- ❌ GraphQL.
- ❌ Code-side rate limiting — Cloudflare edge handles.
- ❌ Cloudflare Access JWT validation inside FastAPI — network bind 127.0.0.1 is sufficient for MVP; JWT verification middleware can be added later for audit logging without breaking the API contract.
- ❌ SQLite migrations (Alembic) — schema is rebuilt from parquet, no historical schema to migrate.
- ❌ Redis / in-process cache — SQLite serves as the read-optimised cache.

## 12. Verification

See plan file for full smoke commands. Headlines:

- Unit: `python -m unittest discover tests/api -v` + `tests.test_layer_status` + `tests.test_no_polish_chars`.
- Local smoke: `alphalens api rebuild-cache` → `alphalens api serve` → curl `/healthz`, `/v1/days`, `/v1/days/2026-05-18`, `/openapi.json`.
- Docker smoke (PR 2 on VPS): `docker compose up -d api` → curl `127.0.0.1:8081/healthz`.
- Cloudflare smoke (PR 2 follow-up): browser SSO + Service Token curl + 302 unauthorised check.
- Zen codereview on PR 1 + PR 3 (mandatory per CLAUDE.md for shared API surface + web/).

## 13. Follow-up issues to file after PR 1

- Optional JWT validation middleware (`Cf-Access-Jwt-Assertion` header) for audit logging.
- `openapi-typescript` generation for `web/src/lib/api-types.ts`.
- Telegram bot adoption of the API (replaces direct parquet read if any).
- Public-domain endpoint exposure decision (currently only Cloudflare-fronted).
