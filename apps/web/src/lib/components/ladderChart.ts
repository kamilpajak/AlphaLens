import type { ChartBar, ChartMarker } from '$lib/types';

/** Marker kinds that close (part of) a position. ENTRY is intentionally absent —
 *  it opens the position, never ends the in-trade window. TP_TOUCHED IS included:
 *  the replay marks exit_reached once every TP price level is touched, so a
 *  touched-but-unsold deeper TP still closes the in-trade window (matches the
 *  pipeline's holding period), even though it sold no tranche. */
const EXIT_KINDS: ReadonlySet<ChartMarker['kind']> = new Set([
	'TP',
	'TP_TOUCHED',
	'SL',
	'TIME_STOP'
]);

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

/** Join separator for a folded multi-tier entry label (E1·E2·E3). */
const TIER_JOIN = '·';

/** Fold coincident entry-tier markers into a single marker.
 *
 *  When a fast move fills several entry rungs in the SAME daily session (e.g. a
 *  gap-down open that trades through E1, E2 and E3 at once), every tier emits an
 *  ENTRY marker on that one bar. They all render `belowBar` at the same slot, so
 *  three arrows and their "E1"/"E2"/"E3" labels overlap into one illegible mark
 *  — the user then sees only "E1" and reasonably concludes E2/E3 are missing.
 *
 *  This collapses each same-day ENTRY group into ONE marker whose label joins the
 *  tier ids with `·` (E1·E2·E3), anchored at the first tier of the group and
 *  keeping EVERY other field (time/price/level_id/ambiguous) of that first tier —
 *  only the label is mutated. Non-ENTRY markers (TP/SL/…) and entries that fill on
 *  DISTINCT sessions pass through untouched — separating them in time is exactly
 *  the informative case we must not flatten. Order is preserved.
 *
 *  Grouping is EXACT `time` string equality, which is same-session iff bars are
 *  daily (YYYY-MM-DD) — the only resolution the payload builder emits. An
 *  intraday-bar migration would need a session-key normalization here first. */
export function collapseEntryMarkers(markers: ChartMarker[]): ChartMarker[] {
	const out: ChartMarker[] = [];
	// time -> index in `out` of the folded ENTRY marker for that session.
	const entryIndexByTime = new Map<string, number>();
	for (const m of markers) {
		if (m.kind !== 'ENTRY') {
			out.push(m);
			continue;
		}
		const existing = entryIndexByTime.get(m.time);
		if (existing === undefined) {
			entryIndexByTime.set(m.time, out.length);
			out.push({ ...m });
		} else {
			out[existing] = { ...out[existing], label: `${out[existing].label}${TIER_JOIN}${m.label}` };
		}
	}
	return out;
}

/** A deeper entry-tier horizontal line: its limit price and axis title. */
export interface EntryTierLine {
	price: number;
	title: string;
}

/** The filled entry tiers BELOW E1, each as a {price, title} price line.
 *
 *  The chart's single `entry` price line only ever draws E1 (the blended-entry
 *  anchor). When a multi-rung ladder also fills E2/E3, those deeper limit prices
 *  have no line, so the ladder geometry is invisible — worse when the tier
 *  markers collapse onto one bar. This returns every ENTRY marker deeper than E1
 *  (identified by its `level_id`) so each rung gets its own dashed line at its
 *  real price. Title is the lowercased tier id (e2, e3) to match the lowercase
 *  axis labels of the entry/tp/stop lines. Markers with no `level_id` are skipped
 *  — the tier identity is unknown, so we cannot say it is not E1. One line per
 *  tier id: a duplicated tier marker (a backend-contract violation — a tier
 *  fills once) must not draw two identical overlapping lines. */
export function deeperEntryTierLines(markers: ChartMarker[]): EntryTierLine[] {
	const lines: EntryTierLine[] = [];
	const seen = new Set<string>();
	for (const m of markers) {
		if (m.kind !== 'ENTRY') continue;
		const id = m.level_id?.trim().toLowerCase();
		if (!id || id === 'e1' || seen.has(id)) continue;
		seen.add(id);
		lines.push({ price: m.price, title: id });
	}
	return lines;
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
