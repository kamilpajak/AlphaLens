import { describe, it, expect } from 'vitest';
import { formatLocalWeekdayTime, formatLocalClock } from '../../src/lib/localTime';

// The footer used to mix two zones unlabelled: the next-open label rendered
// in exchange-local time (ET for XNYS) while the ambient clock rendered in
// UTC. Both now render in the *viewer's* local zone (DST-aware via Intl), so
// the two readings agree. These helpers take explicit `timeZone`/`locale`
// overrides ONLY so the suite is deterministic regardless of the CI runner's
// system zone; production omits them and inherits the browser's own settings.

describe('formatLocalWeekdayTime — next-open label in the viewer local zone', () => {
	// 2026-06-22T13:30Z == the NYSE 09:30 open in EDT (summer). The same instant
	// is a different wall-clock reading per viewer, but always a Monday here.
	const open = '2026-06-22T13:30:00Z';

	it('renders weekday + 24h time in the requested zone, lowercase, no tz token', () => {
		expect(formatLocalWeekdayTime(open, { timeZone: 'Europe/Warsaw' })).toBe('mon 15:30');
		expect(formatLocalWeekdayTime(open, { timeZone: 'America/New_York' })).toBe('mon 09:30');
		expect(formatLocalWeekdayTime(open, { timeZone: 'Asia/Tokyo' })).toBe('mon 22:30');
	});

	it('keeps English weekday names regardless of locale (the footer copy is English)', () => {
		// Even a pl-PL viewer sees "mon", not "pon." — only the time shifts zone.
		expect(formatLocalWeekdayTime(open, { timeZone: 'Europe/Warsaw', locale: 'pl-PL' })).toBe(
			'mon 15:30'
		);
	});
});

describe('formatLocalClock — ambient footer clock in the viewer local zone', () => {
	const d = new Date('2026-06-22T11:53:00Z');

	it('keeps an ISO YYYY-MM-DD date with 24h time and a trailing zone label', () => {
		expect(formatLocalClock(d, { timeZone: 'Europe/Warsaw', locale: 'pl-PL' })).toBe(
			'2026-06-22 13:53 CEST'
		);
		expect(formatLocalClock(d, { timeZone: 'America/New_York', locale: 'en-US' })).toBe(
			'2026-06-22 07:53 EDT'
		);
		expect(formatLocalClock(d, { timeZone: 'Asia/Tokyo', locale: 'ja-JP' })).toBe(
			'2026-06-22 20:53 JST'
		);
	});

	it('falls back to a GMT offset label where the locale has no named abbreviation', () => {
		// en viewers get "GMT+8" for Hong Kong (no English short name) — still
		// unambiguous, which is the whole point of labelling the zone.
		expect(formatLocalClock(d, { timeZone: 'Asia/Hong_Kong', locale: 'en-US' })).toBe(
			'2026-06-22 19:53 GMT+8'
		);
	});
});
