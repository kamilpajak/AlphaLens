/**
 * Single source of truth for the Gemini model labels shown in the SPA.
 *
 * The SPA never displays exact preview IDs (e.g. `gemini-3.1-pro-preview`,
 * `gemini-3.5-flash`); those rot on every model bump. See the silent-failure
 * postmortem: `reference_gemini_model_retirement_silent_failure.md` —
 * retired IDs lingered in /about and the global footer ticker long after
 * PR #257 cut over the pipeline code, advertising a state the backend had
 * left behind. Pipeline source carries the exact IDs; the SPA shows brand
 * names that survive a minor-version bump (e.g. 3.5 → 3.6 Flash) without
 * re-staling. Bump these constants only when the brand family changes
 * (e.g. Gemini 3 → Gemini 4).
 */
export const GEMINI = {
	/** Used for theme→beneficiary mapping (L3) and high-confidence brief generation (L5 Pro path). */
	PRO: 'Gemini 3 Pro',
	/** Used for news event extraction (L2) and lower-confidence brief generation (L5 Flash path). */
	FLASH: 'Gemini 3 Flash',
	/** L5 routes Pro vs Flash on weighted_score; the about page renders both as a paired label. */
	PRO_OR_FLASH: 'Gemini 3 Pro / Flash'
} as const;
