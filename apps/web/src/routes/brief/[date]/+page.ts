import { error } from '@sveltejs/kit';
import type { PageLoad } from './$types';
import { apiFetch } from '$lib/api';
import { getTaxonomy, listDecisions } from '$lib/feedback';
import type { DayBrief, Decision, DayIndexEntry, FeedbackTaxonomy, Paginated } from '$lib/types';

export const load: PageLoad = async ({ fetch, params }) => {
	const [indexRes, briefRes, taxonomy, decisions] = await Promise.all([
		apiFetch('/v1/days?limit=200', {}, fetch),
		apiFetch(`/v1/days/${params.date}`, {}, fetch),
		// Best-effort: a stale frontend pointing at a backend without the
		// feedback endpoints (e.g. mid-rollout, dev VM without ALPHALENS_
		// FEEDBACK_DB) should still render the brief. Both branches return
		// null on failure so CandidateCard hides the FeedbackControls row.
		getTaxonomy(fetch).catch((): FeedbackTaxonomy | null => null),
		listDecisions(params.date, fetch).catch((): Decision[] => [])
	]);
	if (!briefRes.ok) {
		// Propagate the real status — 404 = missing brief (expected for dates
		// without a daily run); 401 = expired Cloudflare Access session (the
		// +error.svelte page renders it as "session expired" with a re-auth
		// prompt); other codes surface as a generic server error. The message
		// stays free of the numeric status so the error page shows the code
		// in exactly one place (the heading), not duplicated in the body.
		error(
			briefRes.status === 404 ? 404 : briefRes.status,
			briefRes.status === 404 ? 'No brief for this date.' : 'Could not load this brief.'
		);
	}
	if (!indexRes.ok) {
		error(indexRes.status, 'Could not load the brief index.');
	}
	const indexBody: Paginated<DayIndexEntry> = await indexRes.json();
	const brief: DayBrief = await briefRes.json();
	// Index decisions by (ticker, theme) for O(1) lookup in the brief page.
	// Brief_date is implicit (matches the route param).
	const decisionsByKey: Record<string, Decision> = {};
	for (const d of decisions) {
		decisionsByKey[`${d.ticker}::${d.theme}`] = d;
	}
	return { days: indexBody.data, brief, taxonomy, decisionsByKey };
};
