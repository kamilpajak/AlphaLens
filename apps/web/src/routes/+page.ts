import { error, isHttpError } from '@sveltejs/kit';
import type { PageLoad } from './$types';
import { apiFetch } from '$lib/api';
import type { DayBrief, DayIndexEntry, Paginated } from '$lib/types';

const EMPTY = { days: [] as DayIndexEntry[], latestBrief: null as DayBrief | null };

export const load: PageLoad = async ({ fetch }) => {
	// The api cache is populated by `alphalens api rebuild-cache` on each
	// daily run. On a fresh VPS the SQLite file may not exist yet (api
	// returns 503 on /readyz but we don't poke that) or the cache may be
	// empty. We also degrade to the empty state on hard network failures
	// so the dashboard keeps rendering instead of crashing into the
	// SvelteKit error boundary.
	try {
		const indexRes = await apiFetch('/v1/days?limit=200', {}, fetch);
		// An expired Cloudflare Access session surfaces as 401 (apiFetch
		// normalises the CORS-blocked login redirect). Surface it as a
		// "session expired" error page rather than the misleading empty
		// "no briefs yet" state, which reads as data loss to the operator.
		if (indexRes.status === 401) error(401, 'Could not load the dashboard.');
		if (!indexRes.ok) return EMPTY;
		const indexBody: Paginated<DayIndexEntry> = await indexRes.json();
		const days = indexBody.data;
		if (days.length === 0) return { days, latestBrief: null as DayBrief | null };

		const latestRes = await apiFetch(`/v1/days/${days[0].date}`, {}, fetch);
		const latestBrief: DayBrief | null = latestRes.ok ? await latestRes.json() : null;
		return { days, latestBrief };
	} catch (e) {
		// error() throws an HttpError — let SvelteKit render +error.svelte
		// instead of the broad catch swallowing the 401 into the empty state.
		if (isHttpError(e)) throw e;
		return EMPTY;
	}
};
