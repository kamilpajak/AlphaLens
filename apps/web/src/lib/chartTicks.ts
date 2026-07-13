// X-axis tick generation for date-domain (time-scale) charts.
//
// A time scale places each tick at its true calendar pixel position. Feeding it
// the distinct data-dates subsampled BY LIST INDEX therefore lands ticks at
// uneven pixel gaps whenever the underlying dates are irregular (weekends, days
// with no observations). `evenTimeTicks` instead emits ticks at even WHOLE-DAY
// intervals across the domain, so the gaps are uniform and no two ticks ever
// collapse to the same M/D label.

const MS_PER_DAY = 86_400_000;

/** Parse a `YYYY-MM-DD` string to a UTC-midnight Date (matches the M/D formatter). */
const toUtcDate = (iso: string) => new Date(iso + 'T00:00:00Z');

/**
 * Evenly-spaced x-axis ticks across the range of `isoDates`.
 *
 * Ticks are anchored on the max date (so the right edge — the most recent
 * observation — is always labelled) and stepped backward by a constant whole
 * number of days chosen to land near `targetCount` ticks. All ticks are
 * UTC-midnight aligned and within `[min, max]`.
 *
 * Degenerate spans (0, 1, or 2 distinct dates) return those dates verbatim —
 * there is nothing to subdivide.
 */
export function evenTimeTicks(isoDates: string[], targetCount = 8): Date[] {
	const days = [...new Set(isoDates)].sort();
	if (days.length <= 2) return days.map(toUtcDate);

	const first = toUtcDate(days[0]).getTime();
	const last = toUtcDate(days[days.length - 1]).getTime();
	const spanDays = Math.round((last - first) / MS_PER_DAY);

	// Whole-day step >= 1 so ticks never share an M/D label; ~targetCount ticks.
	const stepDays = Math.max(1, Math.round(spanDays / Math.max(1, targetCount - 1)));
	const stepMs = stepDays * MS_PER_DAY;

	const ticks: Date[] = [];
	for (let t = last; t >= first; t -= stepMs) ticks.push(new Date(t));
	return ticks.reverse();
}
