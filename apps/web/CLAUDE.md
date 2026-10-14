# `apps/web` — SvelteKit SPA

Working brief for editing the dashboard. It loads only once Claude reads, writes or edits a file
under `apps/web/`, and it is NOT re-injected after `/compact`. Run `pnpm` commands from
`apps/web`, not the workspace root.

Deploy and auth topology (Cloudflare Pages settings, the same-domain-cookie path A, CORS and
Access requirements), local dev, `dev:vps`, the test split and the OpenAPI codegen flow live in
[`README.md`](README.md). Read them there; never restate them here.

## Atomic tokens never wrap mid-string

- Any token that reads wrong when split across two lines must carry Tailwind `whitespace-nowrap`:
  dates (`YYYY-MM-DD`), math and formulas (`α=-2.01`, `L = λW`), numeric ranges (`20-40 bps`),
  version strings, tickers.
- Why: CSS treats `-` (hyphen-minus) and other separators as valid line-break opportunities, so
  `2026-05-25` can break to `2026-` / `05-25`.
- Token inline with wrapping prose → wrap ONLY the token in its own
  `<span class="whitespace-nowrap">`.
- Token sharing a flex row with an icon (for example a chevron nav button) → put
  `whitespace-nowrap` on the flex anchor itself.
- Nothing enforces this; it is a review-time rule. Verified 2026-10-14: the only test naming the
  class, `tests/unit/statusPill.test.ts`, pins `StatusPill`'s class builder alone. Established
  PR #261.

## Storybook is the component catalog and the spec

Storybook 10 (`storybook` and `@storybook/sveltekit` 10.6.0, `@storybook/addon-svelte-csf` 5.1.3)
is a devDependency. The 32 `.stories.svelte` files under `src/lib/` DO import it
(`defineMeta` from `@storybook/addon-svelte-csf`), and nothing in `svelte.config.js` or
`vite.config.ts` excludes them — they stay out of the production Cloudflare Pages bundle
because no route reaches them, i.e. they are tree-shaken, not configured out. Import
Storybook from a route-reachable module and it ships.

- Every new or changed component ships a `.stories.svelte` beside it, one `<Story>` per meaningful
  state (default / empty / loading / error / edge). Never one kitchen-sink story.
- Check the catalog before building new UI. Reuse the primitives in `src/lib/components/`:
  `StatusPill`, `LedgerRow`, `Disclosure`, `SectionPanel`, `DetailField`, `EvidenceLink`,
  `LedgerFilterBar`, the tooltip family. Read the component source for its real props — never
  invent props.
- Bind REAL fixtures, never invented numbers. Data-bearing stories use `tests/fixtures/api-mock/*`
  (`days/2026-05-18.json`, `edge-summary.json`, `edge-outcomes.json`) or the component's own
  exported enums. Fake-but-plausible data is a defect.
- `play` is a PROP, not a snippet: `play={async ({ canvasElement }) => …}`. The `{#snippet play}`
  form silently never runs as the play function.
- Use the `canvasElement` play-arg, not `canvas`, for raw-DOM queries (for example
  `.querySelector('math')`).
- For a `bind:this` ref, alias a duplicate value+type default import:
  `import type FooInstance from './Foo.svelte'`, then `$state<FooInstance | null>(null)`.

### Gate — run both from `apps/web`

```
pnpm run check            # svelte-kit sync + svelte-check
pnpm run build-storybook
```

- Both must pass. There is no Storybook test-runner in CI, so `play()` assertions are NON-gating:
  they run only when a story is opened in `pnpm run storybook`. Verify there when an interaction
  matters.
- `pnpm run check` exits 0 WITH warnings. Five tooltip triggers each emit one
  `a11y_no_noninteractive_tabindex` — a focusable `span` is what makes the tooltip reachable by
  touch and keyboard — and none carries a `svelte-ignore`; only `TooltipBubble.stories.svelte`
  suppresses it. A 6th warning means a new trigger.
- The warning COUNT was last measured by a full `pnpm run check` on **2026-09-15** and has not been
  re-run since. The five trigger components were re-verified statically on 2026-10-14: `ChipTip`,
  `ExpertPillar`, `GatePill`, `JargonTip` and `SignalBar` are the only sources under
  `src/lib/components/` carrying a real `tabindex` attribute with no `svelte-ignore`.

## Build before `docker compose up` (local Docker stack only)

- Build first: `pnpm --filter web build`, then `just up`.
- nginx and the `apps/web/build/` bind-mount live in
  `deploy/docker/django-prod/docker-compose.override.yaml` (lines 38-51), which Compose auto-loads
  only when no `-f` is passed. Bring the stack up with a bare `docker compose up -d` from that
  directory, or `just up`. Passing `-f docker-compose.yaml` selects the VPS-shaped stack, which
  has no nginx service at all (verified 2026-10-14).
- On macOS Docker Desktop a missing source path at container start makes Docker create an empty
  directory, and the mount stays empty even after the build appears on the host — there is no live
  re-bind.
- Symptom: nginx serves `403 Forbidden` on `/` plus `rewrite or internal redirection cycle while
  internally redirecting to "/index.html"` on every SPA route, while `/api/*` still proxies fine.
  The SPA is on `http://localhost:8080` (`NGINX_HTTP_PORT`).
- Already up with an empty mount: build, then `docker compose restart nginx` from
  `deploy/docker/django-prod`.
- Does NOT apply to production — Cloudflare Pages builds and serves the SPA; the VPS serves no SPA.
