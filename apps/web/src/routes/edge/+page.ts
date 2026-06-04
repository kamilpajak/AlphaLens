import type { PageLoad } from './$types';
import { apiFetch } from '$lib/api';
import type { EdgeSummary, EdgeOutcome } from '$lib/types';

// The /edge dashboard reads two independent endpoints:
//   GET /v1/edge/summary   — the N-gated benchmark-excess aggregate + the
//                            always-on deployment/open-positions blocks
//   GET /v1/edge/outcomes  — the per-candidate rows for the table
// Both degrade to null/[] on any failure (offline, 401, 5xx) so the page
// renders a clean "no data" state rather than crashing — same pattern as
// the briefs loader.
//
// `window` is calendar days back from the LATEST brief_date in the cache
// (backend semantics), so the window is stable regardless of when the API
// is hit vs the nightly rebuild. Default 90d gives a broad telemetry view.
const WINDOW_DAYS = 90;

export const load: PageLoad = async ({ fetch }) => {
	const summary = await loadSummary(fetch);
	const outcomes = await loadOutcomes(fetch);
	return { summary, outcomes };
};

async function loadSummary(fetch: typeof globalThis.fetch): Promise<EdgeSummary | null> {
	try {
		const res = await apiFetch(`/v1/edge/summary?window=${WINDOW_DAYS}`, {}, fetch);
		if (!res.ok) return null;
		return (await res.json()) as EdgeSummary;
	} catch {
		return null;
	}
}

async function loadOutcomes(fetch: typeof globalThis.fetch): Promise<EdgeOutcome[]> {
	try {
		const res = await apiFetch(`/v1/edge/outcomes?window=${WINDOW_DAYS}`, {}, fetch);
		if (!res.ok) return [];
		const body: { data: EdgeOutcome[] } = await res.json();
		return body.data ?? [];
	} catch {
		return [];
	}
}
