import { describe, it, expect } from 'vitest';
import {
	isPendingStatus,
	LADDER_STATUS,
	LADDER_STATUS_BY_CODE,
	ladderStatusBody,
	ladderStatusLabel,
	PENDING_STATUS
} from '../../src/lib/data/ladderStatus';

// Mirror of the classification strings the pipeline can emit
// (`ladder_replay._classify` + `LadderOutcome.status`). Kept here so a new
// status added pipeline-side without a UI gloss trips this test.
const PIPELINE_CODES = [
	'TP_FULL',
	'PARTIAL_TP_THEN_SL',
	'SL_HIT',
	'TIME_STOP',
	'PARTIAL_TP_OPEN',
	'OPEN',
	'NO_FILL',
	'BAD_GEOMETRY',
	'NO_STRUCTURE',
	'NO_DATA'
];

describe('ladderStatus glossary', () => {
	it('covers every pipeline classification code', () => {
		for (const code of PIPELINE_CODES) {
			expect(LADDER_STATUS_BY_CODE.has(code), `missing gloss for ${code}`).toBe(true);
		}
	});

	it('has no codes the pipeline cannot emit (no stale entries)', () => {
		for (const entry of LADDER_STATUS) {
			expect(PIPELINE_CODES, `unexpected gloss for ${entry.code}`).toContain(entry.code);
		}
	});

	it('every entry has a non-empty short gloss and body', () => {
		for (const entry of LADDER_STATUS) {
			expect(entry.short.length, `${entry.code} short`).toBeGreaterThan(0);
			expect(entry.body.length, `${entry.code} body`).toBeGreaterThan(0);
			expect(['ongoing', 'terminal', 'unmeasurable']).toContain(entry.group);
		}
	});

	it('codes are unique', () => {
		const codes = LADDER_STATUS.map((e) => e.code);
		expect(new Set(codes).size).toBe(codes.length);
	});
});

describe('ladderStatusBody', () => {
	it('resolves a known code (case- and whitespace-insensitive)', () => {
		expect(ladderStatusBody('OPEN')).toBe(LADDER_STATUS_BY_CODE.get('OPEN')!.body);
		expect(ladderStatusBody('  open ')).toBe(LADDER_STATUS_BY_CODE.get('OPEN')!.body);
		expect(ladderStatusBody('Tp_Full')).toBe(LADDER_STATUS_BY_CODE.get('TP_FULL')!.body);
	});

	it('returns the PENDING body for blank / null placeholders', () => {
		expect(ladderStatusBody(null)).toBe(PENDING_STATUS.body);
		expect(ladderStatusBody(undefined)).toBe(PENDING_STATUS.body);
		expect(ladderStatusBody('')).toBe(PENDING_STATUS.body);
		expect(ladderStatusBody('   ')).toBe(PENDING_STATUS.body);
	});

	it('falls back gracefully for an unknown non-empty code', () => {
		expect(ladderStatusBody('WAT')).toContain('no description');
	});
});

describe('pending placeholder helpers', () => {
	it('isPendingStatus is true only for blank / null', () => {
		expect(isPendingStatus(null)).toBe(true);
		expect(isPendingStatus(undefined)).toBe(true);
		expect(isPendingStatus('')).toBe(true);
		expect(isPendingStatus('   ')).toBe(true);
		expect(isPendingStatus('OPEN')).toBe(false);
		expect(isPendingStatus(' NO_FILL ')).toBe(false);
	});

	it('ladderStatusLabel shows PENDING for blank, else the trimmed code', () => {
		expect(ladderStatusLabel('')).toBe('PENDING');
		expect(ladderStatusLabel(null)).toBe('PENDING');
		expect(ladderStatusLabel('  OPEN ')).toBe('OPEN');
	});

	it('PENDING is NOT a pipeline code (stays out of LADDER_STATUS)', () => {
		expect(PIPELINE_CODES).not.toContain('PENDING');
		expect(LADDER_STATUS_BY_CODE.has('PENDING')).toBe(false);
	});
});
