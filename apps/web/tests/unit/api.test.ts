import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { apiFetch, stripTrailingSlashes } from '../../src/lib/api';
import { clearSessionExpired, markSessionExpired, sessionExpired } from '../../src/lib/session.svelte';

// `apiFetch` normalizes the failure modes of a Cloudflare-Access-gated,
// cross-origin SPA → API call into synthetic status codes the callsites'
// `if (!res.ok)` branches understand. These branches are security-relevant
// (they decide "session expired → re-auth" vs "transient outage → don't loop")
// and were only covered indirectly by Playwright. Each test mocks one fetch
// outcome and asserts the normalized response.

function htmlResponse(status: number): Response {
	return new Response('<!doctype html><html>login</html>', {
		status,
		headers: { 'content-type': 'text/html; charset=utf-8' }
	});
}

function jsonResponse(status: number, body: unknown = {}): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

beforeEach(() => {
	// Isolate the global session-expiry flag between cases — apiFetch mutates
	// it as a side effect on the two auth-expiry paths.
	clearSessionExpired();
});

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('stripTrailingSlashes', () => {
	it('removes a single trailing slash', () => {
		expect(stripTrailingSlashes('https://api.example.com/')).toBe('https://api.example.com');
	});

	it('removes multiple trailing slashes', () => {
		expect(stripTrailingSlashes('https://api.example.com///')).toBe('https://api.example.com');
	});

	it('leaves a slash-free string untouched', () => {
		expect(stripTrailingSlashes('https://api.example.com')).toBe('https://api.example.com');
	});

	it('returns empty string for empty input', () => {
		expect(stripTrailingSlashes('')).toBe('');
	});

	it('returns empty string for an all-slashes input', () => {
		expect(stripTrailingSlashes('////')).toBe('');
	});

	it('preserves internal slashes', () => {
		expect(stripTrailingSlashes('https://api.example.com/v1/')).toBe('https://api.example.com/v1');
	});
});

describe('apiFetch normalization branches', () => {
	it('returns 503 when fetch throws and the client is offline', async () => {
		vi.stubGlobal('navigator', { onLine: false });
		const fetcher = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));

		const res = await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(res.status).toBe(503);
	});

	it('returns synthetic 401 when fetch throws but the client is online', async () => {
		// Online client: a thrown TypeError is the CF Access cross-origin
		// redirect-refusal, normalized to an auth failure (not a 503).
		vi.stubGlobal('navigator', { onLine: true });
		const fetcher = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));

		const res = await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(res.status).toBe(401);
	});

	it('returns synthetic 401 when CF Access serves its login page as 200 + HTML', async () => {
		const fetcher = vi.fn().mockResolvedValue(htmlResponse(200));

		const res = await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(res.status).toBe(401);
	});

	it('passes through a non-2xx HTML body (real upstream error, not auth)', async () => {
		// A 502 + HTML is nginx/cloudflared, not an expired session — masking it
		// as 401 would bounce the user into an infinite SSO loop. Let it through.
		const upstream = htmlResponse(502);
		const fetcher = vi.fn().mockResolvedValue(upstream);

		const res = await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(res.status).toBe(502);
		expect(res).toBe(upstream);
	});

	it('passes through a successful JSON response untouched', async () => {
		const upstream = jsonResponse(200, { data: [] });
		const fetcher = vi.fn().mockResolvedValue(upstream);

		const res = await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(res.status).toBe(200);
		expect(res).toBe(upstream);
	});

	it('attaches credentials: include so the CF_Authorization cookie flows', async () => {
		const fetcher = vi.fn().mockResolvedValue(jsonResponse(200));

		await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(fetcher).toHaveBeenCalledTimes(1);
		const init = fetcher.mock.calls[0][1] as RequestInit;
		expect(init.credentials).toBe('include');
	});

	it('preserves caller init while forcing credentials include', async () => {
		const fetcher = vi.fn().mockResolvedValue(jsonResponse(200));

		await apiFetch(
			'/v1/days',
			{ method: 'POST', body: '{"x":1}' },
			fetcher as unknown as typeof fetch
		);

		const init = fetcher.mock.calls[0][1] as RequestInit;
		expect(init.method).toBe('POST');
		expect(init.body).toBe('{"x":1}');
		expect(init.credentials).toBe('include');
	});
});

describe('apiFetch session-expiry side effect', () => {
	it('marks the session expired when fetch throws and the client is online', async () => {
		// Cross-origin redirect-refusal with a live network = expired CF Access
		// session → fire the global overlay.
		vi.stubGlobal('navigator', { onLine: true });
		const fetcher = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));

		const res = await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(res.status).toBe(401);
		expect(sessionExpired()).toBe(true);
	});

	it('marks the session expired when CF Access serves login HTML as 200', async () => {
		const fetcher = vi.fn().mockResolvedValue(htmlResponse(200));

		const res = await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(res.status).toBe(401);
		expect(sessionExpired()).toBe(true);
	});

	it('does NOT mark the session on a successful JSON response', async () => {
		const fetcher = vi.fn().mockResolvedValue(jsonResponse(200, { data: [] }));

		await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(sessionExpired()).toBe(false);
	});

	it('does NOT mark the session on the offline (503) path', async () => {
		// A genuinely offline client is a transient connectivity failure, not an
		// auth expiry — the overlay must stay closed so we don't push the user
		// into an SSO flow they can't complete.
		vi.stubGlobal('navigator', { onLine: false });
		const fetcher = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));

		const res = await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(res.status).toBe(503);
		expect(sessionExpired()).toBe(false);
	});

	it('does NOT mark the session on a non-2xx HTML body (real upstream error)', async () => {
		const fetcher = vi.fn().mockResolvedValue(htmlResponse(502));

		const res = await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(res.status).toBe(502);
		expect(sessionExpired()).toBe(false);
	});
});

describe('apiFetch auto-clears the overlay on proof-of-life', () => {
	// A single transient failure (network-wake race, tunnel restart, CORS blip)
	// latches the overlay because nothing resets the flag. Any 2xx response
	// proves CF Access let the request through to Django → the session is
	// alive → clear the flag so a stale overlay self-heals on the next success.
	it('clears a previously-set flag on a successful JSON response', async () => {
		markSessionExpired();
		const fetcher = vi.fn().mockResolvedValue(jsonResponse(200, { data: [] }));

		await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(sessionExpired()).toBe(false);
	});

	it('does NOT clear the flag on a non-2xx response (ambiguous — could be cloudflared down)', async () => {
		// A 500/502 does not prove the CF Access session is alive (the error may
		// originate at the tunnel before Django). Stay conservative: only a
		// confirmed 2xx clears the overlay.
		markSessionExpired();
		const fetcher = vi.fn().mockResolvedValue(jsonResponse(500, { detail: 'boom' }));

		await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(sessionExpired()).toBe(true);
	});

	it('does NOT clear the flag on the offline (503) path', async () => {
		markSessionExpired();
		vi.stubGlobal('navigator', { onLine: false });
		const fetcher = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));

		await apiFetch('/v1/days', {}, fetcher as unknown as typeof fetch);

		expect(sessionExpired()).toBe(true);
	});
});

describe('apiFetch silentAuth — background polls do not raise the overlay', () => {
	// The /v1/market/status poll runs every 60s and on every tab focus; it is
	// explicitly "fail silent" noise. With silentAuth it still returns the
	// synthetic 401 (so the callsite's `!res.ok` branch behaves), but it must
	// NOT raise the global re-auth overlay — only fetches the user cares about
	// (briefs, edge) do. Kills the dominant false-positive (tab-wake races).
	it('returns synthetic 401 but does NOT mark the session when fetch throws online', async () => {
		vi.stubGlobal('navigator', { onLine: true });
		const fetcher = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));

		const res = await apiFetch(
			'/v1/market/status',
			{},
			fetcher as unknown as typeof fetch,
			{ silentAuth: true }
		);

		expect(res.status).toBe(401);
		expect(sessionExpired()).toBe(false);
	});

	it('returns synthetic 401 but does NOT mark the session on CF login HTML 200', async () => {
		const fetcher = vi.fn().mockResolvedValue(htmlResponse(200));

		const res = await apiFetch(
			'/v1/market/status',
			{},
			fetcher as unknown as typeof fetch,
			{ silentAuth: true }
		);

		expect(res.status).toBe(401);
		expect(sessionExpired()).toBe(false);
	});

	it('still auto-clears the flag on a successful response even when silent', async () => {
		// silentAuth suppresses RAISING the overlay, not the proof-of-life clear:
		// a successful poll should self-heal a stale overlay regardless.
		markSessionExpired();
		const fetcher = vi.fn().mockResolvedValue(jsonResponse(200, {}));

		await apiFetch(
			'/v1/market/status',
			{},
			fetcher as unknown as typeof fetch,
			{ silentAuth: true }
		);

		expect(sessionExpired()).toBe(false);
	});
});
