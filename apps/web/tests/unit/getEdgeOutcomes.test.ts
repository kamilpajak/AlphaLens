// apps/web/tests/unit/getEdgeOutcomes.test.ts
import { describe, it, expect, vi } from 'vitest';
import { getEdgeOutcomes, EDGE_WINDOW_DAYS } from '$lib/api';

function jsonResponse(body: unknown): Response {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

const FACETS = {
	status: { terminal: 2, ongoing: 1 },
	classification: { TP_FULL: 1, SL_HIT: 1, OPEN: 1 }
};

const BODY = {
	data: [{ ticker: 'AMPL' }, { ticker: 'RGTI' }],
	total: 2,
	returned: 2,
	truncated: false,
	facets: FACETS
};

describe('getEdgeOutcomes', () => {
	it('requests the windowed endpoint WITHOUT a classification param for an empty selection', async () => {
		const fetcher = vi.fn().mockResolvedValue(jsonResponse(BODY));
		const out = await getEdgeOutcomes(new Set(), fetcher as unknown as typeof fetch);
		expect(fetcher).toHaveBeenCalledOnce();
		const url = (fetcher.mock.calls[0][0] as string) ?? '';
		expect(url).toContain('/v1/edge/outcomes');
		expect(url).toContain(`window=${EDGE_WINDOW_DAYS}`);
		expect(url).not.toContain('classification');
		expect(out.rows).toHaveLength(2);
		expect(out.total).toBe(2);
		expect(out.truncated).toBe(false);
		expect(out.facets).toEqual(FACETS);
	});

	it('serializes the selection sorted into one comma-joined classification param', async () => {
		const fetcher = vi.fn().mockResolvedValue(jsonResponse(BODY));
		await getEdgeOutcomes(new Set(['TIME_STOP', 'SL_HIT']), fetcher as unknown as typeof fetch);
		const url = (fetcher.mock.calls[0][0] as string) ?? '';
		// Sorted regardless of insertion order, comma URL-encoded as %2C.
		expect(url).toContain('classification=SL_HIT%2CTIME_STOP');
	});

	it('degrades to an empty result on a non-ok response', async () => {
		const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 500 }));
		const out = await getEdgeOutcomes(new Set(), fetcher as unknown as typeof fetch);
		expect(out).toEqual({ rows: [], total: 0, truncated: false, facets: null });
	});

	it('falls back to facets: null when an older API build omits the block', async () => {
		const fetcher = vi
			.fn()
			.mockResolvedValue(jsonResponse({ data: [{ ticker: 'AMPL' }], total: 1, truncated: false }));
		const out = await getEdgeOutcomes(new Set(), fetcher as unknown as typeof fetch);
		expect(out.rows).toHaveLength(1);
		expect(out.facets).toBeNull();
	});
});
