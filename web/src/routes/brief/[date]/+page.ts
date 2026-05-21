import { error } from '@sveltejs/kit';
import type { PageLoad } from './$types';
import type { DayBrief, DayIndexEntry, Paginated } from '$lib/types';

export const load: PageLoad = async ({ fetch, params }) => {
	const [indexRes, briefRes] = await Promise.all([
		fetch('/api/v1/days?limit=200'),
		fetch(`/api/v1/days/${params.date}`)
	]);
	if (!briefRes.ok) {
		error(404, `No brief for ${params.date}`);
	}
	const indexBody: Paginated<DayIndexEntry> = await indexRes.json();
	const brief: DayBrief = await briefRes.json();
	return { days: indexBody.data, brief };
};
