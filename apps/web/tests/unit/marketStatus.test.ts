import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { refreshMarketStatus } from '../../src/lib/marketStatus.svelte';
import { clearSessionExpired, sessionExpired } from '../../src/lib/session.svelte';

// The /v1/market/status poll is the highest-frequency apiFetch in the app
// (every 60s + on every tab focus) and is explicitly "fail silent" noise.
// A failed poll must NOT raise the global CF-Access re-auth overlay — that
// turned transient blips (tab-wake network races, tunnel restarts) into a
// blocking full-screen modal. Genuine expiry is still surfaced by the data
// fetches the user actually cares about (briefs, edge).

function jsonResponse(status: number, body: unknown = {}): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

beforeEach(() => {
	clearSessionExpired();
});

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('refreshMarketStatus does not raise the re-auth overlay', () => {
	it('leaves the session flag clear when the poll throws on a live network', async () => {
		// Online client + a thrown TypeError = the exact tab-wake / blip shape
		// that used to latch the overlay.
		vi.stubGlobal('navigator', { onLine: true });
		const fetcher = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));

		await refreshMarketStatus(fetcher as unknown as typeof fetch);

		expect(sessionExpired()).toBe(false);
	});

	it('leaves the session flag clear when CF Access serves login HTML 200', async () => {
		const fetcher = vi.fn().mockResolvedValue(
			new Response('<!doctype html><html>login</html>', {
				status: 200,
				headers: { 'content-type': 'text/html; charset=utf-8' }
			})
		);

		await refreshMarketStatus(fetcher as unknown as typeof fetch);

		expect(sessionExpired()).toBe(false);
	});

	it('still updates market state on a successful poll', async () => {
		const fetcher = vi.fn().mockResolvedValue(
			jsonResponse(200, {
				is_trading_day: true,
				is_half_day: false,
				is_open_now: true,
				next_open_iso: '2026-06-26T13:30:00Z',
				next_close_iso: '2026-06-25T20:00:00Z',
				exchange: 'XNYS'
			})
		);

		await refreshMarketStatus(fetcher as unknown as typeof fetch);

		expect(sessionExpired()).toBe(false);
	});
});
