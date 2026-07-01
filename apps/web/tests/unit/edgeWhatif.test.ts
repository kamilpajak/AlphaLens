import { describe, expect, it } from 'vitest';
import {
	WHATIF_LENS_REGISTRY,
	hasWhatif,
	resolveLensMeta,
	whatifLenses
} from '../../src/lib/edgeWhatif';
import type { WhatIfPanel } from '../../src/lib/types';

// Pins the client-side what-if registry mirror + the pure derivation the /edge
// sandbox renders. The labels + in_sample/validated status live HERE (the slim
// Django image serves the lens map keyed by lens_id only), so an unknown lens_id
// must degrade to a cautious, NOT-validated default rather than break the UI.

const panel = (lenses: WhatIfPanel['lenses']): WhatIfPanel => ({
	status: 'ok',
	n_matured: 120,
	threshold: 30,
	in_sample: true,
	note: 'counterfactual; in-sample; not validated',
	lenses
});

describe('resolveLensMeta', () => {
	it('resolves a registered lens to its label + status', () => {
		const m = resolveLensMeta('be_0p5r');
		expect(m.label).toBe('break-even +0.5R');
		expect(m.status).toBe('in_sample');
	});

	it('falls back for an unknown lens_id to the raw id + a cautious in_sample default', () => {
		const m = resolveLensMeta('be_9p9r_future');
		expect(m.label).toBe('be_9p9r_future');
		expect(m.status).toBe('in_sample'); // unknown is treated as NOT validated
	});
});

describe('whatifLenses', () => {
	it('maps + sorts lenses by id with their aggregates + resolved meta', () => {
		const views = whatifLenses(
			panel({
				be_0p5r: { n: 100, mean_r: 0.069, median_r: 0.044 },
				aa_unknown: { n: 5, mean_r: null, median_r: null }
			})
		);
		expect(views.map((v) => v.lensId)).toEqual(['aa_unknown', 'be_0p5r']);
		const be = views.find((v) => v.lensId === 'be_0p5r');
		expect(be?.label).toBe('break-even +0.5R');
		expect(be?.meanR).toBe(0.069);
		expect(be?.medianR).toBe(0.044);
		expect(be?.n).toBe(100);
	});
});

describe('hasWhatif', () => {
	it('is false for null / empty lenses, true when a lens is present', () => {
		expect(hasWhatif(null)).toBe(false);
		expect(hasWhatif(undefined)).toBe(false);
		expect(hasWhatif(panel({}))).toBe(false);
		expect(hasWhatif(panel({ be_0p5r: { n: 1, mean_r: 0, median_r: 0 } }))).toBe(true);
	});
});

describe('WHATIF_LENS_REGISTRY', () => {
	it('declares the break-even lens (mirror of pipeline BREAKEVEN_LENSES)', () => {
		expect(WHATIF_LENS_REGISTRY.be_0p5r).toBeDefined();
		expect(WHATIF_LENS_REGISTRY.be_0p5r.status).toBe('in_sample');
	});

	it('declares the fill-anchored lens (exit-geometry path b)', () => {
		expect(WHATIF_LENS_REGISTRY.fill_anchored_0p5atr).toBeDefined();
		expect(WHATIF_LENS_REGISTRY.fill_anchored_0p5atr.status).toBe('in_sample');
		expect(WHATIF_LENS_REGISTRY.fill_anchored_0p5atr.category).toBe('exit-stop');
	});
});
