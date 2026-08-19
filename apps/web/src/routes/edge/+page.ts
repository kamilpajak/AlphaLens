import type { PageLoad } from './$types';
import { apiFetch, getEdgeOutcomes, EDGE_WINDOW_DAYS } from '$lib/api';
import type { EdgeSummary } from '$lib/types';

// The /edge dashboard reads two independent endpoints:
//   GET /v1/edge/summary   — the N-gated benchmark-excess aggregate + the
//                            always-on deployment/open-positions blocks
//   GET /v1/edge/outcomes  — the per-candidate rows for the table
// Both degrade to null/[] on any failure (offline, 401, 5xx) so the page
// renders a clean "no data" state rather than crashing — same pattern as
// the briefs loader.
//
// The initial outcomes fetch is WINDOW-ONLY — no status, no classification —
// even when the URL carries a `?class=` deep link. The page's mount-time
// refetch effect applies the classification server-side after hydration;
// seeding it here would ship a wrong-default first paint for the plain route.
export const load: PageLoad = async ({ fetch }) => {
	const summary = await loadSummary(fetch);
	const { rows, total, truncated, facets } = await getEdgeOutcomes(new Set(), fetch);
	// `outcomesTotal` is the TRUE match count of the current listing (may exceed
	// the returned rows when the server caps it); `outcomesTruncated` flags that
	// older rows were dropped; `outcomesFacets` carry the pre-filter window
	// population for the server-truth chip counts.
	return {
		summary,
		outcomes: rows,
		outcomesTotal: total,
		outcomesTruncated: truncated,
		outcomesFacets: facets
	};
};

async function loadSummary(fetch: typeof globalThis.fetch): Promise<EdgeSummary | null> {
	try {
		const res = await apiFetch(`/v1/edge/summary?window=${EDGE_WINDOW_DAYS}`, {}, fetch);
		if (!res.ok) return null;
		return (await res.json()) as EdgeSummary;
	} catch {
		return null;
	}
}
