import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES = resolve(__dirname, 'fixtures/api-mock');
const SUMMARY = JSON.parse(readFileSync(resolve(FIXTURES, 'edge-summary.json'), 'utf-8'));

// Switching the outcomes table to ONGOING and back must leave the terminal sort
// where it was. The terminal default is `closed` desc; `closed` has no column in
// the ongoing view, so the switch steps the sort aside onto `brief` — and the
// return trip has to put `closed` back. Without the restore the terminal table
// comes back on `brief` desc, which buries every TIME_STOP row: a TIME_STOP
// carries an OLD brief_date (the position aged to its TTL) with a RECENT
// matured_at, so "newest brief first" sorts the whole class to the bottom.

function mk(ticker: string, cls: string, brief: string, matured: string | null, terminal: boolean) {
	return {
		ticker,
		brief_date: brief,
		matured_at: matured,
		theme: 'ai-infra',
		ladder_classification: cls,
		captured_tp_count: null,
		touched_tp_count: null,
		terminal,
		realized_r: terminal ? 0.5 : null,
		open_r: terminal ? null : 0.3,
		market_excess_return: terminal ? 0.1 : null,
		forward_return: terminal ? 0.05 : null,
		benchmark_window_return: 0.02,
		holding_days_elapsed: 10,
		realized_return_pct_of_book: terminal ? 0.1 : null,
		scorer_config_version: 'v1',
		source: 'thematic',
		event_overlap: false
	};
}

// Two TIME_STOP rows with old briefs and the most recent closes, two fast rows
// with new briefs and older closes: the shape where `closed` desc and `brief`
// desc give opposite orders.
const OUTCOMES = {
	data: [
		mk('TSTOPA', 'TIME_STOP', '2026-08-01', '2026-09-05', true),
		mk('TSTOPB', 'TIME_STOP', '2026-08-02', '2026-09-04', true),
		mk('FASTA', 'SL_HIT', '2026-09-01', '2026-09-02', true),
		mk('FASTB', 'TP_FULL', '2026-09-02', '2026-09-03', true),
		mk('LIVEA', 'OPEN', '2026-09-03', null, false)
	],
	total: 5,
	returned: 5,
	truncated: false,
	facets: {
		status: { terminal: 4, ongoing: 1 },
		classification: {
			terminal: { TIME_STOP: 2, SL_HIT: 1, TP_FULL: 1 },
			ongoing: { OPEN: 1 }
		}
	}
};

const CLOSED_DESC = ['TSTOPA', 'TSTOPB', 'FASTB', 'FASTA'];

async function stub(page: import('@playwright/test').Page) {
	await page.route('**/api/v1/market/status**', (r) =>
		r.fulfill({
			json: {
				is_trading_day: false,
				is_half_day: false,
				is_open_now: false,
				next_open_iso: '2099-01-01T13:30:00+00:00',
				next_close_iso: '2099-01-01T20:00:00+00:00',
				exchange: 'XNYS'
			}
		})
	);
	await page.route('**/api/v1/days**', (r) =>
		r.fulfill({ json: { data: [], meta: { total: 0, limit: 200, offset: 0 } } })
	);
	await page.route('**/v1/edge/summary**', (r) => r.fulfill({ json: SUMMARY }));
	await page.route('**/v1/edge/outcomes**', (r) => r.fulfill({ json: OUTCOMES }));
}

const rowLinks = (page: import('@playwright/test').Page) => page.locator('tbody a[href^="/brief/"]');
const activeSortHeader = (page: import('@playwright/test').Page) =>
	page.locator('thead button.text-amber');
const viewButton = (page: import('@playwright/test').Page, view: 'terminal' | 'ongoing') =>
	page.getByRole('button', { name: new RegExp(`^${view} \\[`) });

test('the terminal sort survives a round trip through the ongoing view', async ({ page }) => {
	await stub(page);
	await page.goto('/edge');
	await expect(rowLinks(page)).toHaveCount(4);
	await expect(activeSortHeader(page)).toHaveText('closed ▼');
	expect(await rowLinks(page).allTextContents()).toEqual(CLOSED_DESC);

	await viewButton(page, 'ongoing').click();
	await expect(rowLinks(page)).toHaveCount(1);
	// The terminal-only column is gone here, so the sort steps aside onto `brief`.
	await expect(activeSortHeader(page)).toHaveText('brief ▼');

	await viewButton(page, 'terminal').click();
	await expect(rowLinks(page)).toHaveCount(4);
	await expect(activeSortHeader(page)).toHaveText('closed ▼');
	expect(await rowLinks(page).allTextContents()).toEqual(CLOSED_DESC);
});

test('a hand-picked sort wins over the restore', async ({ page }) => {
	await stub(page);
	await page.goto('/edge');
	await expect(rowLinks(page)).toHaveCount(4);

	await viewButton(page, 'ongoing').click();
	await expect(activeSortHeader(page)).toHaveText('brief ▼');
	// Picking a sort by hand in the ongoing view discards the held `closed`.
	await page.getByRole('button', { name: /^ticker/ }).click();
	await expect(activeSortHeader(page)).toHaveText('ticker ▲');

	await viewButton(page, 'terminal').click();
	await expect(activeSortHeader(page)).toHaveText('ticker ▲');
	expect(await rowLinks(page).allTextContents()).toEqual(['FASTA', 'FASTB', 'TSTOPA', 'TSTOPB']);
});
