/**
 * Hermetic gate on the market-context strip's DISAPPEARING-CELL rule.
 *
 * The owner's rule: a cell with no value is not rendered at all — label, value
 * and flip point go together. Before this, an absent driver printed an em dash
 * with its threshold beside it ("VS MA200 — (bear below 0%)"), which is a
 * threshold for a number that is not there. On 47 of the 164 published brief
 * dates — every date before 2026-07-05, when `market_state` shipped — five of
 * the strip's eight cells were empty.
 *
 * WHY THIS FILE EXISTS AND THE UNIT TESTS DO NOT COVER IT. The cell builders in
 * `src/lib/components/marketContextCells.ts` are pinned by 17 unit tests, but
 * those test the MODEL. The user-visible half — that an empty row and the
 * toggle are not RENDERED — lives in the template, and the repo has no
 * component-mounting harness (no @testing-library/svelte, no jsdom). Playwright
 * already renders `/brief/<date>` against these fixtures, so the DOM assertion
 * belongs here. Without it, re-adding an unfiltered `{#each}` would leave every
 * unit test green.
 *
 * The two fixtures are the two sides of the rule:
 *   2026-09-05 — label + drivers present (and `squeeze_on: false`, which is a
 *                real reading and must NOT be treated as absent).
 *   2026-04-14 — nothing present: no label, no drivers, no returns.
 */

import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES_DIR = resolve(__dirname, 'fixtures/api-mock');

const DAYS_INDEX = JSON.parse(readFileSync(`${FIXTURES_DIR}/days.json`, 'utf-8'));
const DAYS_INDEX_BODY = JSON.stringify({
	data: DAYS_INDEX,
	meta: { total: DAYS_INDEX.length, limit: 200, offset: 0 }
});

function installMockedApi(page: Page) {
	return page.route('**/api/v1/**', (route) => {
		const url = new URL(route.request().url());
		if (url.pathname === '/api/v1/days') {
			return route.fulfill({ status: 200, contentType: 'application/json', body: DAYS_INDEX_BODY });
		}
		const day = url.pathname.match(/^\/api\/v1\/days\/(\d{4}-\d{2}-\d{2})$/);
		if (day) {
			try {
				return route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: readFileSync(`${FIXTURES_DIR}/days/${day[1]}.json`, 'utf-8')
				});
			} catch {
				return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
			}
		}
		return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
	});
}

const strip = (page: Page) => page.getByRole('region', { name: 'market context' });

test.describe('market-context strip — a cell with no value is not rendered', () => {
	test('a date WITH drivers shows them, each beside its flip point', async ({ page }) => {
		await installMockedApi(page);
		await page.goto('/brief/2026-09-05');

		// Scope every assertion to the driver LIST. The strip also hosts the
		// glossary tooltip, whose body legitimately contains the words "bear
		// below" and several em dashes — a free-text locator matches those and
		// says nothing about the cells.
		const drivers = strip(page).locator('dl[aria-label="market state drivers"]');
		await expect(drivers).toBeVisible();
		await expect(drivers.getByText('vs ma200', { exact: false })).toBeVisible();
		// The flip point is the point: it is what makes the number legible
		// without opening a tooltip, so it must survive wherever a value does.
		await expect(drivers.getByText('(bear below 0%)', { exact: false }).first()).toBeVisible();
	});

	test('a date with NOTHING renders no driver row, no returns row and no toggle', async ({
		page
	}) => {
		await installMockedApi(page);
		await page.goto('/brief/2026-04-14');

		const s = strip(page);
		await expect(s).toBeVisible();

		// The two <dl> wrappers are gone, not emptied: an aria-labelled list with
		// no children is announced as an empty labelled list, which is worse than
		// the em dash it replaces.
		await expect(s.locator('dl[aria-label="market state drivers"]')).toHaveCount(0);
		await expect(s.locator('dl[aria-label="index 5-session returns"]')).toHaveCount(0);

		// No cell survives anywhere in the strip. Asserted on <dd>, not on free
		// text: the glossary tooltip inside this same region carries em dashes
		// and the phrase "bear below" in its prose, so a text locator would
		// match the explanation rather than the data and could never fail.
		await expect(s.locator('dl[aria-label] dd')).toHaveCount(0);

		// An expander onto nothing is worse than no expander.
		await expect(s.getByRole('button')).toHaveCount(0);

		// What MUST survive: the label (a real answer — "no regime word for this
		// date") and the sentence that stops the strip reading as a signal.
		await expect(s.getByText('market · unknown', { exact: false })).toBeVisible();
		await expect(s.getByText('label unvalidated · context-only', { exact: false })).toBeVisible();
	});
});
