import { describe, expect, it } from 'vitest';
import {
	WHATIF_LENS_REGISTRY,
	hasWhatif,
	resolveLensMeta,
	whatifEarnsDisplay,
	whatifLenses
} from '../../src/lib/edgeWhatif';
import type { WhatIfLens, WhatIfPanel } from '../../src/lib/types';

// Pins the client-side what-if registry mirror + the pure derivation the /edge
// sandbox renders. The labels + in_sample/validated status live HERE (the slim
// Django image serves the lens map keyed by lens_id only), so an unknown lens_id
// must degrade to a cautious, NOT-validated default rather than break the UI.

// A fully-shaped lens aggregate with sensible empty defaults; override per case.
const lens = (p: Partial<WhatIfLens> = {}): WhatIfLens => ({
	n: 0,
	mean_r: null,
	median_r: null,
	realized_r_baseline: null,
	realized_r_baseline_n: 0,
	...p
});

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
				be_0p5r: { n: 100, mean_r: 0.069, median_r: 0.044, realized_r_baseline: -0.22, realized_r_baseline_n: 98 },
				aa_unknown: lens({ n: 5 })
			})
		);
		expect(views.map((v) => v.lensId)).toEqual(['aa_unknown', 'be_0p5r']);
		const be = views.find((v) => v.lensId === 'be_0p5r');
		expect(be?.label).toBe('break-even +0.5R');
		expect(be?.meanR).toBe(0.069);
		expect(be?.medianR).toBe(0.044);
		expect(be?.n).toBe(100);
	});

	it('surfaces the same-cohort realized baseline + its own n (not the panel-wide mean)', () => {
		const [be] = whatifLenses(
			panel({
				be_0p5r: { n: 60, mean_r: 0.09, median_r: 0.06, realized_r_baseline: -0.22, realized_r_baseline_n: 58 }
			})
		);
		expect(be.realizedRBaseline).toBe(-0.22);
		expect(be.realizedRBaselineN).toBe(58);
	});
});

describe('whatifEarnsDisplay', () => {
	// The sandbox renders only once the what-if "earns display": >=2 populated
	// lenses (a real head-to-head) OR a validated lens. A single in-sample lens —
	// the current live state (be_0p5r only) — does NOT earn a permanent panel.
	it('is false for null / empty', () => {
		expect(whatifEarnsDisplay(null)).toBe(false);
		expect(whatifEarnsDisplay(panel({}))).toBe(false);
	});

	it('is false for a single populated in-sample lens (current live state)', () => {
		expect(whatifEarnsDisplay(panel({ be_0p5r: lens({ n: 55, mean_r: 0.075, median_r: 0.044 }) }))).toBe(
			false
		);
	});

	it('is true once a second lens is populated (head-to-head)', () => {
		expect(
			whatifEarnsDisplay(
				panel({
					be_0p5r: lens({ n: 55, mean_r: 0.075, median_r: 0.044 }),
					fill_anchored_0p5atr: lens({ n: 12, mean_r: 0.2, median_r: 0.1 })
				})
			)
		).toBe(true);
	});

	it('does not count an empty (n=0) lens toward the head-to-head', () => {
		expect(
			whatifEarnsDisplay(
				panel({
					be_0p5r: lens({ n: 55, mean_r: 0.075, median_r: 0.044 }),
					fill_anchored_0p5atr: lens({ n: 0 })
				})
			)
		).toBe(false);
	});
});

describe('hasWhatif', () => {
	it('is false for null / empty lenses, true when a lens is present', () => {
		expect(hasWhatif(null)).toBe(false);
		expect(hasWhatif(undefined)).toBe(false);
		expect(hasWhatif(panel({}))).toBe(false);
		expect(hasWhatif(panel({ be_0p5r: lens({ n: 1, mean_r: 0, median_r: 0 }) }))).toBe(true);
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
