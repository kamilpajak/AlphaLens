import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// Regression for the "Buffett tooltip still reads transparent for SOME cards"
// report. The popover fill is opaque (bg-surface-pop), but the buffett meta-bar
// block dims itself with `opacity-60` when fundamentals coverage is thin
// (buffett_data_coverage < 0.5). CSS opacity applies to the WHOLE subtree and
// caps it — the popover lives inside that block, so its own opacity-100 can
// never exceed the ancestor's 0.6 and the card text bleeds through. This only
// hits low-coverage cards, which is why it looked like "only some candidates".
//
// tooltip-elevation.spec.ts cannot catch this: it reads the bubble's
// backgroundColor, whose alpha stays 1 regardless of an ancestor's `opacity`.
// Here we assert the EFFECTIVE opacity (product up the ancestor chain to the
// card) is 1, so no ancestor dims the popover.

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES_DIR = resolve(__dirname, 'fixtures/api-mock');
const DAYS_INDEX = JSON.parse(readFileSync(`${FIXTURES_DIR}/days.json`, 'utf-8'));
const DAYS_INDEX_BODY = JSON.stringify({
	data: DAYS_INDEX,
	meta: { total: DAYS_INDEX.length, limit: 200, offset: 0 }
});
const LOW_COV_DATE: string = DAYS_INDEX[0].date;

// Take the real fixture day and force the first candidate's Buffett block into
// the low-coverage branch (2/6 ⇒ 0.33 < 0.5 ⇒ buffLowCov), reproducing the
// exact card state from the bug report.
const RAW_DAY = JSON.parse(readFileSync(`${FIXTURES_DIR}/days/${LOW_COV_DATE}.json`, 'utf-8'));
const FIRST = RAW_DAY.candidates[0];
FIRST.expert_assessments = FIRST.expert_assessments ?? {};
FIRST.expert_assessments.buffett = {
	...(FIRST.expert_assessments.buffett ?? {}),
	buffett_quality_score: null,
	buffett_data_coverage: 2 / 6
};
const LOW_COV_DAY_BODY = JSON.stringify(RAW_DAY);

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
		if (url.pathname === '/api/v1/market/status')
			return route.fulfill({ status: 200, contentType: 'application/json', body: MARKET_STATUS_OPEN_BODY });
		if (url.pathname === '/api/v1/days')
			return route.fulfill({ status: 200, contentType: 'application/json', body: DAYS_INDEX_BODY });
		if (url.pathname === `/api/v1/days/${LOW_COV_DATE}`)
			return route.fulfill({ status: 200, contentType: 'application/json', body: LOW_COV_DAY_BODY });
		return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
	});
}

test('buffett popover stays opaque on thin-coverage cards (no ancestor dims it)', async ({ page }) => {
	await installApiMock(page);
	await page.goto(`/brief/${LOW_COV_DATE}`);

	const card = page.locator('article[id]').first();
	await expect(card).toBeVisible();

	const chip = page.locator('[data-testid="chip-tip"][data-term="buffett quality"]').first();
	await expect(chip).toBeVisible();
	await chip.focus();

	// Multiply computed `opacity` of every ANCESTOR of the popover, from its
	// parent up to (but excluding) the card. We start above the tooltip span so
	// the assertion is independent of the bubble's own opacity-0→100 fade-in
	// transition; the card's own `fade-up` opacity is excluded for the same
	// reason. The bug lives strictly in an ancestor between the popover and the
	// card (the buffLowCov `opacity-60` block), so any product < 1 here means
	// the popover is being dimmed and will read transparent over card text.
	const ancestorOpacity = await chip.evaluate((wrapper) => {
		const tip = wrapper.querySelector('[role="tooltip"]') as HTMLElement | null;
		const card = wrapper.closest('article');
		if (!tip || !card) return null;
		let el: HTMLElement | null = tip.parentElement;
		let product = 1;
		while (el && el !== card) {
			const o = parseFloat(getComputedStyle(el).opacity);
			if (!Number.isNaN(o)) product *= o;
			el = el.parentElement;
		}
		return product;
	});

	expect(ancestorOpacity).toBe(1);
});
