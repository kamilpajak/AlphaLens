/** Client-side registry + derivation for the /edge break-even WHAT-IF sandbox.
 *
 * The Django API serves the what-if lens map keyed by `lens_id` ONLY (the slim
 * image must not import the pipeline registry), so the human labels, the
 * in_sample/validated status AND the per-lens scope live HERE — the served
 * `note` says so, rather than pointing a standalone API consumer at fields the
 * payload does not carry. Keep `WHATIF_LENS_REGISTRY` in sync with
 * `alphalens_pipeline/feedback/breakeven_lenses.py::BREAKEVEN_LENSES`. An unknown
 * lens_id (a pipeline lens added before this mirror is updated) degrades to a
 * cautious NOT-validated default rather than breaking the UI.
 */
import type { WhatIfLens, WhatIfPanel } from './types';

export type WhatIfStatus = 'in_sample' | 'validated';

export interface WhatIfLensMeta {
	label: string;
	status: WhatIfStatus;
	/** Short scope token for the chip — how much of the plan this lens replaces. */
	category: string;
	/** One-line scope sentence for the SELECTED lens (#1160). Per-lens on purpose:
	 *  the panel used to state one blanket "alternative exit-stop" over all of
	 *  them, which is false for the fill-anchored and both bracket lenses. */
	replaces: string;
}

/** Mirror of the pipeline BREAKEVEN_LENSES registry (label + status + scope).
 *  Entries MUST stay FLAT object literals — the cross-language parity guard
 *  (`test_spa_registry_mirrors_pipeline_lenses`) matches `id: { ... }` up to the
 *  first closing brace, so a nested value silently breaks it. */
export const WHATIF_LENS_REGISTRY: Record<string, WhatIfLensMeta> = {
	be_0p5r: {
		label: 'break-even +0.5R',
		status: 'in_sample',
		category: 'stop only',
		replaces:
			'the stop only - the brief TP ladder and entry tiers are kept; once +0.5R is reached the stop ratchets up to break-even'
	},
	fill_anchored_0p5atr: {
		label: 'fill-anchored stop (0.5·ATR)',
		status: 'in_sample',
		category: 'stop + entry',
		replaces:
			'the stop AND the entry ladder - entries collapse to a single 100% tier at E1 and the stop sits 0.5xATR below that fill; the brief TP ladder is kept'
	},
	be_0p5r_trail0p6: {
		label: 'break-even +0.5R · trail 0.6',
		status: 'in_sample',
		category: 'stop only',
		replaces:
			'the stop only - the brief TP ladder and entry tiers are kept; past +0.5R the stop trails at 0.6 of the peak gain instead of sitting at break-even'
	},
	atr_bracket_1p5: {
		label: 'ATR bracket 1.5 (bezpazery) · realised-fill anchor',
		status: 'in_sample',
		category: 'whole exit',
		replaces:
			'the WHOLE exit - the brief TP tranches are discarded for a single 100% target at anchor +1.5xATR (floor +0.6%, capped at the 52-week high), with a static stop at anchor -1.5xATR (no ratchet, no trail); entry tiers are kept and the anchor is the realised fill blend'
	},
	atr_bracket_1p5_planned: {
		label: 'ATR bracket 1.5 (bezpazery) · planned-blend anchor',
		status: 'in_sample',
		category: 'whole exit',
		replaces:
			'the WHOLE exit - the brief TP tranches are discarded for a single 100% target at anchor +1.5xATR (floor +0.6%, capped at the 52-week high), with a static stop at anchor -1.5xATR (no ratchet, no trail); entry tiers are kept and the anchor is the planned blend over ALL intended tiers, which is what the live rail places against'
	},
	be_0p5r_trail0p6_ttl7: {
		label: 'break-even +0.5R · trail 0.6 · entry TTL 7',
		status: 'in_sample',
		category: 'stop only',
		replaces:
			'the stop only - the brief TP ladder and entry tiers are kept; past +0.5R the stop trails at 0.6 of the peak gain, and entry limits expire after the production 7-session order TTL, so tiers the live rail could never fill do not fill here'
	}
};

/** Resolve a served `lens_id` to its display metadata. Unknown ids fall back to
 *  the raw id as the label + a cautious `in_sample` status (never `validated`),
 *  so a pipeline lens added ahead of this mirror still renders, clearly unvalidated. */
export function resolveLensMeta(lensId: string): WhatIfLensMeta {
	return (
		WHATIF_LENS_REGISTRY[lensId] ?? {
			label: lensId,
			status: 'in_sample',
			category: 'unknown',
			// Never guess a scope for an unmirrored lens: claiming the wrong one
			// with confidence is the defect this field exists to prevent (#1160).
			replaces: 'not in the SPA mirror yet - scope unknown'
		}
	);
}

export interface WhatIfLensView extends WhatIfLensMeta {
	lensId: string;
	n: number;
	meanR: number | null;
	medianR: number | null;
	/** Same-cohort realized-R baseline (this lens's own contributing rows) the
	 *  "vs realized" figure compares against — NOT the panel-wide gross mean. */
	realizedRBaseline: number | null;
	realizedRBaselineN: number;
	/** Paired per-row direction counts over the baseline cohort (strict inequality;
	 *  ties feed neither side). Null below the N-gate, like the means. */
	nHelped: number | null;
	nHarmed: number | null;
	/** Provenance ref (design-memo section) when the lens's parameters were fixed
	 *  BEFORE registration; null for in-sample-tuned lenses. */
	preregisteredRef: string | null;
}

/** Flatten the served lens map into sorted, metadata-resolved view rows. */
export function whatifLenses(panel: WhatIfPanel): WhatIfLensView[] {
	return Object.entries(panel.lenses)
		.map(([lensId, agg]: [string, WhatIfLens]) => ({
			lensId,
			...resolveLensMeta(lensId),
			n: agg.n,
			meanR: agg.mean_r,
			medianR: agg.median_r,
			realizedRBaseline: agg.realized_r_baseline,
			realizedRBaselineN: agg.realized_r_baseline_n,
			// `?? null` is a deploy-transition shim: an API image predating these
			// fields serves lens objects without them (undefined), which must render
			// exactly like the gated null, never as "undefined" text.
			nHelped: agg.n_helped ?? null,
			nHarmed: agg.n_harmed ?? null,
			preregisteredRef: agg.preregistered_ref ?? null
		}))
		.sort((a, b) => a.lensId.localeCompare(b.lensId));
}

/** Whether the panel has any lens to show at all (fill-coverage > 0). */
export function hasWhatif(panel: WhatIfPanel | null | undefined): boolean {
	return !!panel && Object.keys(panel.lenses).length > 0;
}

/** Whether the sandbox has EARNED a permanent render (doctrine: surface a lens
 *  only when it earns display). True once >=2 lenses are populated (n>0 — a real
 *  head-to-head) OR any lens is validated. A single in-sample lens — the current
 *  live state (be_0p5r only, fill_anchored forward-only) — does NOT qualify, so the
 *  panel stays hidden until the head-to-head matures, then reappears with zero
 *  code change (the registry + Django aggregate stay wired). */
export function whatifEarnsDisplay(panel: WhatIfPanel | null | undefined): boolean {
	if (!panel) return false;
	const views = whatifLenses(panel);
	return views.filter((v) => v.n > 0).length >= 2 || views.some((v) => v.status === 'validated');
}
