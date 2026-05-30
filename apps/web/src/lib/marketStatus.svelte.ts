/**
 * Client for ``/v1/market/status`` — polls the Django endpoint that
 * projects XNYS calendar state for the closed-market banner.
 *
 * Single source-of-truth design: every component that needs market state
 * imports ``marketStatus`` from this module. Background refresh is owned
 * by ``startMarketStatusPoll`` (called once in ``+layout.svelte``); other
 * mounts read the Svelte 5 ``$state`` rune-backed export and never
 * trigger their own fetches.
 *
 * Refresh cadence: 60 s. The endpoint is read-only and downstream of an
 * in-process calendar lookup, so the cost is dominated by network RTT
 * (~50 ms cross-origin to api.alphalens.kamilpajak.pl). 60 s is short
 * enough that the banner appears/disappears within a session boundary,
 * long enough that an idle tab doesn't hammer CF Access. The SPA also
 * re-polls on ``visibilitychange`` so a tab waking from background
 * doesn't display a stale "closed" indicator after the market opens.
 *
 * Design memo: ``docs/research/paper_trading_non_trading_day_2026_05_29.md``
 * §5 (PR-C sequencing).
 */

import { apiFetch } from '$lib/api';

export interface MarketStatus {
	is_trading_day: boolean;
	is_half_day: boolean;
	next_open_iso: string;
	exchange: string;
}

interface MarketStatusState {
	/** Latest payload from the server, or null until the first successful poll. */
	value: MarketStatus | null;
	/** True after the first poll resolves (success OR failure). The banner
	 *  hides until this flips so the closed-market chrome doesn't flash
	 *  briefly during the initial fetch. */
	hasLoaded: boolean;
	/** Optional last error message — surfaced only in dev console. The
	 *  banner is a "fail-silent" surface: we'd rather show no banner than
	 *  a confusing "couldn't fetch market status" toast. */
	lastError: string | null;
}

/**
 * Svelte 5 rune-backed state. Components subscribe via ``$derived`` over
 * ``marketStatus.value`` exactly as they would for any rune.
 *
 * Exported as a single object (rather than three separate runes) so the
 * trio updates atomically — there's no observer window where ``hasLoaded``
 * is true but ``value`` is still stale from a previous response.
 */
export const marketStatus = $state<MarketStatusState>({
	value: null,
	hasLoaded: false,
	lastError: null
});

/**
 * Pull once and update the shared state. Exported for callers that want
 * a manual refresh (e.g. operator toggling a button), separate from the
 * background poll loop.
 */
export async function refreshMarketStatus(fetcher: typeof fetch = fetch): Promise<void> {
	try {
		const res = await apiFetch('/v1/market/status', {}, fetcher);
		if (!res.ok) {
			marketStatus.lastError = `HTTP ${res.status}`;
			marketStatus.hasLoaded = true;
			return;
		}
		const body = (await res.json()) as MarketStatus;
		marketStatus.value = body;
		marketStatus.lastError = null;
		marketStatus.hasLoaded = true;
	} catch (err) {
		// Fail silent — the banner just doesn't render. We deliberately
		// do NOT surface a "couldn't reach API" toast because /v1/market/status
		// failures are noise relative to the actual data fetches the user
		// cares about (briefs, candidates).
		marketStatus.lastError = err instanceof Error ? err.message : 'unknown error';
		marketStatus.hasLoaded = true;
	}
}

/** Default poll cadence in ms. Exported so tests can swap it. */
export const DEFAULT_POLL_INTERVAL_MS = 60_000;

/**
 * Start the background poll. Idempotent — calling twice does NOT spawn
 * a second interval (we track a module-level handle). Returns a cleanup
 * function for SvelteKit's ``$effect`` cleanup slot.
 */
let _pollHandle: ReturnType<typeof setInterval> | null = null;
let _visibilityHandler: (() => void) | null = null;

export function startMarketStatusPoll(
	intervalMs: number = DEFAULT_POLL_INTERVAL_MS
): () => void {
	if (_pollHandle !== null) {
		// Already running; subsequent callers get a no-op cleanup.
		return () => undefined;
	}

	// Fire one immediately so the banner state is correct before the
	// first interval elapses.
	void refreshMarketStatus();

	_pollHandle = setInterval(() => {
		void refreshMarketStatus();
	}, intervalMs);

	if (typeof document !== 'undefined') {
		_visibilityHandler = () => {
			if (document.visibilityState === 'visible') {
				void refreshMarketStatus();
			}
		};
		document.addEventListener('visibilitychange', _visibilityHandler);
	}

	return () => {
		if (_pollHandle !== null) {
			clearInterval(_pollHandle);
			_pollHandle = null;
		}
		if (_visibilityHandler !== null && typeof document !== 'undefined') {
			document.removeEventListener('visibilitychange', _visibilityHandler);
			_visibilityHandler = null;
		}
	};
}

/**
 * Format a millisecond duration as a compact "in 1h 24m" style string.
 *
 * Exported so the banner template can render it without re-deriving the
 * math and so tests can assert the formatting directly. Sub-minute
 * residual is rounded down to "<1m" instead of showing 0 — we'd rather
 * understate than disappear the countdown entirely on the last tick
 * before the market actually opens.
 */
export function formatCountdown(ms: number): string {
	if (ms <= 0) return 'opening now';
	const totalMinutes = Math.floor(ms / 60_000);
	if (totalMinutes < 1) return '<1m';
	const days = Math.floor(totalMinutes / (60 * 24));
	const hours = Math.floor((totalMinutes - days * 60 * 24) / 60);
	const minutes = totalMinutes - days * 60 * 24 - hours * 60;

	const parts: string[] = [];
	if (days > 0) parts.push(`${days}d`);
	if (hours > 0) parts.push(`${hours}h`);
	// Hide minutes for spans >= 1 day to keep the chip compact; the
	// banner re-renders every second anyway so the user sees the rollover
	// once we drop below 24 h.
	if (days === 0 && minutes > 0) parts.push(`${minutes}m`);
	return parts.join(' ');
}
