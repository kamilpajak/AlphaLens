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

/**
 * The day index this file serves, newest first — the order the page assumes
 * when it derives its prev/next links. It is NOT `fixtures/api-mock/days.json`
 * (which lists three other dates), because one test has to walk from a date
 * with a drawer to a date without one and back, and that walk must be a
 * CLIENT-SIDE navigation: `page.goto` remounts the component and would hide
 * the very state the walk is about.
 *
 * Every row is derived from the day payload it points at, never typed by hand.
 */
function indexRow(date: string, body: Record<string, unknown>) {
	return {
		date,
		n_candidates: (body.candidates as unknown[]).length,
		n_themes: body.n_themes,
		top_theme: body.top_theme
	};
}

/**
 * The 29% SHAPE, composed here from TWO REAL CAPTURES rather than stored as a
 * day fixture.
 *
 * No day fixture in the repo carries `market_ret5_*`
 * (`grep -l market_ret5 tests/fixtures/api-mock/days/*.json` finds none), and
 * the production API is behind Cloudflare Access, so a genuine
 * `/api/v1/days/2026-05-19` capture is not available here. Both halves below
 * are real, and the composition is done in the test — on purpose — so that no
 * file in `fixtures/` pretends to be a capture it is not:
 *
 *   - candidate rows: `days/2026-05-18.json`, a real pre-signal day whose only
 *     `market*` column is `market_cap`, i.e. the API's own shape before the
 *     classifier shipped;
 *   - the three returns: `market-context/2026-05-19.json`, full precision as
 *     produced by the production `five_session_index_returns` (provenance is in
 *     that file).
 *
 * The index-level payload is identical on every candidate of a date, and the
 * page reads it off `candidates[0]`, so only that row is stamped.
 */
const PRE_SIGNAL_DAY = JSON.parse(
	readFileSync(`${FIXTURES_DIR}/days/2026-05-18.json`, 'utf-8')
);
const RET5 = JSON.parse(readFileSync(`${FIXTURES_DIR}/market-context/2026-05-19.json`, 'utf-8'));
const RETURNS_ONLY_DATE = '2026-05-19';
const RETURNS_ONLY_DAY = {
	...PRE_SIGNAL_DAY,
	date: RETURNS_ONLY_DATE,
	candidates: PRE_SIGNAL_DAY.candidates.map((c: Record<string, unknown>, i: number) =>
		i === 0
			? {
					...c,
					market_ret5_spy: RET5.market_ret5_spy,
					market_ret5_qqq: RET5.market_ret5_qqq,
					market_ret5_iwm: RET5.market_ret5_iwm
				}
			: c
	)
};
const RETURNS_ONLY_BODY = JSON.stringify(RETURNS_ONLY_DAY);

const LABELLED_DATE = '2026-09-05';
const EMPTY_DATE = '2026-04-14';
const LABELLED_DAY = JSON.parse(readFileSync(`${FIXTURES_DIR}/days/${LABELLED_DATE}.json`, 'utf-8'));
const EMPTY_DAY = JSON.parse(readFileSync(`${FIXTURES_DIR}/days/${EMPTY_DATE}.json`, 'utf-8'));
const DAYS_INDEX = [
	indexRow(LABELLED_DATE, LABELLED_DAY),
	indexRow(RETURNS_ONLY_DATE, RETURNS_ONLY_DAY),
	indexRow(EMPTY_DATE, EMPTY_DAY)
];
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
		if (day && day[1] === RETURNS_ONLY_DATE) {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: RETURNS_ONLY_BODY
			});
		}
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
		await page.goto(`/brief/${LABELLED_DATE}`);

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
		await page.goto(`/brief/${EMPTY_DATE}`);

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

	// The shape this change exists for, and the one the owner opened: 47 of the
	// 164 published dates (29%) publish the three returns with no market_state
	// at all. Asserted POSITIVELY — that the three values are on screen — because
	// the other two tests can only ever assert this row absent, so an `{#each}`
	// rendering the wrong field would leave them green.
	test('a date with returns but NO label shows all three, and nothing else', async ({ page }) => {
		await installMockedApi(page);
		await page.goto(`/brief/${RETURNS_ONLY_DATE}`);

		const s = strip(page);
		const returns = s.locator('dl[aria-label="index 5-session returns"]');
		await expect(returns).toBeVisible();
		await expect(returns.locator('dd')).toHaveCount(3);
		// The real trio for 2026-05-19, which is what the owner had on screen.
		await expect(returns.locator('dd')).toHaveText(['-0.60%', '-0.81%', '-3.39%']);
		await expect(returns.getByText('spy 5d', { exact: false })).toBeVisible();
		await expect(returns.getByText('qqq 5d', { exact: false })).toBeVisible();
		await expect(returns.getByText('iwm 5d', { exact: false })).toBeVisible();

		// ...while the whole market_state family stays absent: no driver row, no
		// toggle, and the label is the honest `unknown`.
		await expect(s.locator('dl[aria-label="market state drivers"]')).toHaveCount(0);
		await expect(s.getByRole('button')).toHaveCount(0);
		await expect(s.getByText('market · unknown', { exact: false })).toBeVisible();
	});

});
