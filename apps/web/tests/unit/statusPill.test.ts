import { describe, expect, it } from 'vitest';
import { statusPillClass } from '../../src/lib/components/statusPill';

// Pins the pure class-composition behind <StatusPill> — the single shared
// bordered status/verdict pill shell reused across /experiments, /edge, the
// ladder legend and TemplateFacts. The tone (a precomputed "text-X border-X"
// string) is supplied by the domain; the shell + variant flags live here.

describe('statusPillClass', () => {
	const base = 'px-1.5 py-0.5 border uppercase tracking-widest';

	it('composes the base shell + default 10px size + tone', () => {
		expect(statusPillClass({ tone: 'text-red border-red' })).toBe(
			`${base} text-[10px] text-red border-red`
		);
	});

	it('size "9" swaps to the 9px variant', () => {
		const c = statusPillClass({ tone: 'text-green border-green', size: '9' });
		expect(c).toContain('text-[9px]');
		expect(c).not.toContain('text-[10px]');
	});

	it('flags add their utility only when true', () => {
		const off = statusPillClass({ tone: 't' });
		expect(off).not.toContain('whitespace-nowrap');
		expect(off).not.toContain('border-dashed');
		expect(off).not.toContain('cursor-help');

		const on = statusPillClass({ tone: 't', nowrap: true, dashed: true, interactive: true });
		expect(on).toContain('whitespace-nowrap');
		expect(on).toContain('border-dashed');
		expect(on).toContain('cursor-help');
	});

	it('appends extra classes last (e.g. inline-block shrink-0)', () => {
		const c = statusPillClass({ tone: 'text-cyan border-cyan', extra: 'inline-block shrink-0' });
		expect(c.endsWith('text-cyan border-cyan inline-block shrink-0')).toBe(true);
	});

	it('never emits empty/double spaces', () => {
		const c = statusPillClass({ tone: 'text-magenta border-magenta' });
		expect(c).not.toMatch(/\s{2,}/);
		expect(c.trim()).toBe(c);
	});
});
