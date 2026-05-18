import type { PageLoad } from './$types';
import type { DayIndexEntry, DayBrief } from '$lib/types';

export const prerender = true;

export const load: PageLoad = async ({ fetch }) => {
	const indexRes = await fetch('/data/days.json');
	const days: DayIndexEntry[] = await indexRes.json();

	// Load most recent day in full for the hero section
	const latest = days[0];
	const latestRes = await fetch(`/data/days/${latest.date}.json`);
	const latestBrief: DayBrief = await latestRes.json();

	return { days, latestBrief };
};
