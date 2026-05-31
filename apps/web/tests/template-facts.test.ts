/**
 * Hermetic smoke for the typed-facts panel (PR-3 of epic #321).
 *
 * Two scenarios:
 *  - Template-extracted catalyst → ``TemplateFacts`` renders inline with
 *    every typed key/value plus the source template_id badge.
 *  - Flash-extracted catalyst (no template match) → ``TemplateFacts``
 *    DOES NOT render so flash-only briefs keep their existing shape.
 *
 * Mocks the full ``/api/v1/days`` + ``/api/v1/days/<date>`` endpoints
 * inline (no on-disk fixture rename) so this spec stays orthogonal to
 * the broader smoke fixture set.
 */

import { expect, test, type Page } from '@playwright/test';

const MARKET_STATUS_TRADING_BODY = JSON.stringify({
	is_trading_day: true,
	is_half_day: false,
	next_open_iso: '2099-01-01T13:30:00+00:00',
	exchange: 'XNYS'
});

interface CandidateOverrides {
	ticker: string;
	brief_template_id: string | null;
	brief_template_facts: Record<string, unknown> | null;
}

function buildDayBody(date: string, candidates: CandidateOverrides[]): string {
	return JSON.stringify({
		date,
		n_candidates: candidates.length,
		n_themes: 1,
		top_theme: 'm_and_a',
		theme_counts: { m_and_a: candidates.length },
		candidates: candidates.map((c, idx) => ({
			date,
			theme: 'm_and_a',
			ticker: c.ticker,
			company_name: `${c.ticker} Inc`,
			rationale: 'test',
			gemini_confidence: 0.9,
			market_cap: 1e10,
			gates_passed: ['tenk', 'press'],
			gates_passed_str: 'tenk,press',
			n_gates_passed: 2,
			gates_failed: [],
			gates_failed_str: '',
			n_gates_failed: 0,
			gates_unknown: [],
			gates_unknown_str: '',
			n_gates_unknown: 0,
			verified: true,
			source_event_url: 'https://www.businesswire.com/x',
			source_event_title: `${c.ticker} acquisition`,
			source_event_published_at: date,
			theme_search_keywords: [],
			industry_id: 101001,
			industry_name: 'Computer Hardware',
			sector_name: 'Technology',
			peer_cohort_level: 'sic4',
			insider_score_usd: 0,
			insider_score_sector_percentile: 50,
			fcff_yield_pct: 5,
			fcff_yield_sector_percentile: 60,
			valuation_pe: 20,
			valuation_ps: 10,
			valuation_ev_rev: 11,
			valuation_ev_ebitda: 12,
			valuation_fcf_margin: 0.2,
			valuation_composite_sector_percentile: 55,
			valuation_financials_publish_date: '2026-04-30',
			valuation_financials_age_days: 31,
			roic_pct: 8,
			roe_pct: 12,
			magic_formula_health_pass: true,
			technical_rsi: 55,
			technical_ma50_distance_pct: 1,
			technical_atr_pct: 3,
			technical_volume_zscore: 1,
			technical_pct_off_52w_high: -5,
			technical_pct_off_52w_low: 30,
			technical_ma200_distance_pct: 5,
			technical_ma200_slope_pct_per_day: 0.1,
			technicals_summary_str: 'RSI 55 / MA50 +1%',
			catalyst_strength: 0.9,
			catalyst_event_type: 'm_and_a',
			catalyst_confidence: 1.0,
			magic_formula_rank: idx + 1,
			magic_formula_cohort_n: candidates.length,
			deep_drawdown_reversal: false,
			layer4_weighted_score: 4,
			also_in_themes: [],
			rank_in_day: idx + 1,
			cohort_size_in_day: candidates.length,
			next_earnings_date: null,
			brief_model_used: 'deepseek/deepseek-v4-pro',
			brief_tldr: `${c.ticker} acquisition.`,
			brief_supply_chain_md: 'Reasoning.',
			brief_bear_summary_md: 'Risks.',
			brief_catalyst_failure_exit: 'Exit.',
			brief_trade_setup: null,
			brief_template_id: c.brief_template_id,
			brief_template_facts: c.brief_template_facts,
			brief_generated_at: `${date}T10:00:00+00:00`
		}))
	});
}

function installMock(page: Page, dayBody: string, date: string) {
	return page.route('**/api/v1/**', (route) => {
		const url = new URL(route.request().url());
		if (url.pathname === '/api/v1/market/status') {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: MARKET_STATUS_TRADING_BODY
			});
		}
		if (url.pathname === '/api/v1/days') {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					data: [
						{ date, n_candidates: 1, n_themes: 1, top_theme: 'm_and_a' }
					],
					meta: { total: 1, limit: 200, offset: 0 }
				})
			});
		}
		const dayMatch = url.pathname.match(/^\/api\/v1\/days\/(\d{4}-\d{2}-\d{2})$/);
		if (dayMatch && dayMatch[1] === date) {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: dayBody
			});
		}
		return route.fulfill({ status: 404, body: '{}' });
	});
}

test('template-facts panel renders when brief_template_id is present', async ({ page }) => {
	const date = '2026-05-31';
	const dayBody = buildDayBody(date, [
		{
			ticker: 'NVDA',
			brief_template_id: 'm_and_a_press_release',
			brief_template_facts: {
				acquirer_ticker: 'NVDA',
				target_ticker: 'XYZ',
				consideration_usd: 5000000000,
				announcement_date: '2026-05-31'
			}
		}
	]);
	await installMock(page, dayBody, date);
	await page.goto(`/brief/${date}`);

	// Wait for the candidate card to be visible — confirms the day loaded.
	await expect(page.getByText('NVDA Inc')).toBeVisible();

	// The typed-facts panel renders with the template_id badge AND each key.
	const panel = page.getByTestId('template-facts');
	await expect(panel).toBeVisible();
	// Badge shows the friendly label, but data-template-id pins the raw
	// snake_case id (Prometheus-safe, audit trail). The verbatim-citation
	// contract scopes the LLM prompt; the SPA renderer may format for
	// readability so long as the raw value is one inspection away.
	const badge = page.getByTestId('template-id');
	await expect(badge).toHaveText('M&A press release');
	await expect(badge).toHaveAttribute('data-template-id', 'm_and_a_press_release');

	// Each key/value pair shows up in the dl grid. Sorted alphabetically by
	// key (component contract) — assert two of them anchor the order.
	const keys = page.getByTestId('template-fact-key');
	await expect(keys).toHaveCount(4);
	await expect(keys.first()).toHaveText('acquirer_ticker');

	// Values: tickers + date render verbatim; *_usd fields are formatted
	// compactly ($5.0B) but the raw integer is preserved on data-raw.
	const values = page.getByTestId('template-fact-value');
	const valueTexts = await values.allInnerTexts();
	expect(valueTexts).toContain('NVDA');
	expect(valueTexts).toContain('XYZ');
	expect(valueTexts).toContain('$5.0B');
	expect(valueTexts).toContain('2026-05-31');
	// Audit trail: raw integer survives on data-raw even though the
	// rendered text is the compact $5.0B form.
	const considerationCell = values.filter({ hasText: '$5.0B' });
	await expect(considerationCell).toHaveAttribute('data-raw', '5000000000');
});

test('template-facts panel hidden when brief_template_id is null (flash path)', async ({
	page
}) => {
	const date = '2026-05-31';
	const dayBody = buildDayBody(date, [
		{
			ticker: 'ABC',
			brief_template_id: null,
			brief_template_facts: null
		}
	]);
	await installMock(page, dayBody, date);
	await page.goto(`/brief/${date}`);

	await expect(page.getByText('ABC Inc')).toBeVisible();

	// Panel is not in the DOM at all on flash-extracted catalysts —
	// hidden via `{#if hasFacts}` block, not via display:none.
	await expect(page.getByTestId('template-facts')).toHaveCount(0);
});
