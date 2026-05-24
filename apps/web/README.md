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

#### Auth model (decision needed before Phase 3)

The Django API is gated by Cloudflare Access (`auth_cf` middleware).
Cross-origin from a browser SPA leaves two auth paths; one MUST be
chosen and applied in the CF Zero Trust dashboard + Django settings
before Phase 3 (VPS pull-only). Picking neither = SPA gets 403.

| Path | How it works | DNS requirement | Trade-off |
|------|--------------|-----------------|-----------|
| **A — Same-domain cookies** | SPA + API share an eTLD+1 (e.g. `app.example.com` + `api.example.com`); browser sends the `CF_Authorization` session cookie cross-origin; Django reads it via existing `auth_cf` middleware. `CORS_ALLOW_CREDENTIALS=True` required. | Custom domain on CF Pages, same parent as the API | No code change; user logs in via CF Access once per session. **Cannot** use Service Tokens (the secret would live in browser JS). |
| **B — Edge proxy via Pages Function** | CF Pages Function at `/api/*` holds a Service Token as a secret env var; injects `CF-Access-Client-Id` + `CF-Access-Client-Secret` headers; forwards to the tunnel. SPA stays same-origin (`VITE_API_BASE` unset). | None — any `pages.dev` URL works | Adds one edge function (~30 LOC) but eliminates CORS + cross-domain cookie issues. Latency: ~10-30 ms per request. |

CF Access **OPTIONS preflight** must be bypassed regardless of path
(CF Zero Trust → Access app for the API origin → enable "Bypass Access
for HTTP OPTIONS"). Without this, browsers' CORS preflights get 302/403
before reaching Django.

For preview branches (`*.pages.dev`), use Django's
`CORS_ALLOWED_ORIGIN_REGEXES` instead of (or alongside) literal
`CORS_ALLOWED_ORIGINS` — e.g.
`r"^https://[\w-]+\.alphalens\.pages\.dev$"`.

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
