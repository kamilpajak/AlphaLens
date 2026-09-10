import { describe, expect, it } from 'vitest';
import {
	defaultDir,
	isSortKeyVisible,
	sortOnViewChange,
	sortOutcomes,
	type SortKey
} from '../../src/lib/edgeSort';
import type { EdgeOutcome } from '../../src/lib/types';

const ALL_KEYS: SortKey[] = [
	'ticker',
	'class',
	'value',
	'hold',
	'brief',
	'closed',
	'book',
	'theme'
];

// Pins the pure client-side sort for the /edge outcomes table: nulls always sort
// LAST (so em-dash rows never jump to the top), a stable secondary tiebreaker
// (brief_date desc, ticker asc), and a sensible default direction per column kind.

function o(p: Partial<EdgeOutcome>): EdgeOutcome {
	return {
		ticker: 'AAA',
		brief_date: '2026-06-01',
		matured_at: null,
		theme: null,
		scorer_config_version: null,
		ladder_classification: 'TP_FULL',
		captured_tp_count: null,
		touched_tp_count: null,
		terminal: true,
		realized_r: null,
		open_r: null,
		market_excess_return: null,
		forward_return: null,
		benchmark_window_return: null,
		holding_days_elapsed: null,
		realized_return_pct_of_book: null,
		source: 'thematic',
		event_overlap: false,
		...p
	};
}

const tk = (rows: EdgeOutcome[]) => rows.map((r) => r.ticker);

describe('isSortKeyVisible', () => {
	it('hides the terminal-only columns (closed, book) in the ongoing view', () => {
		// Ongoing rows have no matured_at and no realized book %, so both columns
		// would render an em-dash for every row — sorting by them is meaningless.
		expect(isSortKeyVisible('closed', 'ongoing')).toBe(false);
		expect(isSortKeyVisible('book', 'ongoing')).toBe(false);
	});

	it('keeps every column visible in the terminal view', () => {
		for (const key of ALL_KEYS) {
			expect(isSortKeyVisible(key, 'terminal')).toBe(true);
		}
	});

	it('keeps the shared columns visible in the ongoing view', () => {
		const shared: SortKey[] = ['ticker', 'class', 'value', 'hold', 'brief', 'theme'];
		for (const key of shared) {
			expect(isSortKeyVisible(key, 'ongoing')).toBe(true);
		}
	});
});

describe('defaultDir', () => {
	it('is desc for numeric/date columns, asc for text', () => {
		expect(defaultDir('value')).toBe('desc');
		expect(defaultDir('closed')).toBe('desc');
		expect(defaultDir('hold')).toBe('desc');
		expect(defaultDir('book')).toBe('desc');
		expect(defaultDir('brief')).toBe('desc');
		expect(defaultDir('ticker')).toBe('asc');
		expect(defaultDir('class')).toBe('asc');
		expect(defaultDir('theme')).toBe('asc');
	});
});

describe('sortOutcomes', () => {
	it('sorts by closed (matured_at) desc with nulls last — the default', () => {
		const rows = [
			o({ ticker: 'OLD', matured_at: '2026-06-10' }),
			o({ ticker: 'OPEN', matured_at: null }),
			o({ ticker: 'NEW', matured_at: '2026-06-30' })
		];
		expect(tk(sortOutcomes(rows, 'closed', 'desc'))).toEqual(['NEW', 'OLD', 'OPEN']);
	});

	it('keeps nulls last even in asc direction', () => {
		const rows = [
			o({ ticker: 'OPEN', matured_at: null }),
			o({ ticker: 'A', matured_at: '2026-06-10' }),
			o({ ticker: 'B', matured_at: '2026-06-30' })
		];
		expect(tk(sortOutcomes(rows, 'closed', 'asc'))).toEqual(['A', 'B', 'OPEN']);
	});

	it('sorts by value (market_excess for terminal) numerically', () => {
		const rows = [
			o({ ticker: 'LO', market_excess_return: -0.14 }),
			o({ ticker: 'HI', market_excess_return: 0.29 }),
			o({ ticker: 'MID', market_excess_return: 0.11 })
		];
		expect(tk(sortOutcomes(rows, 'value', 'desc'))).toEqual(['HI', 'MID', 'LO']);
	});

	it('uses open_r as the value for ongoing rows', () => {
		const rows = [
			o({ ticker: 'A', terminal: false, open_r: -0.5, market_excess_return: 9 }),
			o({ ticker: 'B', terminal: false, open_r: 0.5, market_excess_return: -9 })
		];
		expect(tk(sortOutcomes(rows, 'value', 'desc'))).toEqual(['B', 'A']);
	});

	it('sorts by brief (brief_date) as a primary key honoring direction', () => {
		const rows = [
			o({ ticker: 'OLD', brief_date: '2026-06-01' }),
			o({ ticker: 'NEW', brief_date: '2026-06-28' }),
			o({ ticker: 'MID', brief_date: '2026-06-15' })
		];
		// asc = oldest recommendation first. The secondary fallback (brief_date DESC)
		// would order these newest-first, so this asserts the 'brief' PRIMARY case runs.
		expect(tk(sortOutcomes(rows, 'brief', 'asc'))).toEqual(['OLD', 'MID', 'NEW']);
		expect(tk(sortOutcomes(rows, 'brief', 'desc'))).toEqual(['NEW', 'MID', 'OLD']);
	});

	it('breaks equal brief_date ties by ticker asc', () => {
		const rows = [
			o({ ticker: 'Z', brief_date: '2026-06-05' }),
			o({ ticker: 'A', brief_date: '2026-06-05' })
		];
		expect(tk(sortOutcomes(rows, 'brief', 'asc'))).toEqual(['A', 'Z']);
	});

	it('sorts ticker alphabetically', () => {
		const rows = [o({ ticker: 'CRL' }), o({ ticker: 'ALT' }), o({ ticker: 'BAH' })];
		expect(tk(sortOutcomes(rows, 'ticker', 'asc'))).toEqual(['ALT', 'BAH', 'CRL']);
	});

	it('applies a stable secondary sort: brief_date desc then ticker asc', () => {
		// equal primary key (all class TP_FULL) -> secondary orders them.
		const rows = [
			o({ ticker: 'Z', brief_date: '2026-06-01' }),
			o({ ticker: 'A', brief_date: '2026-06-05' }),
			o({ ticker: 'B', brief_date: '2026-06-05' })
		];
		expect(tk(sortOutcomes(rows, 'class', 'asc'))).toEqual(['A', 'B', 'Z']);
	});

	it('does not mutate the input array', () => {
		const rows = [o({ ticker: 'B' }), o({ ticker: 'A' })];
		sortOutcomes(rows, 'ticker', 'asc');
		expect(tk(rows)).toEqual(['B', 'A']);
	});
});

// The view toggle (terminal ↔ ongoing) hides the terminal-only columns, so an
// active sort on one of them has to step aside. `sortOnViewChange` is the whole
// rule: step aside, hold the sort, put it back on the return trip.
describe('sortOnViewChange', () => {
	const CLOSED_DESC = { key: 'closed', dir: 'desc' } as const;
	const BRIEF_DESC = { key: 'brief', dir: 'desc' } as const;

	it('steps a terminal-only sort aside and holds it when leaving for ongoing', () => {
		const r = sortOnViewChange(CLOSED_DESC, null, 'ongoing');
		expect(r.sort).toEqual(BRIEF_DESC);
		expect(r.stashed).toEqual(CLOSED_DESC);
	});

	it('puts the held sort back when returning to terminal', () => {
		// The bug this pins: without the restore the table came back on `brief`
		// desc, which pushes every TIME_STOP row (old brief_date, recent
		// matured_at) to the bottom of the list.
		const r = sortOnViewChange(BRIEF_DESC, CLOSED_DESC, 'terminal');
		expect(r.sort).toEqual(CLOSED_DESC);
		expect(r.stashed).toBeNull();
	});

	it('holds the direction too, not just the column', () => {
		const closedAsc = { key: 'closed', dir: 'asc' } as const;
		const away = sortOnViewChange(closedAsc, null, 'ongoing');
		const back = sortOnViewChange(away.sort, away.stashed, 'terminal');
		expect(back.sort).toEqual(closedAsc);
	});

	it('holds the other terminal-only column (% book) the same way', () => {
		const bookDesc = { key: 'book', dir: 'desc' } as const;
		const away = sortOnViewChange(bookDesc, null, 'ongoing');
		expect(away.sort).toEqual(BRIEF_DESC);
		const back = sortOnViewChange(away.sort, away.stashed, 'terminal');
		expect(back.sort).toEqual(bookDesc);
	});

	it('leaves a sort on a shared column alone in both directions', () => {
		const tickerAsc = { key: 'ticker', dir: 'asc' } as const;
		const away = sortOnViewChange(tickerAsc, null, 'ongoing');
		expect(away).toEqual({ sort: tickerAsc, stashed: null });
		const back = sortOnViewChange(tickerAsc, null, 'terminal');
		expect(back).toEqual({ sort: tickerAsc, stashed: null });
	});

	it('keeps holding a sort whose column the next view still cannot show', () => {
		// Defensive: a held terminal-only sort must not be restored INTO ongoing.
		const r = sortOnViewChange(BRIEF_DESC, CLOSED_DESC, 'ongoing');
		expect(r.sort).toEqual(BRIEF_DESC);
		expect(r.stashed).toEqual(CLOSED_DESC);
	});

	it('has nothing to restore once the caller drops the held sort', () => {
		// The page clears the held sort when the reader picks a sort by hand —
		// that hand-picked sort must survive the return trip untouched.
		const themeAsc = { key: 'theme', dir: 'asc' } as const;
		const r = sortOnViewChange(themeAsc, null, 'terminal');
		expect(r).toEqual({ sort: themeAsc, stashed: null });
	});
});
