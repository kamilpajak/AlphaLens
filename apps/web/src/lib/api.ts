// Centralised API URL construction.
//
// In production the SPA is served from the same origin as the API, so
// `/api/v1/*` works without any base URL. In development we run against
// either the legacy FastAPI container (port 8081) or the new Django app
// (port 8000); both are reached through Vite's dev-server proxy which
// rewrites `/api/*` to the upstream root.
//
// `VITE_API_BASE` overrides this for cross-origin deployments (e.g. when
// the frontend is hosted on Cloudflare Pages but the API runs elsewhere).
// Leave it unset and you get same-origin behaviour, which is what every
// existing deploy expects.

const RAW_BASE = (import.meta.env.VITE_API_BASE ?? '').trim();

// Strip a trailing slash so callers can use `api('/v1/days')` without
// producing `//v1/days` for VITE_API_BASE='https://api.example.com/'.
const API_BASE = RAW_BASE.replace(/\/+$/, '');

/**
 * Build a fully-qualified API URL.
 *
 * Same-origin (no VITE_API_BASE): `api('/v1/days')` → `/api/v1/days`.
 * Cross-origin: `api('/v1/days')` → `https://api.example.com/v1/days`.
 *
 * The `/api` prefix is dropped when VITE_API_BASE is set because that
 * prefix is a same-origin proxy artifact, not part of the API contract.
 */
export function api(path: string): string {
	const normalized = path.startsWith('/') ? path : `/${path}`;
	if (API_BASE) return `${API_BASE}${normalized}`;
	return `/api${normalized}`;
}

/** True when running against an external API (Cloudflare Access, etc). */
export function isCrossOrigin(): boolean {
	return API_BASE !== '';
}

/**
 * Fetch wrapper that auto-includes credentials for cross-origin calls.
 *
 * Cross-origin fetch defaults to `credentials: 'same-origin'`, so the
 * browser would NOT attach the CF_Authorization cookie when the SPA
 * (app.<domain>) hits the API (api.<domain>). Without the cookie CF
 * Access returns its HTML login page instead of proxying through to
 * Django, and the SPA's `await res.json()` throws SyntaxError on the
 * `<!doctype html>`, surfacing as a 500 on every API-backed route.
 *
 * Use this for every API call so the cookie flows on both same-origin
 * (local Docker stack) and cross-origin (Pages + Tunnel) deploys —
 * `credentials: 'include'` is a no-op for same-origin requests.
 *
 * Accepts the same second-arg shape as `fetch` plus a `fetcher` slot for
 * the SvelteKit page-load `fetch` (preserves SSR cookie hand-off if we
 * ever bring SSR back).
 */
export async function apiFetch(
	path: string,
	init: RequestInit = {},
	fetcher: typeof fetch = fetch
): Promise<Response> {
	const res = await fetcher(api(path), { credentials: 'include', ...init });
	// CF Access session expiry: when the CF_Authorization cookie is invalid
	// or expired, CF Access transparently serves its login HTML with status
	// 200, so `res.ok` would be true and downstream `.json()` would crash on
	// `<!doctype`. Detect by content-type (the API always returns JSON; any
	// HTML body here is the login page, never legitimate API output) and
	// surface a synthetic 401 so callsites' `if (!res.ok)` branches catch it.
	const contentType = res.headers.get('content-type') ?? '';
	if (contentType.includes('text/html')) {
		return new Response(null, { status: 401, statusText: 'Unauthorized' });
	}
	return res;
}
