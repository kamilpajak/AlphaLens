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
		error(404, `No brief for ${params.date}`);
	}
	if (!indexRes.ok) {
		error(indexRes.status, `Index fetch failed (${indexRes.status})`);
	}
	const indexBody: Paginated<DayIndexEntry> = await indexRes.json();
	const brief: DayBrief = await briefRes.json();
	return { days: indexBody.data, brief };
};
