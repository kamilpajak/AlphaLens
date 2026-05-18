import { error } from '@sveltejs/kit';
import type { PageLoad } from './$types';
import type { DayBrief, DayIndexEntry } from '$lib/types';

export const load: PageLoad = async ({ fetch, params }) => {
	const [indexRes, briefRes] = await Promise.all([
		fetch('/data/days.json'),
		fetch(`/data/days/${params.date}.json`)
	]);
	if (!briefRes.ok) {
		error(404, `No brief for ${params.date}`);
	}
	const days: DayIndexEntry[] = await indexRes.json();
	const brief: DayBrief = await briefRes.json();
	return { days, brief };
};
