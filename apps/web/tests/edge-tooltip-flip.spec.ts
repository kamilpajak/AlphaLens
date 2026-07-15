import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// Regression for "tooltips clipped by the /edge outcomes table": the table body
// is an `overflow-auto` scroll box, which clips descendants regardless of
// z-index. A classification tooltip on a row near the TOP of the scroll box used
// to open upward and be cut off by the box edge / sticky header. clampToViewport
// now auto-flips it to open downward so it stays inside the box.

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES = resolve(__dirname, 'fixtures/api-mock');
const SUMMARY = JSON.parse(readFileSync(resolve(FIXTURES, 'edge-summary.json'), 'utf-8'));
const OUTCOMES = JSON.parse(readFileSync(resolve(FIXTURES, 'edge-outcomes.json'), 'utf-8'));

async function stub(page: Page) {
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
	await page.route('**/v1/edge/excess-telemetry**', (r) =>
		r.fulfill({ status: 404, json: { detail: 'nf' } })
	);
}

test('a classification tooltip at the top of the scroll box flips below and stays inside it', async ({
	page
}) => {
	await stub(page);
	await page.goto('/edge');
	await expect(page.getByTestId('outcomes-table')).toBeVisible();

	// Put a classification chip right under the top of the scroll box, then focus
	// it and let clampToViewport's rAF measure run.
	await page.evaluate(() => {
		const box = document.querySelector('[data-testid="outcomes-scroll"]')! as HTMLElement;
		box.scrollTop = 40;
		const br = box.getBoundingClientRect();
		const chip =
			[...box.querySelectorAll('[data-testid="chip-tip"]')].find(
				(c) => c.getBoundingClientRect().top - br.top < 80
			) ?? box.querySelector('[data-testid="chip-tip"]')!;
		chip.setAttribute('data-probe', '1');
		(chip as HTMLElement).focus();
	});
	await page.waitForFunction(
		() => document.querySelector('[data-probe="1"] [role="tooltip"]')?.getAttribute('data-tt-flip') === 'below'
	);

	const geom = await page.evaluate(() => {
		const box = document.querySelector('[data-testid="outcomes-scroll"]')!.getBoundingClientRect();
		const bubble = document
			.querySelector('[data-probe="1"] [role="tooltip"]')!
			.getBoundingClientRect();
		return {
			flipTop: bubble.top >= box.top - 1,
			flipBottom: bubble.bottom <= box.bottom + 1
		};
	});
	// The flipped-below bubble sits fully within the scroll box (not clipped).
	expect(geom.flipTop).toBe(true);
	expect(geom.flipBottom).toBe(true);
});

test('a tooltip in a scroll box narrower than the bubble is width-capped and stays inside horizontally', async ({
	page
}) => {
	await stub(page);
	await page.goto('/edge');
	await expect(page.getByTestId('outcomes-table')).toBeVisible();

	// Force the scroll box narrower than the default 20rem bubble so the width-cap
	// + horizontal clamp must engage (mimics the width-capped / centered page where
	// the box's right edge sits inside the viewport).
	await page.addStyleTag({ content: '[data-testid="outcomes-scroll"]{max-width:300px !important;}' });
	await page.evaluate(() => {
		const box = document.querySelector('[data-testid="outcomes-scroll"]')! as HTMLElement;
		const chip = box.querySelector('[data-testid="chip-tip"]')!;
		chip.setAttribute('data-probe', '1');
		(chip as HTMLElement).focus();
	});
	await page.waitForFunction(() => {
		const bub = document.querySelector('[data-probe="1"] [role="tooltip"] > span');
		return bub ? bub.getBoundingClientRect().width < 320 : false;
	});

	const geom = await page.evaluate(() => {
		const box = document.querySelector('[data-testid="outcomes-scroll"]')!.getBoundingClientRect();
		const bub = document
			.querySelector('[data-probe="1"] [role="tooltip"] > span')!
			.getBoundingClientRect();
		return {
			capped: bub.width < 320,
			insideLeft: bub.left >= box.left - 1,
			insideRight: bub.right <= box.right + 1
		};
	});
	expect(geom.capped).toBe(true);
	expect(geom.insideLeft).toBe(true);
	expect(geom.insideRight).toBe(true);
});
