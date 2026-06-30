/** Client-side registry + derivation for the /edge break-even WHAT-IF sandbox.
 *
 * The Django API serves the what-if lens map keyed by `lens_id` ONLY (the slim
 * image must not import the pipeline registry), so the human labels + the
 * in_sample/validated status live HERE. Keep `WHATIF_LENS_REGISTRY` in sync with
 * `alphalens_pipeline/feedback/breakeven_lenses.py::BREAKEVEN_LENSES`. An unknown
 * lens_id (a pipeline lens added before this mirror is updated) degrades to a
 * cautious NOT-validated default rather than breaking the UI.
 */
import type { WhatIfLens, WhatIfPanel } from './types';

export type WhatIfStatus = 'in_sample' | 'validated';

export interface WhatIfLensMeta {
	label: string;
	status: WhatIfStatus;
	category: string;
}

/** Mirror of the pipeline BREAKEVEN_LENSES registry (labels + status only). */
export const WHATIF_LENS_REGISTRY: Record<string, WhatIfLensMeta> = {
	be_0p5r: { label: 'break-even +0.5R', status: 'in_sample', category: 'exit-stop' }
};

/** Resolve a served `lens_id` to its display metadata. Unknown ids fall back to
 *  the raw id as the label + a cautious `in_sample` status (never `validated`),
 *  so a pipeline lens added ahead of this mirror still renders, clearly unvalidated. */
export function resolveLensMeta(lensId: string): WhatIfLensMeta {
	return WHATIF_LENS_REGISTRY[lensId] ?? { label: lensId, status: 'in_sample', category: 'unknown' };
}

export interface WhatIfLensView extends WhatIfLensMeta {
	lensId: string;
	n: number;
	meanR: number | null;
	medianR: number | null;
}

/** Flatten the served lens map into sorted, metadata-resolved view rows. */
export function whatifLenses(panel: WhatIfPanel): WhatIfLensView[] {
	return Object.entries(panel.lenses)
		.map(([lensId, agg]: [string, WhatIfLens]) => ({
			lensId,
			...resolveLensMeta(lensId),
			n: agg.n,
			meanR: agg.mean_r,
			medianR: agg.median_r
		}))
		.sort((a, b) => a.lensId.localeCompare(b.lensId));
}

/** Whether the panel has any lens to show at all (fill-coverage > 0). */
export function hasWhatif(panel: WhatIfPanel | null | undefined): boolean {
	return !!panel && Object.keys(panel.lenses).length > 0;
}
