/** Pure client-side sort for the /edge PER-CANDIDATE OUTCOMES table.
 *
 * The data is fully loaded, so sorting is client-side. Rules: NULLs always sort
 * LAST (an em-dash row must never jump to the top, regardless of direction), and a
 * stable secondary tiebreaker (brief_date desc, ticker asc) keeps the order
 * deterministic. The default (`closed` desc) surfaces the most-recently-completed
 * decision at the top for the terminal view, while ongoing rows (matured_at null)
 * fall back to brief_date desc — preserving the prior behaviour.
 */
import type { EdgeOutcome } from './types';

export type SortKey = 'ticker' | 'class' | 'value' | 'hold' | 'closed' | 'book' | 'theme';
export type SortDir = 'asc' | 'desc';

// Columns whose natural first-click direction is descending (numbers + dates read
// best high-to-low / newest-first); text columns default to ascending (A→Z).
const DESC_FIRST: ReadonlySet<SortKey> = new Set(['value', 'hold', 'closed', 'book']);

export function defaultDir(key: SortKey): SortDir {
	return DESC_FIRST.has(key) ? 'desc' : 'asc';
}

function keyValue(row: EdgeOutcome, key: SortKey): string | number | null {
	switch (key) {
		case 'ticker':
			return row.ticker;
		case 'class':
			return row.ladder_classification;
		case 'value':
			// terminal rows show benchmark-excess return; ongoing show the open R-multiple.
			return row.terminal ? row.market_excess_return : row.open_r;
		case 'hold':
			return row.holding_days_elapsed;
		case 'closed':
			// 'YYYY-MM-DD' sorts lexicographically = chronologically.
			return row.matured_at;
		case 'book':
			return row.realized_return_pct_of_book;
		case 'theme':
			return row.theme;
	}
}

function secondary(x: EdgeOutcome, y: EdgeOutcome): number {
	if (x.brief_date !== y.brief_date) return x.brief_date < y.brief_date ? 1 : -1; // desc
	return x.ticker < y.ticker ? -1 : x.ticker > y.ticker ? 1 : 0; // asc
}

/** Return a NEW array sorted by `key`/`dir`; nulls last; stable secondary order. */
export function sortOutcomes(rows: EdgeOutcome[], key: SortKey, dir: SortDir): EdgeOutcome[] {
	const sign = dir === 'asc' ? 1 : -1;
	return [...rows].sort((x, y) => {
		const a = keyValue(x, key);
		const b = keyValue(y, key);
		const aNull = a === null;
		const bNull = b === null;
		if (aNull && bNull) return secondary(x, y);
		if (aNull) return 1; // nulls last, independent of dir
		if (bNull) return -1;
		const primary = (a < b ? -1 : a > b ? 1 : 0) * sign;
		return primary !== 0 ? primary : secondary(x, y);
	});
}
