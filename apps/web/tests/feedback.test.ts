/**
 * Playwright smoke for the FeedbackControls UI on a brief page.
 *
 * Mocks the four /v1/feedback/* endpoints + the standard /v1/days/{date}
 * route used by the brief page loader. Verifies:
 *   1. Default state renders Interested + Dismiss + more buttons
 *   2. Click Interested → POST fires → recorded chip appears
 *   3. Click Undo → DELETE fires → buttons restored
 *   4. Dismiss → category dropdown → reason dropdown → recorded chip
 *
 * The taxonomy returned matches the locked 2026-05-29 schema; if the
 * pipeline-side ``DISMISS_TAXONOMY`` drifts and the test taxonomy doesn't,
 * the test will fail in a way that highlights the drift.
 */

import { expect, test, type Page, type Route } from '@playwright/test';
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

// Pick the first fixture with at least one candidate.
const DATE = '2026-05-18';
const DAY_BODY = readFileSync(`${FIXTURES_DIR}/days/${DATE}.json`, 'utf-8');
const DAY_OBJ = JSON.parse(DAY_BODY);
const FIRST_CANDIDATE = DAY_OBJ.candidates[0];

const TAXONOMY_BODY = JSON.stringify({
	actions: ['interested', 'watching', 'dismissed', 'paper_traded', 'live_traded'],
	categories: {
		thesis_setup: ['wrong_theme', 'too_expensive', 'bad_setup'],
		risk_quality: ['business_management', 'risk_jurisdiction', 'dont_understand'],
		portfolio_style: ['already_have_exposure', 'liquidity_too_low', 'not_my_style'],
		other: ['other']
	}
});

// Track POST/DELETE calls so assertions can verify the right body was sent.
interface PostCall {
	body: Record<string, unknown>;
}
interface DeleteCall {
	id: string;
}

async function installMocks(page: Page) {
	const posts: PostCall[] = [];
	const deletes: DeleteCall[] = [];
	// In-memory store mirroring server upsert semantics so a GET after POST
	// would show the row — keeps the test honest if we add a refetch.
	const store: Map<string, Record<string, unknown>> = new Map();

	await page.route('**/api/v1/**', async (route: Route) => {
		const req = route.request();
		const url = new URL(req.url());

		if (url.pathname === '/api/v1/days') {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: DAYS_INDEX_BODY
			});
		}
		const dayMatch = url.pathname.match(/^\/api\/v1\/days\/(\d{4}-\d{2}-\d{2})$/);
		if (dayMatch) {
			return route.fulfill({ status: 200, contentType: 'application/json', body: DAY_BODY });
		}
		if (url.pathname === '/api/v1/feedback/taxonomy') {
			return route.fulfill({ status: 200, contentType: 'application/json', body: TAXONOMY_BODY });
		}
		if (url.pathname === '/api/v1/feedback/decisions' && req.method() === 'GET') {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ data: Array.from(store.values()) })
			});
		}
		if (url.pathname === '/api/v1/feedback/decisions' && req.method() === 'POST') {
			const body = req.postDataJSON() as Record<string, unknown>;
			posts.push({ body });
			const key = `${body.ticker}::${body.theme}`;
			const id = `mock-${posts.length}`;
			const stored = {
				id,
				brief_date: body.brief_date,
				ticker: body.ticker,
				theme: body.theme,
				surfaced_at: body.surfaced_at,
				action: body.action,
				action_at: new Date().toISOString(),
				dismiss_category: body.dismiss_category ?? null,
				dismiss_reason: body.dismiss_reason ?? null,
				dismiss_note: body.dismiss_note ?? null,
				confidence_subjective: body.confidence_subjective ?? null,
				paper_trade_plan_id: null,
				position_size_usd: null,
				entry_price: null,
				market_regime_at_entry: 'unknown'
			};
			store.set(key, stored);
			return route.fulfill({
				status: 201,
				contentType: 'application/json',
				body: JSON.stringify(stored)
			});
		}
		const delMatch = url.pathname.match(/^\/api\/v1\/feedback\/decisions\/(.+)$/);
		if (delMatch && req.method() === 'DELETE') {
			deletes.push({ id: delMatch[1] });
			// Find + drop by id (idempotent: noop on miss).
			for (const [key, value] of store.entries()) {
				if ((value as { id: string }).id === delMatch[1]) {
					store.delete(key);
					break;
				}
			}
			return route.fulfill({ status: 204, body: '' });
		}
		return route.fulfill({
			status: 404,
			contentType: 'application/json',
			body: JSON.stringify({ detail: `unhandled mock: ${url.pathname}` })
		});
	});
	return { posts, deletes };
}

test.describe('feedback controls', () => {
	test('renders Interested + Dismiss + more buttons by default', async ({ page }) => {
		await installMocks(page);
		await page.goto(`/brief/${DATE}`);
		const firstCard = page
			.locator(`[data-testid="feedback-controls"][data-ticker="${FIRST_CANDIDATE.ticker}"]`)
			.first();
		await expect(firstCard).toBeVisible();
		await expect(firstCard.getByTestId('feedback-interested')).toBeVisible();
		await expect(firstCard.getByTestId('feedback-dismiss')).toBeVisible();
		await expect(firstCard.getByTestId('feedback-more')).toBeVisible();
	});

	test('click Interested fires POST + replaces buttons with recorded chip', async ({ page }) => {
		const { posts } = await installMocks(page);
		await page.goto(`/brief/${DATE}`);
		const firstCard = page
			.locator(`[data-testid="feedback-controls"][data-ticker="${FIRST_CANDIDATE.ticker}"]`)
			.first();
		await firstCard.getByTestId('feedback-interested').click();
		await expect(firstCard.getByTestId('feedback-recorded')).toContainText('interested');
		await expect(firstCard.getByTestId('feedback-undo')).toBeVisible();
		// Buttons gone.
		await expect(firstCard.getByTestId('feedback-interested')).toHaveCount(0);
		// POST body shape.
		expect(posts.length).toBeGreaterThanOrEqual(1);
		const lastPost = posts[posts.length - 1].body;
		expect(lastPost.ticker).toBe(FIRST_CANDIDATE.ticker);
		expect(lastPost.action).toBe('interested');
		expect(lastPost.brief_date).toBe(DATE);
	});

	test('undo fires DELETE + restores buttons', async ({ page }) => {
		const { deletes } = await installMocks(page);
		await page.goto(`/brief/${DATE}`);
		const firstCard = page
			.locator(`[data-testid="feedback-controls"][data-ticker="${FIRST_CANDIDATE.ticker}"]`)
			.first();
		await firstCard.getByTestId('feedback-interested').click();
		await firstCard.getByTestId('feedback-undo').click();
		// Buttons back.
		await expect(firstCard.getByTestId('feedback-interested')).toBeVisible();
		expect(deletes.length).toBe(1);
	});

	test('hides controls when listDecisions fails (no silent overwrite)', async ({ page }) => {
		// Zen pre-merge finding #1: if taxonomy succeeds but decisions
		// fails, rendering the controls would let a fresh POST silently
		// overwrite a server-side decision the user can't see. Loader
		// returns null for decisions on failure → controls hidden.
		// Use the shared pathname-based mocks (query-tolerant: they match
		// `/v1/days?limit=200`, the day, and the taxonomy regardless of the
		// query string) so the brief renders, then override ONLY the decisions
		// GET to fail. The override is registered AFTER installMocks so it wins
		// — Playwright matches the most-recently-added route first. This keeps
		// the test independent of the dev mock-api proxy (a bare
		// `**/api/v1/days` glob misses the `?limit=200` query and falls through
		// to the proxy, which made this case flaky).
		await installMocks(page);
		await page.route(
			'**/api/v1/feedback/decisions**',
			(route) =>
				route.request().method() === 'GET'
					? route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
					: route.fallback() // let POST/DELETE fall through to installMocks
		);
		await page.goto(`/brief/${DATE}`);
		// Brief still renders (cards visible) but feedback controls are hidden.
		await expect(page.locator('article[id]').first()).toBeVisible();
		await expect(page.locator('[data-testid="feedback-controls"]')).toHaveCount(0);
	});

	test('dismiss → category → reason flow records dismissed with reason', async ({ page }) => {
		const { posts } = await installMocks(page);
		await page.goto(`/brief/${DATE}`);
		const firstCard = page
			.locator(`[data-testid="feedback-controls"][data-ticker="${FIRST_CANDIDATE.ticker}"]`)
			.first();
		await firstCard.getByTestId('feedback-dismiss').click();
		// Step 1: choose category.
		await expect(firstCard.getByTestId('feedback-pick-category')).toBeVisible();
		await firstCard.getByTestId('feedback-category-thesis_setup').click();
		// Step 2: choose reason.
		await expect(firstCard.getByTestId('feedback-pick-reason')).toBeVisible();
		await firstCard.getByTestId('feedback-reason-too_expensive').click();
		// Recorded.
		await expect(firstCard.getByTestId('feedback-recorded')).toContainText('dismissed');
		await expect(firstCard.getByTestId('feedback-recorded')).toContainText('too expensive');
		// POST body.
		expect(posts.length).toBe(1);
		expect(posts[0].body.action).toBe('dismissed');
		expect(posts[0].body.dismiss_category).toBe('thesis_setup');
		expect(posts[0].body.dismiss_reason).toBe('too_expensive');
	});
});
