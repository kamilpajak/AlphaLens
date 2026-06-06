/**
 * Footer "status strip" labels.
 *
 * These are DESCRIPTIVE mirrors of the pipeline's design constants — a
 * terminal-style flavour strip, NOT a live readout fetched from the API.
 * They are intentionally static (wiring a decorative strip to live backend
 * config would be over-engineering, and most entries describe a *method*,
 * not a single tunable number). If one of the cited Python constants
 * changes, update the matching value here to keep the strip honest.
 *
 * Two route-keyed vocabularies share one component: the thematic set
 * (dashboard / briefs / brief / about) describes the thematic-tool pipeline
 * (Polygon news + DeepSeek V4 Pro/Flash + verification gates); the
 * experiments set (/experiments) describes the active-alpha research ledger
 * (αt thresholds, Bonferroni, multi-phase audit, PIT discipline).
 */

import { MODELS } from '$lib/models';

export type FooterChip = { label: string; value: string };

export const tickerThematic: FooterChip[] = [
	// tri-state press-verification gate (pass / fail / unknown) —
	// alphalens_pipeline/thematic/mapping/orchestrator.py (issue #149)
	{ label: 'PRESS-GATE', value: 'tri-state ok' },
	// minimum catalyst-strength breakpoint —
	// alphalens_pipeline/thematic/screening/catalyst_signals.py::catalyst_floor
	{ label: 'CATALYST-FLOOR', value: '0.55' },
	// Greenblatt magic-formula rank within the daily candidate cohort —
	// alphalens_pipeline/thematic/argumentation/orchestrator.py (magic_formula_rank)
	{ label: 'MAGIC-FORMULA', value: 'cohort' },
	// theme→beneficiary mapping (L3) + high-confidence brief generation (L5)
	{ label: 'PRO-MODEL', value: MODELS.PRO },
	// news event extraction (L2) + lower-confidence brief generation (L5)
	{ label: 'FLASH-MODEL', value: MODELS.FLASH },
	// press-release lookback window —
	// alphalens_pipeline/thematic/mapping/catalyst_resolver.py::DEFAULT_LOOKBACK_DAYS
	{ label: 'PRESS-WINDOW', value: '30d' },
	// assumed slippage in the deterministic trade-setup geometry
	{ label: 'SLIPPAGE', value: '50bps' },
	// Polygon free-tier rate limit (5 requests / minute)
	{ label: 'LIMIT', value: 'polygon 5rpm' }
];

export const tickerExperiments: FooterChip[] = [
	{ label: 'DOCTRINE', value: 'αt ≥ 3.5 deploy' },
	{ label: 'MARGINAL', value: 'αt 2.0-3.5 paper' },
	{ label: 'NOISE', value: 'αt < 2.0' },
	{ label: 'BONFERRONI', value: 'escalates per test' },
	{ label: 'MULTI-PHASE', value: 'stride-5 mean ± std' },
	{ label: 'PIT', value: 'point-in-time mandatory' },
	{ label: 'SLIPPAGE-STRESS', value: '50bps half-spread' },
	{ label: 'LITERATURE', value: 'not oracle' }
];
