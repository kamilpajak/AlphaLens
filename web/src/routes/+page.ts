import type { PageLoad } from './$types';
import type { DayIndexEntry, DayBrief } from '$lib/types';

const EMPTY = { days: [] as DayIndexEntry[], latestBrief: null as DayBrief | null };

export const load: PageLoad = async ({ fetch }) => {
	// /data/days.json is rewritten by the pipeline. On a fresh VPS the file
	// may not exist yet (nginx 404) or may be the empty array. We also
	// degrade to the empty state on hard network failures so the dashboard
	// keeps rendering instead of crashing into the SvelteKit error boundary.
	try {
		const indexRes = await fetch('/data/days.json');
		if (!indexRes.ok) return EMPTY;
		const days: DayIndexEntry[] = await indexRes.json();
		if (days.length === 0) return { days, latestBrief: null as DayBrief | null };

		const latestRes = await fetch(`/data/days/${days[0].date}.json`);
		const latestBrief: DayBrief | null = latestRes.ok ? await latestRes.json() : null;
		return { days, latestBrief };
	} catch {
		return EMPTY;
	}
};
