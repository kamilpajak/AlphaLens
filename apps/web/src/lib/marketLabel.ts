/**
 * Human-readable market label for an ISO 10383 exchange MIC.
 *
 * The footer session chip used to render the raw MIC (``XNYS``). That is
 * technically the calendar anchor, but it under-sells the candidate universe:
 * AlphaLens selects US-listed common stocks across XNYS (NYSE), XNAS (Nasdaq)
 * and XASE (NYSE American), which all share one US-equity session calendar. A
 * lone "XNYS" reads as "NYSE only". Collapsing every US venue to "US MARKET"
 * states the real scope while staying accurate about the session (the three
 * venues open/close together).
 *
 * Extending to a new venue (e.g. Warsaw once ``/v1/market/status`` reports a
 * ``XWAR`` session) is a one-line map entry — ``XWAR: 'PL MARKET'``. Until then
 * an unmapped MIC falls back to itself, so a newly wired exchange renders its
 * raw MIC rather than a wrong "US MARKET" or a blank chip.
 */

const MARKET_LABELS: Record<string, string> = {
	// US cash-equity venues — one shared session calendar (09:30–16:00 ET).
	XNYS: 'US MARKET', // New York Stock Exchange
	XNAS: 'US MARKET', // Nasdaq
	XASE: 'US MARKET' // NYSE American (ex-AMEX)
};

export function marketLabel(mic: string): string {
	const key = mic.trim().toUpperCase();
	return MARKET_LABELS[key] ?? key;
}
