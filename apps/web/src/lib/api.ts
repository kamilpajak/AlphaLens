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
