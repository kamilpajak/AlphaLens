# ADR 0009 — Django + DRF replaces FastAPI + SQLite cache

- **Status:** Accepted (2026-05-22)
- **Supersedes:** the `alphalens/api/` FastAPI module shipped in
  PRs #176-#181 (briefs REST API rollout)
- **References:**
  - `apps/alphalens-django/docs/migration-log.md` — full F1-F8 history
  - `apps/alphalens-django/docs/openapi-parity/parity-report.md` — contract diff
  - PRs #176-#181 (legacy FastAPI rollout, now decommissioned)

## Context

The briefs REST API shipped in May 2026 as a FastAPI app backed by a
read-only SQLite cache rebuilt from `~/.alphalens/thematic_briefs/*.parquet`.
This was the minimum viable surface — it served a single SvelteKit SPA
behind Cloudflare Access.

It quickly accumulated impedance mismatches:

- 70-column schema duplicated four ways (`schema.py` dataclasses + Pydantic
  response models + raw SQL DDL + parquet column list)
- Denormalised `*_str` columns shipped just because SQLite JSON queries
  were awkward
- Read-only architecture meant feature work like paper-trade ledger or
  user notes was a non-starter
- No real authentication — CF Access at the edge was the only gate, and
  the API itself trusted any caller reaching `127.0.0.1:8086`
- No migration story: schema changes required `DROP TABLE` + full rebuild

## Decision

Replace the FastAPI app with a Django 6.0 + DRF backend, keeping the
parquet pipeline output as the canonical write target. The Django stack
owns:

- ORM models (`Brief`, `DayMeta`) with native composite PK and JSONField
- DRF viewsets serving the same `/v1/*` contract (parity verified
  endpoint-by-endpoint and snapshotted as a pytest gate)
- Cloudflare Access JWT verification server-side (`auth_cf` app), so
  the API enforces auth even if the edge fails
- Parquet → Postgres sync via a `manage.py rebuild_briefs_cache`
  management command, invoked by the daily systemd timer

The pipeline (`alphalens.thematic.*`, `alphalens.watchdog.*`, etc.)
stays intact — Django only owns the presentation surface and an
opinionated cache.

## Greenfield framing

The migration is **greenfield**: no live external traffic, no rollback
plan, no parallel deploy. The original migration plan included a
canary 5% nginx routing step; it was dropped in F7 because the
brownfield assumption (existing traffic worth protecting) didn't hold.
F8 deletes `alphalens/api/`, the legacy CLI subcommand, the legacy
docker-compose service, and the FastAPI/uvicorn root dependencies in
one commit.

## Consequences

### Positive

- Single source of truth for the schema (Django model → migration →
  serializer derives shape automatically)
- Real auth: CF Access JWT verified with JWKS in-process
- Schema migrations are version-controlled and reversible
- Postgres backend opens up paper-trade ledger, user annotations,
  feedback collection without architecture changes
- `drf-spectacular` generates OpenAPI 3.0 with zero warnings; contract
  diff against the legacy snapshot is a pytest gate, so future regressions
  surface immediately

### Negative

- New runtime dependency on Postgres (was just SQLite)
- Larger image: `python:3.13-slim` + Django + DRF + psycopg vs the
  minimal FastAPI footprint
- Two compose stacks during F1-F8 transition (legacy + django-prod);
  collapsed to one after F8

### Risks accepted

- Greenfield assumption means no rollback to FastAPI is possible after
  F8 merge. Verified by full-suite pytest + Playwright + manual
  `docker compose up` smoke before merging.
