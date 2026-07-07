// apps/web/tests/unit/getEdgeExcessTelemetry.test.ts
import { describe, it, expect, vi } from 'vitest';
import { getEdgeExcessTelemetry } from '$lib/api';

function jsonResponse(body: unknown): Response {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

describe('getEdgeExcessTelemetry', () => {
	it('requests the windowed endpoint and returns the parsed body', async () => {
		const payload = { benchmark: 'SPY', status: 'ok', points: [], trend: [] };
		const fetcher = vi.fn().mockResolvedValue(jsonResponse(payload));
		const out = await getEdgeExcessTelemetry(90, fetcher as unknown as typeof fetch);
		expect(fetcher).toHaveBeenCalledOnce();
		const url = (fetcher.mock.calls[0][0] as string) ?? '';
		expect(url).toContain('/v1/edge/excess-telemetry');
		expect(url).toContain('window=90');
		expect(out?.benchmark).toBe('SPY');
	});

	it('returns null on a non-ok response', async () => {
		const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 500 }));
		const out = await getEdgeExcessTelemetry(90, fetcher as unknown as typeof fetch);
		expect(out).toBeNull();
	});
});
