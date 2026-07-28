import { describe, it, expect } from 'vitest';
import day from '../fixtures/api-mock/days/2026-05-18.json';

// Fixture-consistency pin for the honest "brief unavailable" state (#921):
// per alphalens_pipeline/thematic/argumentation/orchestrator.py, a row with
// brief_status "unavailable" has ALL LLM prose columns None — `b = brief or {}`
// makes every `b.get(...)` column None when the LLM brief failed. Only the
// deterministic trade setup and the brief_generated_at stamp are written
// regardless of the LLM outcome, so the mock fixture must mirror that shape.
const LLM_PROSE_COLUMNS = [
	'brief_model_used',
	'brief_tldr',
	'brief_supply_chain_md',
	'brief_bear_summary_md',
	'brief_catalyst_failure_exit'
] as const;

describe('days/2026-05-18.json brief_status "unavailable" rows', () => {
	const unavailable = day.candidates.filter((c) => c.brief_status === 'unavailable');

	it('contains at least one unavailable row to pin', () => {
		expect(unavailable.length).toBeGreaterThan(0);
	});

	it('nulls every LLM prose column on unavailable rows', () => {
		for (const candidate of unavailable) {
			for (const column of LLM_PROSE_COLUMNS) {
				expect(candidate[column], `${candidate.ticker}.${column}`).toBeNull();
			}
		}
	});

	it('keeps the deterministic trade setup and the generated_at stamp', () => {
		for (const candidate of unavailable) {
			expect(candidate.brief_trade_setup, `${candidate.ticker}.brief_trade_setup`).not.toBeNull();
			expect(
				candidate.brief_generated_at,
				`${candidate.ticker}.brief_generated_at`
			).not.toBeNull();
		}
	});
});
