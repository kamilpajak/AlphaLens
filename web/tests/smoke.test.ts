import { expect, test, type ConsoleMessage, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { GLOSSARY } from '../src/lib/data/glossary.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES_DIR = resolve(__dirname, 'fixtures/api-mock');

/**
 * The app fetches /api/v1/* from the FastAPI service in production; the
 * preview server here has no live api container, so we intercept those
 * requests and serve bodies synthesised from a checked-in fixture set
 * under tests/fixtures/api-mock/. Fixtures are an explicit testing
 * artefact — not a production export — so they don't drift with daily
 * pipeline runs and the test stays fully hermetic.
 */
// Pre-read fixture files at module load — synchronous reads in the route
// handler add per-request latency that races SvelteKit's client-side load
// function against the Playwright test's first DOM query.
const DAYS_INDEX = JSON.parse(readFileSync(`${FIXTURES_DIR}/days.json`, 'utf-8'));
const DAYS_INDEX_BODY = JSON.stringify({
	data: DAYS_INDEX,
	meta: { total: DAYS_INDEX.length, limit: 200, offset: 0 }
});
const DAY_BODIES: Record<string, string> = {};
for (const day of DAYS_INDEX) {
	const path = `${FIXTURES_DIR}/days/${day.date}.json`;
	try {
		DAY_BODIES[day.date] = readFileSync(path, 'utf-8');
	} catch {
		/* missing — handler will surface 404 */
	}
}

function installApiMock(page: Page) {
	return page.route('**/api/v1/**', (route) => {
		const url = new URL(route.request().url());
		// /api/v1/days[?limit=…]
		if (url.pathname === '/api/v1/days') {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: DAYS_INDEX_BODY
			});
		}
		// /api/v1/days/{date}
		const dayMatch = url.pathname.match(/^\/api\/v1\/days\/(\d{4}-\d{2}-\d{2})$/);
		if (dayMatch) {
			const date = dayMatch[1];
			const body = DAY_BODIES[date];
			if (body) {
				return route.fulfill({ status: 200, contentType: 'application/json', body });
			}
			return route.fulfill({
				status: 404,
				contentType: 'application/json',
				body: JSON.stringify({ detail: `no brief for date=${date}` })
			});
		}
		return route.fulfill({
			status: 404,
			contentType: 'application/json',
			body: JSON.stringify({ detail: `unhandled api mock: ${url.pathname}` })
		});
	});
}

test.beforeEach(async ({ page }) => {
	await installApiMock(page);
});

/**
 * Smoke tests covering every route + interactive surface in the production build.
 *
 * What this catches:
 *   - SSR compile errors (e.g. invalid {@const} placement, missing imports)
 *   - 500 responses from broken prerender entries / load functions
 *   - 404s on routes that should prerender (every brief in days.json, /about,
 *     /, /briefs)
 *   - Client-side console errors / page errors / unhandled rejections
 *   - Missing static data JSON files
 *   - Broken internal links (anything `<a href="/...">` that 404s)
 *   - Major content regressions (header chip text, candidate count)
 *   - SPA navigation hydration failures between nav links
 *   - Filter / checkbox / details interaction errors on the brief detail page
 *   - Hover tooltip render failures on gate pills
 *
 * What it does NOT catch:
 *   - Visual regressions (Tailwind class drift, layout shifts)
 *   - Accessibility deficiencies
 *   - Data correctness vs source parquet
 */

const days: { date: string; n_candidates: number }[] = DAYS_INDEX;
const latestDay = days[0];

function attachErrorCollectors(consoleErrors: string[], pageErrors: string[]) {
	return {
		onConsole: (msg: ConsoleMessage) => {
			if (msg.type() === 'error') consoleErrors.push(msg.text());
		},
		onPageError: (err: Error) => {
			pageErrors.push(err.message);
		}
	};
}

test.describe('smoke — every route renders without errors', () => {
	for (const path of ['/', '/briefs', '/about', '/experiments']) {
		test(`GET ${path} renders OK`, async ({ page }) => {
			const consoleErrors: string[] = [];
			const pageErrors: string[] = [];
			const { onConsole, onPageError } = attachErrorCollectors(consoleErrors, pageErrors);
			page.on('console', onConsole);
			page.on('pageerror', onPageError);

			const response = await page.goto(path);
			expect(response?.status(), `GET ${path} should be 200`).toBe(200);

			await expect(page.locator('header a[href="/"]').first()).toContainText('ALPHALENS');

			expect(consoleErrors, `${path} console errors`).toEqual([]);
			expect(pageErrors, `${path} page errors`).toEqual([]);
		});
	}

	// Every brief in days.json must be prerendered and load cleanly.
	for (const day of days) {
		test(`GET /brief/${day.date} renders all candidates`, async ({ page }) => {
			const consoleErrors: string[] = [];
			const pageErrors: string[] = [];
			const { onConsole, onPageError } = attachErrorCollectors(consoleErrors, pageErrors);
			page.on('console', onConsole);
			page.on('pageerror', onPageError);

			const response = await page.goto(`/brief/${day.date}`);
			expect(response?.status()).toBe(200);

			await expect(page.getByText(day.date).first()).toBeVisible();

			const candidateCount = await page.locator('article[id]').count();
			expect(candidateCount, `${day.date} candidate count`).toBe(day.n_candidates);

			expect(consoleErrors, `${day.date} console errors`).toEqual([]);
			expect(pageErrors, `${day.date} page errors`).toEqual([]);
		});
	}

	// Under SPA mode the HTTP response is always 200 (nginx serves index.html
	// as fallback); the 404 surfaces as SvelteKit's error page rendering "404"
	// after the client-side router or load function rejects.
	test('GET /brief/2099-01-01 (unknown date) renders SvelteKit 404', async ({ page }) => {
		await page.goto('/brief/2099-01-01');
		await expect(page.getByText('404', { exact: false })).toBeVisible();
	});

	test('GET /unknown-route renders SvelteKit 404', async ({ page }) => {
		await page.goto('/this-route-does-not-exist');
		await expect(page.getByText('404', { exact: false })).toBeVisible();
	});
});

test.describe('smoke — SPA navigation', () => {
	test('clicking every header nav link transitions cleanly', async ({ page }) => {
		const consoleErrors: string[] = [];
		const pageErrors: string[] = [];
		const { onConsole, onPageError } = attachErrorCollectors(consoleErrors, pageErrors);
		page.on('console', onConsole);
		page.on('pageerror', onPageError);

		await page.goto('/');

		for (const path of ['/briefs', '/about', '/experiments', '/']) {
			await page.locator(`header a[href="${path}"]`).first().click();
			await expect(page).toHaveURL(new RegExp(path === '/' ? '/$' : path));
		}

		expect(consoleErrors).toEqual([]);
		expect(pageErrors).toEqual([]);
	});

	test('all internal links on every page resolve to 200/404 (no 5xx)', async ({ page, request }) => {
		const seen = new Set<string>();
		for (const path of ['/', '/briefs', '/about', '/experiments', `/brief/${latestDay.date}`]) {
			await page.goto(path);
			const hrefs = await page.locator('a[href^="/"]').evaluateAll((nodes) =>
				nodes
					// target="_blank" signals "different upstream" (e.g. /api/docs
					// served by the FastAPI process behind nginx in prod) — the
					// SvelteKit preview server doesn't proxy it, so excluding the
					// link keeps the test honest without 5xx false positives.
					.filter((n) => (n as HTMLAnchorElement).getAttribute('target') !== '_blank')
					.map((n) => (n as HTMLAnchorElement).getAttribute('href') ?? '')
					.filter((h) => !h.startsWith('//') && !h.startsWith('/#'))
			);
			for (const href of hrefs) seen.add(href.split('#')[0]);
		}
		for (const href of seen) {
			const res = await request.get(href);
			expect(res.status(), `link target ${href}`).toBeLessThan(500);
		}
	});
});

test.describe('smoke — brief detail interactions', () => {
	test('theme filter chips and verified-only checkbox toggle without errors', async ({ page }) => {
		const consoleErrors: string[] = [];
		const pageErrors: string[] = [];
		const { onConsole, onPageError } = attachErrorCollectors(consoleErrors, pageErrors);
		page.on('console', onConsole);
		page.on('pageerror', onPageError);

		await page.goto(`/brief/${latestDay.date}`);

		// Click each theme filter chip; assert the candidate list narrows or
		// the empty-state placeholder appears — guarantees Svelte's $derived
		// filtered-list reactivity ran before we move on.
		const chips = page.getByRole('button', { name: /^#/ });
		const count = await chips.count();
		for (let i = 0; i < count; i++) {
			await chips.nth(i).click();
			await expect(page.locator('article[id], .text-center')).not.toHaveCount(0);
		}
		await page.getByRole('button', { name: /^all \(/ }).click();
		await expect(page.locator('article[id]')).toHaveCount(latestDay.n_candidates);

		// Toggle verified-only — same reactivity guarantee.
		const cb = page.locator('input[type="checkbox"]').first();
		await cb.check();
		await expect(page.locator('article[id], .text-center')).not.toHaveCount(0);
		await cb.uncheck();
		await expect(page.locator('article[id]')).toHaveCount(latestDay.n_candidates);

		expect(consoleErrors).toEqual([]);
		expect(pageErrors).toEqual([]);
	});

	test('signal-bar tooltip renders on hover (CSS regression guard)', async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		// data-testid is stable across Tailwind class refactors.
		const firstBar = page.locator('article[id] [data-testid="signal-bar"]').first();
		await firstBar.hover();
		const tooltip = page.locator('article[id] [role="tooltip"]').first();
		await expect(tooltip).toBeVisible();
	});

	test('jargon-tip tooltip renders on hover (CSS regression guard)', async ({ page }) => {
		await page.goto('/experiments');
		// The "how.to.read" block contains the first inline JargonTip wrappers.
		// Hover the wrapper (.group) to trigger group-hover:opacity-100 on the
		// sibling tooltip. data-testid is stable across Tailwind class refactors.
		// The first αt JargonTip wraps a nested Carhart 4F JargonTip, so the
		// outer tip contains 2 tooltips — assert the OUTER one (direct child) is
		// visible on hover via the `>` direct-child selector.
		const firstTip = page.locator('[data-testid="jargon-tip"]').first();
		await firstTip.hover();
		const tooltip = firstTip.locator('> [role="tooltip"]');
		await expect(tooltip).toBeVisible();
	});

	test('gate-pill tooltip renders on hover (CSS regression guard)', async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		// Inner pill carries the status symbol. Hover its wrapper (.group) to
		// trigger group-hover:opacity-100 on the sibling tooltip.
		const firstPill = page
			.locator('article[id] .group:has(.cursor-help)')
			.first();
		await firstPill.hover();
		const tooltip = page.locator('article[id] [role="tooltip"]').first();
		// toBeVisible() enforces opacity > 0; this catches a regression of the
		// group-hover:opacity-100 transition as well as DOM presence.
		await expect(tooltip).toBeVisible();
	});

	test('expanding the full-markdown <details> works for every candidate', async ({ page }) => {
		await page.goto(`/brief/${latestDay.date}`);
		// Auto-wait until the client-side load function has rendered the
		// candidate cards. The pre-PR-3 test used a sync `.count()` and
		// happened to win the race because static-JSON fetches resolved
		// before the first DOM query; the api route mock has slightly more
		// latency so the race tips the other way without auto-wait.
		await expect(page.locator('article[id] details')).toHaveCount(latestDay.n_candidates);
		const detailsCount = await page.locator('article[id] details').count();
		expect(detailsCount).toBe(latestDay.n_candidates);

		// Toggle the first three to keep test bounded.
		const sample = Math.min(3, detailsCount);
		for (let i = 0; i < sample; i++) {
			const det = page.locator('article[id] details').nth(i);
			await det.locator('summary').click();
			await expect(det).toHaveAttribute('open', '');
		}
	});
});

test.describe('experiments — hybrid tooltip policy', () => {
	// Policy: short acronyms (αt, OOS, IS, FL, CAR, BAB, Q5) get a JargonTip
	// at EVERY occurrence in data text. Multi-word / longer terms (Carhart 4F,
	// Bonferroni, phase-aliasing, multi-phase audit, Phase A-E, R², R2000-PIT,
	// FCFF, PEAD, NW HAC, Form-4, ADR) get a tooltip on FIRST occurrence per
	// section. Each paradigm <article> and each pattern <li> is a section.
	// These assertions guard the policy from silent drift via text edits.

	test('αt always-tooltip — appears as JargonTip across the page', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("αt")').count();
		expect(count, 'αt JargonTip count across page').toBeGreaterThanOrEqual(20);
	});

	test('OOS — appears as JargonTip in patterns + each paradigm row mini-bar', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("OOS")').count();
		expect(count, 'OOS JargonTip count').toBeGreaterThanOrEqual(20);
	});

	test('IS — appears as JargonTip in patterns + each paradigm row mini-bar', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("IS")').count();
		expect(count, 'IS JargonTip count').toBeGreaterThanOrEqual(20);
	});

	test('FL — JargonTip exists at least in how.to.read + paradigm metrics', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("FL")').count();
		expect(count, 'FL JargonTip count').toBeGreaterThanOrEqual(2);
	});

	test('CAR — wrapped on every occurrence in P04 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#P04 [data-testid="jargon-tip"]:has-text("CAR")').count();
		expect(count, 'CAR JargonTip count in P04').toBe(4);
	});

	test('BAB — exists in P15 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#P15 [data-testid="jargon-tip"]:has-text("BAB")').count();
		expect(count, 'BAB JargonTip count in P15').toBeGreaterThanOrEqual(1);
	});

	test('Q5 — exists in S01 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#S01 [data-testid="jargon-tip"]:has-text("Q5")').count();
		expect(count, 'Q5 JargonTip count in S01').toBeGreaterThanOrEqual(1);
	});

	test('Bonferroni — first-per-section across patterns + paradigm rows', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("Bonferroni")').count();
		expect(count, 'Bonferroni JargonTip count').toBeGreaterThanOrEqual(2);
	});

	test('Carhart 4F — first-per-section in how.to.read + patterns', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("Carhart")').count();
		expect(count, 'Carhart 4F JargonTip count').toBeGreaterThanOrEqual(2);
	});

	test('PEAD — exists in P14 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#P14 [data-testid="jargon-tip"]:has-text("PEAD")').count();
		expect(count, 'PEAD JargonTip count in P14').toBeGreaterThanOrEqual(1);
	});

	test('NW HAC — exists in P14 row', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#P14 [data-testid="jargon-tip"]:has-text("NW HAC")').count();
		expect(count, 'NW HAC JargonTip count in P14').toBeGreaterThanOrEqual(1);
	});

	test('Form-4 — exists in S01 row (first-per-section)', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('article#S01 [data-testid="jargon-tip"]:has-text("Form-4")').count();
		expect(count, 'Form-4 JargonTip count in S01').toBeGreaterThanOrEqual(1);
	});

	test('ADR — exists in patterns section', async ({ page }) => {
		await page.goto('/experiments');
		const count = await page.locator('[data-testid="jargon-tip"]:has-text("ADR")').count();
		expect(count, 'ADR JargonTip count').toBeGreaterThanOrEqual(1);
	});

	test('glossary section has NO JargonTips (defines them; tooltips would be self-referential)', async ({ page }) => {
		await page.goto('/experiments');
		// The glossary section comes last on the page; scope by section title text.
		// Each glossary entry is a <dt>/<dd> pair inside the section. JargonTip
		// uses <span data-testid="jargon-tip">, none should appear under the
		// glossary section header.
		const glossarySection = page.locator('section').filter({ hasText: 'glossary.terms' });
		const count = await glossarySection.locator('[data-testid="jargon-tip"]').count();
		expect(count, 'glossary section JargonTip count must be 0').toBe(0);
	});

	test('paradigm detail fields collapsed by default (P0.1)', async ({ page }) => {
		await page.goto('/experiments');
		// Every paradigm row's <details> must start closed so the page is
		// scannable on initial render. If a future edit flips a default to
		// open, scroll depth balloons back to the pre-P0.1 level.
		const opened = await page.locator('article details[open]').count();
		expect(opened, 'no paradigm details should be open on initial load').toBe(0);
		// And every paradigm row must contain exactly one <details>.
		const articles = await page.locator('article').count();
		const details = await page.locator('article details').count();
		expect(details, 'one <details> per paradigm article').toBe(articles);
	});

	test('footer ticker switches vocabulary on /experiments vs other routes (P1.2)', async ({ page }) => {
		await page.goto('/');
		// Auto-wait until the layout's $derived ticker computation has
		// flushed and the chip spans are in the DOM with text-amber class.
		// Without this the sync `.allTextContents()` race-loses on warm
		// Vite preview runs that boot the preview server's hot-class scan
		// after the navigation completes.
		await expect(page.locator('footer span.text-amber').first()).toBeVisible();
		const dashChips = (await page.locator('footer span.text-amber').allTextContents()).join(' ');
		expect(dashChips, 'dashboard footer keeps thematic vocab').toContain('PRESS-GATE');
		expect(dashChips, 'dashboard footer does not show research vocab').not.toContain('DOCTRINE');

		await page.goto('/experiments');
		await expect(page.locator('footer span.text-amber').first()).toBeVisible();
		const expChips = (await page.locator('footer span.text-amber').allTextContents()).join(' ');
		expect(expChips, '/experiments footer shows research vocab').toContain('DOCTRINE');
		expect(expChips, '/experiments footer does not show thematic vocab').not.toContain('PRESS-GATE');
	});

	test('sticky TOC renders on xl viewport with 7 section anchors (P3.1)', async ({ page }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.goto('/experiments');
		// Aside rail is `hidden xl:block` — invisible below 1280px.
		const tocLinks = await page.locator('nav[aria-label="Section table of contents"] a').count();
		expect(tocLinks, '7 section anchors in TOC').toBe(7);
		// Section ids the TOC points to must exist on the page.
		for (const id of ['status', 'how-to-read', 'paradigms', 'patterns', 'infra', 'methodology', 'glossary']) {
			const ok = await page.locator(`section#${id}`).count();
			expect(ok, `section#${id} exists`).toBe(1);
		}
	});

	test('--color-fg-muted contrast against --color-bg meets WCAG AA (≥4.5:1) (P1.1)', async ({ page }) => {
		await page.goto('/experiments');
		const ratio = await page.evaluate(() => {
			const cs = getComputedStyle(document.documentElement);
			const fg = cs.getPropertyValue('--color-fg-muted').trim();
			const bg = cs.getPropertyValue('--color-bg').trim();
			const hexToRgb = (h: string) => {
				const m = h.replace('#', '');
				return [0, 2, 4].map((i) => parseInt(m.slice(i, i + 2), 16));
			};
			const lum = (rgb: number[]) => {
				const c = rgb.map((v) => {
					const s = v / 255;
					return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
				});
				return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
			};
			const la = lum(hexToRgb(fg));
			const lb = lum(hexToRgb(bg));
			return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
		});
		expect(ratio, '--color-fg-muted vs --color-bg WCAG ratio').toBeGreaterThanOrEqual(4.5);
	});

	test('heading semantics: ≥7 h2 (one per section), ≥31 h3 (paradigms+patterns) (P0.3)', async ({ page }) => {
		await page.goto('/experiments');
		const h2 = await page.locator('h2').count();
		const h3 = await page.locator('h3').count();
		expect(h2, '≥7 h2 (status.legend, how.to.read, paradigms.ledger, failure.patterns, infrastructure.live, methodology.artifacts, glossary.terms)').toBeGreaterThanOrEqual(7);
		expect(h3, '≥31 h3 (18 paradigm names + 13 pattern names)').toBeGreaterThanOrEqual(31);
	});

	test('hash auto-expand opens target paradigm row (P0.2)', async ({ page }) => {
		await page.goto('/experiments#P14');
		// $effect runs after hydration; wait briefly for the details to flip.
		await page.locator('article#P14 details[open]').waitFor({ timeout: 2000 });
		const opened = await page.locator('article#P14 details[open]').count();
		expect(opened, 'P14 details must be open after hash deep-link').toBe(1);
	});
});

test.describe('glossary auto-discovery (per-page coverage)', () => {
	// For every entry in $lib/data/glossary, assert at least one inline
	// JargonTip with the corresponding data-term attribute renders on every
	// page in entry.pages (default ['experiments']). The data-term attribute
	// (set on JargonTip wrapper) is stable across text-label variants — so
	// [Bonferroni correction|Bonferroni] still passes the "Bonferroni
	// correction" assertion.
	//
	// Adding a new glossary entry without an inline reference will fail the
	// matching test here, surfacing orphan glossary entries automatically.
	//
	// pages mapping → URL:
	//   - 'experiments' → '/experiments'
	//   - 'briefs'      → '/brief/2026-05-18' (a known-good fixture)
	const PAGE_URL: Record<'experiments' | 'briefs', string> = {
		experiments: '/experiments',
		briefs: '/brief/2026-05-18'
	};

	for (const entry of GLOSSARY) {
		const targetPages = entry.pages ?? ['experiments'];
		for (const tp of targetPages) {
			const url = PAGE_URL[tp];
			test(`glossary term "${entry.term}" has ≥1 inline JargonTip on ${url}`, async ({ page }) => {
				await page.goto(url);
				// Brief detail loads candidates via client-side fetch; wait for at
				// least the first CandidateCard to mount before counting tooltips.
				// /experiments is fully prerendered, so the wait resolves instantly
				// for that route.
				await page.locator('[data-testid="jargon-tip"]').first().waitFor({ timeout: 5000 });
				const count = await page
					.locator(`[data-testid="jargon-tip"][data-term="${entry.term}"]`)
					.count();
				expect(
					count,
					`term "${entry.term}" should appear as inline JargonTip somewhere on ${url} (glossary section excluded — those terms appear in <dt>, not JargonTip)`
				).toBeGreaterThanOrEqual(1);
			});
		}
	}
});

test.describe('smoke — mobile (390 + 360 viewports)', () => {
	// Catches: layout/header/nav/footer that forces a wider viewport than the
	// device; CandidateCard header strip that can't collapse; tooltips whose
	// absolute layout pushes scrollWidth past the viewport edge.
	const PHONES = [
		{ name: 'iphone-13', width: 390, height: 844 },
		{ name: 'small-android', width: 360, height: 800 }
	];
	for (const { name, width, height } of PHONES) {
		for (const path of ['/', '/briefs', '/about', '/experiments', `/brief/${latestDay.date}`]) {
			test(`${name} (${width}x${height}) — no horizontal scroll on ${path}`, async ({ page }) => {
				await page.setViewportSize({ width, height });
				const response = await page.goto(path);
				expect(response?.status()).toBe(200);
				const scrollW = await page.evaluate(() => document.documentElement.scrollWidth);
				expect(scrollW, `${path} scrollWidth must be ≤ viewport ${width}`).toBeLessThanOrEqual(width);
			});
		}
	}
});

test.describe('smoke — api fixture integrity', () => {
	test('every fixture day listed in days.json has a per-day file', () => {
		// Sanity check the mock fixture set itself — if a day is missing
		// from DAY_BODIES we want to fail at test discovery, not silently
		// 404 inside one of the route handlers downstream.
		const missing = days.filter((day) => !DAY_BODIES[day.date]);
		expect(missing, `fixtures missing per-day JSON for: ${missing.map((d) => d.date)}`).toEqual([]);
	});
});
