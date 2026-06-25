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

import { markSessionExpired } from './session.svelte';

const RAW_BASE = (import.meta.env.VITE_API_BASE ?? '').trim();

/**
 * Strip trailing slashes so callers can use `api('/v1/days')` without
 * producing `//v1/days` for VITE_API_BASE='https://api.example.com/'.
 *
 * A linear character scan rather than a `/\/+$/` regex: the anchored greedy
 * quantifier triggers Sonar's super-linear-backtracking warning (S8786), and
 * this is unambiguously O(n).
 */
export function stripTrailingSlashes(value: string): string {
	let end = value.length;
	while (end > 0 && value[end - 1] === '/') end--;
	return value.slice(0, end);
}

const API_BASE = stripTrailingSlashes(RAW_BASE);

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
	let res: Response;
	try {
		res = await fetcher(api(path), { credentials: 'include', ...init });
	} catch {
		// A genuinely offline client (no network interface) shouldn't be told
		// "session expired" — surface 503 so the error page reads as a
		// transient connectivity failure. navigator.onLine only rules out the
		// fully-offline case; a reachable-network-but-302 (the dominant cause
		// for this Access-gated SPA) still falls through to 401 below.
		if (typeof navigator !== 'undefined' && !navigator.onLine) {
			return new Response(null, { status: 503, statusText: 'Service Unavailable' });
		}
		// Cross-origin fetch throws a TypeError when CF Access answers an
		// unauthenticated XHR with a 302 to its login origin: the browser
		// refuses to follow a cross-origin redirect, so the promise rejects.
		// Normalise to a synthetic 401 so callers' `if (!res.ok)` branch
		// handles it as an auth failure instead of the raw throw bubbling
		// into SvelteKit's generic "500 Internal Error" page.
		//
		// Flip the global session-expiry flag so the layout-level re-auth
		// overlay fires on EVERY route — not just the loaders that escalate
		// 401 to the full-page error boundary. Marked only here (and on the
		// login-HTML path below), NOT on the 503 offline return above.
		//
		// Known limitation (LOCAL DEV ONLY): a same-origin online fetch that
		// throws is a genuine connectivity failure (no CF Access in the loop),
		// yet it is also normalised to a session-expiry here. In production the
		// SPA is always cross-origin (CF Pages + Tunnel), where an online throw
		// IS the Access redirect-refusal — and gating on isCrossOrigin() would
		// still not separate that from a Tunnel-down (both throw cross-origin),
		// so the dev-only false positive is left as-is.
		markSessionExpired();
		return new Response(null, { status: 401, statusText: 'Unauthorized' });
	}
	// CF Access session expiry: when the CF_Authorization cookie is invalid
	// or expired, CF Access transparently serves its login HTML with status
	// 200, so `res.ok` would be true and downstream `.json()` would crash on
	// `<!doctype`. Detect by content-type (the API always returns JSON; any
	// HTML body here is the login page, never legitimate API output) and
	// surface a synthetic 401 so callsites' `if (!res.ok)` branches catch it.
	const contentType = res.headers.get('content-type') ?? '';
	// Gate on res.ok: CF Access serves its login page as 200 + text/html, so a
	// SUCCESSFUL HTML body means an expired session → synthetic 401. A non-2xx
	// HTML body is a genuine upstream error page (nginx/cloudflared 502/503);
	// masking that as 401 would bounce the user into an infinite SSO loop
	// during a transient outage, so let it through as the real error.
	if (res.ok && contentType.includes('text/html')) {
		// Same auth-expiry signal as the catch branch — fire the global overlay.
		markSessionExpired();
		return new Response(null, { status: 401, statusText: 'Unauthorized' });
	}
	return res;
}

/**
 * Fetch the ladder-replay chart payload for one recommendation.
 *
 * Lazy-called from the /edge inline accordion (and later the brief toggle) on
 * first expand — never in the page loader. Returns the parsed `ChartPayload`
 * on success or `null` on any failure (offline, 401, 5xx, malformed body), so
 * the caller can render the same dotted-border empty box it uses for the
 * NO_DATA / NO_STRUCTURE states rather than throwing.
 *
 * `briefDate` and `ticker` are path segments; they are URL-encoded defensively
 * even though both come from controlled ledger data.
 */
export async function getEdgeChart(
	briefDate: string,
	ticker: string,
	fetcher: typeof fetch = fetch
): Promise<import('./types').ChartPayload | null> {
	const path = `/v1/edge/chart/${encodeURIComponent(briefDate)}/${encodeURIComponent(ticker)}`;
	try {
		const res = await apiFetch(path, {}, fetcher);
		if (!res.ok) return null;
		return (await res.json()) as import('./types').ChartPayload;
	} catch {
		return null;
	}
}
