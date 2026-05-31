/**
 * Single source of truth for the LLM model labels shown in the SPA.
 *
 * The thematic pipeline migrated Gemini → DeepSeek V4 via OpenRouter
 * (PR-G #318, 2026-05-31). The SPA never displays exact preview/model
 * IDs (e.g. `deepseek/deepseek-v4-pro`, `deepseek/deepseek-v4-flash`);
 * those rot on every model bump. See the silent-failure postmortem:
 * `reference_gemini_model_retirement_silent_failure.md` — retired IDs
 * lingered in /about and the global footer ticker long after the
 * pipeline code cut over, advertising a state the backend had left
 * behind. Pipeline source carries the exact IDs; the SPA shows brand
 * names that survive a minor-version bump (e.g. a routing/version tweak)
 * without re-staling. Bump these constants only when the brand family
 * changes (e.g. DeepSeek V4 → V5).
 */
export const MODELS = {
	/** Used for theme→beneficiary mapping (L3) and high-confidence brief generation (L5 Pro path). */
	PRO: 'DeepSeek V4 Pro',
	/** Used for news event extraction (L2) and lower-confidence brief generation (L5 Flash path). */
	FLASH: 'DeepSeek V4 Flash',
	/** L5 routes Pro vs Flash on weighted_score; the about page renders both as a paired label. */
	PRO_OR_FLASH: 'DeepSeek V4 Pro / Flash'
} as const;
