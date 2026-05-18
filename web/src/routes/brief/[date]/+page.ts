import type { PageLoad } from './$types';
import type { DayBrief, DayIndexEntry } from '$lib/types';

export const prerender = true;

export const load: PageLoad = async ({ fetch, params }) => {
	const [indexRes, briefRes] = await Promise.all([
		fetch('/data/days.json'),
		fetch(`/data/days/${params.date}.json`)
	]);
	const days: DayIndexEntry[] = await indexRes.json();
	const brief: DayBrief = await briefRes.json();
	return { days, brief };
};
