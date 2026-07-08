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
	const { rows, total, truncated } = await loadOutcomes(fetch);
	// `outcomesTotal` is the TRUE match count in the window (may exceed the
	// returned rows when the server caps the listing); `outcomesTruncated` flags
	// that older rows were dropped, so the table can say "showing N of M" rather
	// than silently under-listing (and under-counting the chip tallies).
	return { summary, outcomes: rows, outcomesTotal: total, outcomesTruncated: truncated };
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

async function loadOutcomes(
	fetch: typeof globalThis.fetch
): Promise<{ rows: EdgeOutcome[]; total: number; truncated: boolean }> {
	try {
		const res = await apiFetch(`/v1/edge/outcomes?window=${WINDOW_DAYS}`, {}, fetch);
		if (!res.ok) return { rows: [], total: 0, truncated: false };
		const body: { data: EdgeOutcome[]; total?: number; truncated?: boolean } = await res.json();
		const rows = body.data ?? [];
		// `total`/`truncated` fall back gracefully if an older API build omits them.
		return { rows, total: body.total ?? rows.length, truncated: body.truncated ?? false };
	} catch {
		return { rows: [], total: 0, truncated: false };
	}
}
