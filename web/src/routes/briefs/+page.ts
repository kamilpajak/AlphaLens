import type { PageLoad } from './$types';
import type { DayIndexEntry } from '$lib/types';

export const load: PageLoad = async ({ fetch }) => {
	try {
		const res = await fetch('/data/days.json');
		if (!res.ok) return { days: [] as DayIndexEntry[] };
		const days: DayIndexEntry[] = await res.json();
		return { days };
	} catch {
		return { days: [] as DayIndexEntry[] };
	}
};
