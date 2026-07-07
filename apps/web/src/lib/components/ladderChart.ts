import type { ChartBar, ChartMarker } from '$lib/types';

/** Marker kinds that close (part of) a position. ENTRY is intentionally absent —
 *  it opens the position, never ends the in-trade window. */
const EXIT_KINDS: ReadonlySet<ChartMarker['kind']> = new Set(['TP', 'SL', 'TIME_STOP']);

/** The time of the position's FINAL exit crossing, or null if it never exited.
 *
 *  The in-trade shading band ends here. Markers are chronological (built from the
 *  replay sequence), so for a multi-tranche scale-out (TP1 → TP2 → TP3) the
 *  position is fully closed only at the LAST take-profit — scanning from the end
 *  returns that terminal exit, not the first partial TP. A PARTIAL_TP_THEN_SL
 *  ends at the SL; a single-TP ladder at its one TP. Returns null when only an
 *  ENTRY (or nothing) is present, i.e. an open/plan-preview ladder whose band
 *  runs to "now" instead. */
export function finalExitMarkerTime(markers: ChartMarker[]): string | null {
	for (let i = markers.length - 1; i >= 0; i--) {
		if (EXIT_KINDS.has(markers[i].kind)) return markers[i].time;
	}
	return null;
}

/** The bar time anchoring the "brief" vertical line: the first bar at/after
 *  brief_date, i.e. the arrival session (session_on_or_after semantics), since
 *  a brief dated on a non-trading day snaps forward to the next session.
 *  ISO YYYY-MM-DD strings compare correctly lexicographically. Assumes bars
 *  are chronological — the payload builder always emits them in session order.
 *  Null means "draw nothing" — no bars (NO_DATA) or the brief postdates every
 *  bar. */
export function briefLineTime(
	bars: ChartBar[],
	briefDate: string | null | undefined
): string | null {
	if (!briefDate) return null;
	return bars.find((b) => b.time >= briefDate)?.time ?? null;
}
