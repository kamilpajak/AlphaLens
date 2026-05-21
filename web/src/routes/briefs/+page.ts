import type { PageLoad } from './$types';
import type { DayIndexEntry, Paginated } from '$lib/types';

// limit=200 caps the page at the API's MAX_LIMIT — the dashboard renders
// every brief day at once and there are nowhere near 200 days in cache.
export const load: PageLoad = async ({ fetch }) => {
	try {
		const res = await fetch('/api/v1/days?limit=200');
		if (!res.ok) return { days: [] as DayIndexEntry[] };
		const body: Paginated<DayIndexEntry> = await res.json();
		return { days: body.data };
	} catch {
		return { days: [] as DayIndexEntry[] };
	}
};
