/**
 * Every animation the SPA runs stops under `prefers-reduced-motion: reduce`.
 *
 * Two animations survive in `app.css`: `fade-up`, a one-shot 0.45s entrance
 * with an 8px slide used on most page sections, and `blink`, an infinite 1.2s
 * opacity toggle on the MarketSession open-state dot. Both are decorative —
 * the open state is also carried by a green dot, the word "live" and a
 * countdown — so both are switched off entirely rather than shortened.
 *
 * Each assertion is paired with the SAME read taken without the preference, so
 * a rule that stopped matching for an unrelated reason cannot pass as a
 * suppression. Without that control the suite would still be green if `app.css`
 * lost its animations altogether.
 *
 * The other half of this guard is NOT here. An `animation` shorthand written as
 * an inline style outranks the media query, and a rendered check only sees the
 * routes its fixtures populate — the one offender this work found sat on a home
 * page section the fixture leaves empty, so a browser assertion for it passed
 * while the defect was present. That case is covered by a source scan instead:
 * `tests/unit/inlineAnimation.test.ts`.
 */

import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES_DIR = resolve(__dirname, 'fixtures/api-mock');

const DAYS_INDEX: { date: string }[] = JSON.parse(
	readFileSync(`${FIXTURES_DIR}/days.json`, 'utf-8')
);
const DAYS_INDEX_BODY = JSON.stringify({
	data: DAYS_INDEX,
	meta: { total: DAYS_INDEX.length, limit: 200, offset: 0 }
});
const LATEST_DATE = DAYS_INDEX[0].date;
const LATEST_DAY_BODY = readFileSync(`${FIXTURES_DIR}/days/${LATEST_DATE}.json`, 'utf-8');

// The venue must read OPEN: `blink` only exists on the open branch of the
// session chip, so a closed stub would make its assertion vacuous.
const MARKET_STATUS_OPEN_BODY = JSON.stringify({
	is_trading_day: true,
	is_half_day: false,
	is_open_now: true,
	next_open_iso: '2099-01-01T13:30:00+00:00',
	next_close_iso: new Date(Date.now() + 2 * 60 * 60 * 1000).toISOString(),
	exchange: 'XNYS'
});

function installApiMock(page: Page) {
	return page.route('**/api/v1/**', (route) => {
		const url = new URL(route.request().url());
		if (url.pathname === '/api/v1/market/status') {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: MARKET_STATUS_OPEN_BODY
			});
		}
		if (url.pathname === '/api/v1/days') {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: DAYS_INDEX_BODY
			});
		}
		if (url.pathname === `/api/v1/days/${LATEST_DATE}`) {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: LATEST_DAY_BODY
			});
		}
		return route.fulfill({
			status: 404,
			contentType: 'application/json',
			body: JSON.stringify({ detail: `unhandled api mock: ${url.pathname}` })
		});
	});
}

/** `animation-name` as the browser resolves it for the first `selector` match. */
async function animationName(page: Page, selector: string): Promise<string> {
	return page.evaluate((sel) => {
		const el = document.querySelector(sel);
		if (el === null) return '__NO_SUCH_ELEMENT__';
		return getComputedStyle(el).animationName;
	}, selector);
}

test.beforeEach(async ({ page }) => {
	await installApiMock(page);
});

test.describe('reduced motion honoured', () => {
	test.use({ reducedMotion: 'reduce' });

	test('the section entrance does not animate', async ({ page }) => {
		await page.goto('/');
		await expect(page.locator('.fade-up').first()).toBeAttached();
		expect(await animationName(page, '.fade-up')).toBe('none');
	});

	test('the open-venue dot does not pulse', async ({ page }) => {
		await page.goto('/');
		await expect(page.locator('.blink')).toHaveCount(1);
		expect(await animationName(page, '.blink')).toBe('none');
	});

});

test.describe('without the preference the same animations run', () => {
	test.use({ reducedMotion: 'no-preference' });

	test('the section entrance animates', async ({ page }) => {
		await page.goto('/');
		await expect(page.locator('.fade-up').first()).toBeAttached();
		expect(await animationName(page, '.fade-up')).toBe('fade-up');
	});

	test('the open-venue dot pulses', async ({ page }) => {
		await page.goto('/');
		await expect(page.locator('.blink')).toHaveCount(1);
		expect(await animationName(page, '.blink')).toBe('blink');
	});
});
