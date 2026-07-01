import { describe, expect, it } from 'vitest';
import { statusRail, alphaBadgeTone } from '../../src/lib/data/research-ledger';

// Pins the two pure UI helpers behind the /experiments scannability pass:
// - statusRail turns a "text-X border-X" tone into a left-rail class so each
//   ledger row carries a status-coloured edge (glanceable verdict column).
// - alphaBadgeTone colours the glanceable αt header badge by the same doctrine
//   thresholds the IS/OOS bars use (<0 red · <2 muted · <3.5 amber · ≥3.5 green).

describe('statusRail', () => {
	it('extracts the border colour into a left rail', () => {
		expect(statusRail('text-red border-red')).toBe('border-l-2 border-red');
		expect(statusRail('text-cyan border-cyan')).toBe('border-l-2 border-cyan');
		expect(statusRail('text-green border-green')).toBe('border-l-2 border-green');
	});
	it('falls back to the grid border when no border token is present', () => {
		expect(statusRail('')).toBe('border-l-2 border-grid');
		expect(statusRail('text-fg-dim')).toBe('border-l-2 border-grid');
	});
});

describe('alphaBadgeTone', () => {
	it('null → muted', () => {
		expect(alphaBadgeTone(null)).toContain('border-grid');
	});
	it('negative → red', () => {
		expect(alphaBadgeTone(-1.2)).toBe('text-red border-red');
	});
	it('below the marginal bar (2.0) → muted', () => {
		expect(alphaBadgeTone(0.15)).toBe('text-fg-muted border-grid');
		expect(alphaBadgeTone(1.99)).toBe('text-fg-muted border-grid');
	});
	it('marginal band [2.0, 3.5) → amber', () => {
		expect(alphaBadgeTone(2.0)).toBe('text-amber border-amber');
		expect(alphaBadgeTone(3.49)).toBe('text-amber border-amber');
	});
	it('doctrine bar (≥3.5) → green', () => {
		expect(alphaBadgeTone(3.5)).toBe('text-green border-green');
		expect(alphaBadgeTone(4.2)).toBe('text-green border-green');
	});
});
