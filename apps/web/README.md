# AlphaLens dashboard (SvelteKit SPA)

Thematic-briefs dashboard. Pure SPA via `@sveltejs/adapter-static`
(`apps/web/svelte.config.js`); reaches the Django briefs API at runtime
via `VITE_API_BASE` (or same-origin `/api/*` for the local Docker stack).

## Local dev

```sh
pnpm install
pnpm dev          # http://localhost:5173
```

The `predev` hook runs `scripts/sync-research-docs.mjs` to copy the
markdown/JSON evidence files referenced from `/experiments` into
`static/docs/research/`. The script exits 1 on missing references.

Plain `pnpm dev` proxies `/api/*` to `VITE_API_TARGET` (default
`http://127.0.0.1:8081`) — i.e. a local backend — and the Playwright smoke
suite serves hand-authored fixtures, so neither needs the production API.

### `pnpm dev:vps` — render the latest LIVE brief

To develop against the real latest brief from the VPS instead of fixtures:

```sh
cp .env.example .env                 # then set DEV_API_URL=https://api.<your-domain>
cloudflared access login --app=https://api.<your-domain>   # one-time, opens Google SSO
pnpm dev:vps                         # http://localhost:5173, real data
```

`scripts/dev-api-proxy.mjs` starts a tiny local proxy that forwards the SPA's
`/v1/*` calls to `DEV_API_URL`, attaching a short-lived Cloudflare Access token
(`cloudflared access token`), then launches `pnpm dev` pointed at it. Read-only
(GET) — it is a viewer, not a write path. The token lasts ~24h; the proxy
refreshes it automatically on a 401. `DEV_API_URL` lives in the gitignored
`.env`, so your domain stays out of the repo.

## Production build

```sh
pnpm build        # writes apps/web/build/
```

`prebuild` runs the same sync-research-docs hook.

## Deploy targets

Two paths are supported. Production uses **Cloudflare Pages**; the local
Docker stack uses the **nginx bind-mount** path for offline testing.

### Cloudflare Pages (production)

Settings (configure in CF Pages dashboard → project → Settings):

| Field | Value |
|-------|-------|
| Production branch | `main` |
| Root directory | `apps/web` |
| Build command | `corepack enable && pnpm install --frozen-lockfile && pnpm build` |
| Build output directory | `build` |
| Node version | `24` (set `NODE_VERSION` env var) |
| Env var (Production) | `VITE_API_BASE=https://api.<your-domain>` |

`static/_redirects` ships an SPA fallback so client-side routes
(`/brief/<date>`, `/experiments`, etc.) resolve after a hard refresh.

The API is reached **cross-origin** at `VITE_API_BASE`. Django prod
settings already read `CORS_ALLOWED_ORIGINS` from env — add the Pages
URL there before first deploy.

#### Auth model — same-domain cookies (Path A)

The Django API is gated by Cloudflare Access (`auth_cf` middleware).
The SPA reaches the API cross-origin via `VITE_API_BASE`. The chosen
auth path is **same-domain cookies**:

- SPA at `app.<domain>` + API at `api.<domain>` share an eTLD+1
- Browser sends the `CF_Authorization` session cookie cross-origin
- Django reads the cookie via existing `auth_cf` middleware
- No SPA code change; user logs in via CF Access (Google SSO) once per session

Required Django prod env (`apps/alphalens-django/config/settings/`):
- `CORS_ALLOW_CREDENTIALS=True`
- `CORS_ALLOWED_ORIGINS=https://app.<domain>` (literal production URL)
- `CORS_ALLOWED_ORIGIN_REGEXES=^https://[\w-]+\.<project>\.pages\.dev$`
  for preview branch deploys (Django's CORS_ALLOWED_ORIGINS is literal-
  match only)

Required CF Zero Trust dashboard (Access app for `api.<domain>`):
- Enable **"Bypass Access for HTTP OPTIONS requests"** — browsers' CORS
  preflights don't carry the `CF_Authorization` cookie; without bypass
  they get 302/403 before reaching Django

Service Tokens are NOT used — they cannot live in browser JS (the
Client Secret would be extractable). Rejected alternative was a CF
Pages Function edge proxy that injected Service Token headers
server-side; rejected because Path A is zero-code and the latency is
identical.

### Local Docker stack (dev / offline test)

`deploy/docker/django-prod/docker-compose.yaml` (with the
`docker-compose.override.yaml` planned in Phase 3 of migration B) runs
nginx with a bind-mount of `apps/web/build/`. Build BEFORE `up`
otherwise nginx mounts an empty dir on macOS — see CLAUDE.md
"workflow conventions" for the gotcha. Same-origin (`VITE_API_BASE`
unset → fetch via `/api/*` reverse-proxy).

## Tests

```sh
pnpm test:unit    # vitest unit suite (tests/unit/**) — runs in CI
pnpm test:smoke   # Playwright; expects pnpm dev or Pages preview running
pnpm test         # full Playwright suite (tests/*.test.ts)
```

The vitest suite (`tests/unit/`) covers the markdown-sanitization pipeline
(`$lib/markdown`), the `apiFetch` auth-normalization branches (`$lib/api`),
and the `Candidate` ⇄ OpenAPI contract. Playwright owns the browser smoke
suite; the two never overlap (see `vitest.config.ts` include glob).

## API types — codegen + contract

The backend's DTO shape is the source of truth, exposed by drf-spectacular at
`GET /api/schema/`. To avoid hand-drift in `src/lib/types.ts`:

* `openapi/schema.yaml` is the **committed** OpenAPI schema (the SPA build runs
  on Cloudflare Pages with no backend, so it cannot generate live).
* `pnpm run gen:api-types` regenerates `src/lib/api-types.gen.ts` from that
  committed schema via `openapi-typescript`.
* `tests/unit/contract.test.ts` pins that the hand-written `Candidate`
  interface and the schema's `Candidate` component declare the **identical**
  key set — a backend rename/drop fails the test instead of surfacing as
  `undefined` in the UI.

When the backend contract changes, regenerate the committed schema from the
Django app (it needs the workspace packages on `PYTHONPATH`):

```sh
cd apps/alphalens-django
PYTHONPATH=../alphalens-feedback:../alphalens-pipeline:../alphalens-research \
  DJANGO_SETTINGS_MODULE=config.settings.base \
  python manage.py spectacular --file ../web/openapi/schema.yaml
cd ../web && pnpm run gen:api-types   # refresh the generated DTOs
```

Then update `src/lib/types.ts` + the `TS_CANDIDATE_KEYS` list in the contract
test to match.
