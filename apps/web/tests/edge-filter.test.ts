import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES = resolve(__dirname, 'fixtures/api-mock');
const SUMMARY = JSON.parse(readFileSync(resolve(FIXTURES, 'edge-summary.json'), 'utf-8'));

// The /edge outcomes toolbar: free-text search over ticker+theme, faceted
// multi-select on ladder-classification + scorer cohort, an "N of M" count, a
// clear-all, and URL round-trip so a filtered view is deep-linkable.

// 6 terminal rows: 3 classes, 2 cohorts, 2 themes — enough for every facet.
// `facets` mirror those same 6 rows (no invented numbers); with ongoing: 0 the
// ongoing view toggle legitimately reads [0].
const OUTCOMES = {
	data: [
		mk('NVDA', 'ai-infra', 'TP_FULL', 'v1'),
		mk('AMD', 'ai-infra', 'SL_HIT', 'v1'),
		mk('SNAP', 'ai-infra', 'SL_HIT', 'v2'),
		mk('PLUG', 'high-gas', 'TIME_STOP', 'v1'),
		mk('AMPL', 'high-gas', 'TP_FULL', 'v2'),
		mk('BE', 'high-gas', 'SL_HIT', 'v2')
	],
	total: 6,
	returned: 6,
	truncated: false,
	facets: {
		status: { terminal: 6, ongoing: 0 },
		classification: { terminal: { SL_HIT: 3, TP_FULL: 2, TIME_STOP: 1 }, ongoing: {} }
	}
};

function mk(ticker: string, theme: string, cls: string, cohort: string) {
	return {
		ticker,
		brief_date: '2026-05-18',
		matured_at: '2026-05-29',
		theme,
		ladder_classification: cls,
		terminal: true,
		realized_r: 1.1,
		open_r: null,
		market_excess_return: 0.1,
		forward_return: 0.05,
		benchmark_window_return: 0.02,
		holding_days_elapsed: 10,
		realized_return_pct_of_book: 0.15,
		scorer_config_version: cohort
	};
}

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

test('free-text search narrows the table and updates the count + URL', async ({ page }) => {
	await stub(page);
	await page.goto('/edge');
	await expect(page.getByTestId('outcomes-filter')).toBeVisible();
	await expect(rowLinks(page)).toHaveCount(6);

	await page.getByTestId('outcomes-search').fill('nvda');
	await expect(rowLinks(page)).toHaveCount(1);
	await expect(rowLinks(page).first()).toHaveText('NVDA');
	// The denominator is the SERVER-truth window population ("in window"), not
	// the fetched row count — honest under the cap.
	await expect(page.getByTestId('outcomes-match-count')).toContainText('1 shown of 6 in window');
	await expect.poll(() => new URL(page.url()).searchParams.get('q')).toBe('nvda');

	// Theme substring hits every high-gas row.
	await page.getByTestId('outcomes-search').fill('high-gas');
	await expect(rowLinks(page)).toHaveCount(3);
});

test('a classification facet chip filters, and clear-all resets', async ({ page }) => {
	await stub(page);
	await page.goto('/edge');
	await expect(rowLinks(page)).toHaveCount(6);

	await page.getByTestId('outcomes-filter').getByRole('button', { name: /^SL_HIT/ }).click();
	await expect(rowLinks(page)).toHaveCount(3); // AMD, SNAP, BE
	await expect.poll(() => new URL(page.url()).searchParams.get('class')).toBe('SL_HIT');
	// Under a single selected chip, the visible table equals that chip's window
	// count (3), and the counter's denominator is the selected class's count.
	await expect(page.getByTestId('outcomes-match-count')).toContainText('3 shown of 3 in window');

	await page.getByTestId('outcomes-clear-all').click();
	await expect(rowLinks(page)).toHaveCount(6);
	await expect.poll(() => new URL(page.url()).searchParams.has('class')).toBe(false);
});

test('URL sync preserves an unrelated query param', async ({ page }) => {
	await stub(page);
	await page.goto('/edge?ref=email');
	await expect(page.getByTestId('outcomes-filter')).toBeVisible();

	await page.getByTestId('outcomes-search').fill('nvda');
	await expect.poll(() => new URL(page.url()).searchParams.get('q')).toBe('nvda');
	// The unrelated param must survive the filter's replaceState.
	expect(new URL(page.url()).searchParams.get('ref')).toBe('email');
});

test('deep-links: a ?q= URL arrives pre-filtered', async ({ page }) => {
	await stub(page);
	await page.goto('/edge?q=snap');
	await expect(page.getByTestId('outcomes-filter')).toBeVisible();
	await expect(rowLinks(page)).toHaveCount(1);
	await expect(rowLinks(page).first()).toHaveText('SNAP');
	await expect(page.getByTestId('outcomes-search')).toHaveValue('snap');
});

test('terminal class chips sum exactly to the ALL chip (per-view server facets)', async ({
	page
}) => {
	await stub(page);
	await page.goto('/edge');
	const bar = page.getByTestId('outcomes-filter');
	// The server now groups facets.classification by the actual per-row terminal
	// flag, so the terminal view's chips (3 + 2 + 1) sum EXACTLY to the ALL chip
	// / view-toggle population (6) — the "516 vs 505" checksum failure dies.
	await expect(bar.getByRole('button', { name: 'SL_HIT 3' })).toBeVisible();
	await expect(bar.getByRole('button', { name: 'TP_FULL 2' })).toBeVisible();
	await expect(bar.getByRole('button', { name: 'TIME_STOP 1' })).toBeVisible();
	await expect(bar.getByRole('button', { name: 'all 6' }).first()).toBeVisible();
	// No class from the other view leaks into this view's chip row.
	await expect(bar.getByRole('button', { name: /^OPEN/ })).toHaveCount(0);
});

// ── Server-side classification refetch (issue #1055) ────────────────────────
// Selecting a classification chip refetches /v1/edge/outcomes with the
// classification= param so rows the (capped) initial listing dropped become
// reachable; the chip counts read the server facets, so sibling chips survive
// the narrowed row set.

const classificationParam = (url: string) =>
	new URL(url).searchParams.get('classification');

test('selecting a class chip refetches server-side and keeps sibling chips', async ({ page }) => {
	await stub(page);
	const requested: (string | null)[] = [];
	// Later registrations win in Playwright, so this overrides stub()'s route.
	await page.route('**/v1/edge/outcomes**', (route) => {
		const cls = classificationParam(route.request().url());
		requested.push(cls);
		if (cls === 'SL_HIT') {
			const data = OUTCOMES.data.filter((r) => r.ladder_classification === 'SL_HIT');
			return route.fulfill({
				json: { ...OUTCOMES, data, total: data.length, returned: data.length }
			});
		}
		return route.fulfill({ json: OUTCOMES });
	});

	await page.goto('/edge');
	await expect(rowLinks(page)).toHaveCount(6);
	// The initial load must stay window-only — no classification param.
	expect(requested).toEqual([null]);

	await page.getByTestId('outcomes-filter').getByRole('button', { name: /^SL_HIT/ }).click();
	await expect(rowLinks(page)).toHaveCount(3); // AMD, SNAP, BE — server-filtered
	await expect.poll(() => new URL(page.url()).searchParams.get('class')).toBe('SL_HIT');
	await expect.poll(() => requested).toContain('SL_HIT');
	// Sibling chips survive with their SERVER-FACET counts (the fetched rows no
	// longer contain any TP_FULL row).
	await expect(
		page.getByTestId('outcomes-filter').getByRole('button', { name: 'TP_FULL 2' })
	).toBeVisible();

	// Back to ALL: a refetch WITHOUT the param restores the full listing. (The
	// status bar renders before the cohort bar; both carry an "all" chip.)
	await page.getByTestId('outcomes-filter').getByRole('button', { name: 'all 6' }).first().click();
	await expect(rowLinks(page)).toHaveCount(6);
	expect(requested[requested.length - 1]).toBeNull();
});

test('a class the server dropped from the listing still renders its chip and becomes reachable', async ({
	page
}) => {
	await stub(page);
	await page.route('**/v1/edge/outcomes**', (route) => {
		const cls = classificationParam(route.request().url());
		if (cls === 'TIME_STOP') {
			const data = OUTCOMES.data.filter((r) => r.ladder_classification === 'TIME_STOP');
			return route.fulfill({
				json: { ...OUTCOMES, data, total: data.length, returned: data.length }
			});
		}
		// Initial (window-only) listing: the cap dropped the TIME_STOP row, but
		// the pre-filter facets still count it.
		const data = OUTCOMES.data.filter((r) => r.ladder_classification !== 'TIME_STOP');
		return route.fulfill({
			json: { ...OUTCOMES, data, total: 6, returned: data.length, truncated: true }
		});
	});

	await page.goto('/edge');
	await expect(rowLinks(page)).toHaveCount(5);
	// Defect (b)'s end-to-end pin: the chip renders from the server facets even
	// though no fetched row carries the class...
	const timeStopChip = page
		.getByTestId('outcomes-filter')
		.getByRole('button', { name: 'TIME_STOP 1' });
	await expect(timeStopChip).toBeVisible();

	// ...and selecting it refetches server-side, surfacing the dropped row.
	await timeStopChip.click();
	await expect(rowLinks(page)).toHaveCount(1);
	await expect(rowLinks(page).first()).toHaveText('PLUG');
});

test('race guard: rapid chip clicks settle on the LAST selection', async ({ page }) => {
	await stub(page);
	await page.route('**/v1/edge/outcomes**', async (route) => {
		const cls = classificationParam(route.request().url());
		if (cls === 'SL_HIT') {
			// Delay the FIRST selection's response past the second one's, so a
			// missing guard would let the stale row set clobber the newer one.
			await new Promise((r) => setTimeout(r, 300));
			const data = OUTCOMES.data.filter((r) => r.ladder_classification === 'SL_HIT');
			return route.fulfill({
				json: { ...OUTCOMES, data, total: data.length, returned: data.length }
			});
		}
		if (cls === 'SL_HIT,TIME_STOP') {
			const data = OUTCOMES.data.filter((r) =>
				['SL_HIT', 'TIME_STOP'].includes(r.ladder_classification)
			);
			return route.fulfill({
				json: { ...OUTCOMES, data, total: data.length, returned: data.length }
			});
		}
		return route.fulfill({ json: OUTCOMES });
	});

	await page.goto('/edge');
	await expect(rowLinks(page)).toHaveCount(6);

	const filterBar = page.getByTestId('outcomes-filter');
	await filterBar.getByRole('button', { name: /^SL_HIT/ }).click();
	await filterBar.getByRole('button', { name: /^TIME_STOP/ }).click();

	// The union selection {SL_HIT, TIME_STOP} wins: 4 rows...
	await expect(rowLinks(page)).toHaveCount(4);
	// ...and stays 4 after the delayed SL_HIT-only response would have landed.
	await page.waitForTimeout(400);
	await expect(rowLinks(page)).toHaveCount(4);
});

test('race guard: deselecting back to ALL mid-flight still restores the full listing', async ({
	page
}) => {
	// Regression pin: guarding on the last-APPLIED key let a return to a
	// previously-applied selection (here '' = ALL) early-return without a
	// corrective fetch, so the delayed filtered response landed unchallenged and
	// the table stuck on the filtered rows while the selection read ALL.
	await stub(page);
	await page.route('**/v1/edge/outcomes**', async (route) => {
		const cls = classificationParam(route.request().url());
		if (cls === 'SL_HIT') {
			// Delay the selection's response so the deselect happens mid-flight.
			await new Promise((r) => setTimeout(r, 300));
			const data = OUTCOMES.data.filter((r) => r.ladder_classification === 'SL_HIT');
			return route.fulfill({
				json: { ...OUTCOMES, data, total: data.length, returned: data.length }
			});
		}
		return route.fulfill({ json: OUTCOMES });
	});

	await page.goto('/edge');
	await expect(rowLinks(page)).toHaveCount(6);

	const slHitChip = page.getByTestId('outcomes-filter').getByRole('button', { name: /^SL_HIT/ });
	await slHitChip.click(); // select → delayed SL_HIT fetch in flight
	await slHitChip.click(); // deselect back to ALL before that response lands

	// After the delayed SL_HIT response would have landed, the FULL listing must
	// be displayed — the stale filtered rows may never stick under an ALL chip.
	await page.waitForTimeout(400);
	await expect(rowLinks(page)).toHaveCount(6);
	await expect.poll(() => new URL(page.url()).searchParams.has('class')).toBe(false);
});
