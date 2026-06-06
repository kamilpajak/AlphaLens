import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiFetch } from '../../src/lib/api';

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

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
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
