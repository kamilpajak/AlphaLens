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
	//
	// An expired Cloudflare Access session surfaces as a synthetic 401 from
	// apiFetch — and apiFetch ALSO flips the global session-expiry store, so
	// the layout-level re-auth overlay fires on top of the (empty) page. The
	// loader therefore no longer escalates 401 to the full-page error boundary;
	// it just degrades to EMPTY like every other non-OK status and lets the
	// single global modal own the re-auth prompt.
	try {
		const indexRes = await apiFetch('/v1/days?limit=200', {}, fetch);
		if (!indexRes.ok) return EMPTY;
		const indexBody: Paginated<DayIndexEntry> = await indexRes.json();
		const days = indexBody.data;
		if (days.length === 0) return { days, latestBrief: null as DayBrief | null };

		const latestRes = await apiFetch(`/v1/days/${days[0].date}`, {}, fetch);
		const latestBrief: DayBrief | null = latestRes.ok ? await latestRes.json() : null;
		return { days, latestBrief };
	} catch {
		return EMPTY;
	}
};
