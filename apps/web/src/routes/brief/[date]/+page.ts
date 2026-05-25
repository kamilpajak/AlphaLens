import { error } from '@sveltejs/kit';
import type { PageLoad } from './$types';
import { apiFetch } from '$lib/api';
import type { DayBrief, DayIndexEntry, Paginated } from '$lib/types';

export const load: PageLoad = async ({ fetch, params }) => {
	const [indexRes, briefRes] = await Promise.all([
		apiFetch('/v1/days?limit=200', {}, fetch),
		apiFetch(`/v1/days/${params.date}`, {}, fetch)
	]);
	if (!briefRes.ok) {
		// Propagate the real status — 404 = missing brief (expected for
		// dates without a daily run), but 401/500/etc. should surface as
		// auth/server errors rather than masquerading as "not found".
		error(briefRes.status === 404 ? 404 : briefRes.status, `Brief fetch failed (${briefRes.status})`);
	}
	if (!indexRes.ok) {
		error(indexRes.status, `Index fetch failed (${indexRes.status})`);
	}
	const indexBody: Paginated<DayIndexEntry> = await indexRes.json();
	const brief: DayBrief = await briefRes.json();
	return { days: indexBody.data, brief };
};
