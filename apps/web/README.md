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

## Smoke tests

```sh
pnpm test:smoke   # Playwright; expects pnpm dev or Pages preview running
```
