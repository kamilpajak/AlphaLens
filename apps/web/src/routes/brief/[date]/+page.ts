import { error } from '@sveltejs/kit';
import type { PageLoad } from './$types';
import { apiFetch } from '$lib/api';
import type { DayBrief, DayIndexEntry, Paginated } from '$lib/types';

/**
 * A renderable empty brief for the requested date. Used on a 401 (expired CF
 * Access session) so the page renders behind the global re-auth overlay
 * instead of escalating to the full-page error boundary — the overlay (fired
 * by apiFetch via the session store) is now the single re-auth surface.
 */
function emptyBrief(date: string): DayBrief {
	return { date, n_candidates: 0, n_themes: 0, top_theme: null, theme_counts: {}, candidates: [] };
}

export const load: PageLoad = async ({ fetch, params }) => {
	const [indexRes, briefRes] = await Promise.all([
		apiFetch('/v1/days?limit=200', {}, fetch),
		apiFetch(`/v1/days/${params.date}`, {}, fetch)
	]);

	// An index that fails only because the session expired (401) shouldn't
	// surface its own error — fall back to an empty day list so the page still
	// renders behind the overlay. A non-401 index failure with a usable brief
	// still degrades to empty days rather than blocking the brief render.
	const days: DayIndexEntry[] = indexRes.ok
		? (await (indexRes.json() as Promise<Paginated<DayIndexEntry>>)).data
		: [];

	if (briefRes.ok) {
		const brief: DayBrief = await briefRes.json();
		return { days, brief };
	}

	// 404 = missing brief (expected for dates without a daily run) → keep the
	// dedicated error page. The message stays free of the numeric status so the
	// error page shows the code in exactly one place (the heading).
	if (briefRes.status === 404) {
		error(404, 'No brief for this date.');
	}

	// 401 = expired CF Access session. Do NOT throw — apiFetch already flipped
	// the global session-expiry store, so render an empty brief behind the
	// overlay (the single re-auth mechanism), consistent with /edge and
	// /briefs which also degrade rather than error.
	if (briefRes.status === 401) {
		return { days, brief: emptyBrief(params.date) };
	}

	// Any other failure (5xx, etc.) is a genuine server error → surface it.
	error(briefRes.status, 'Could not load this brief.');
};
