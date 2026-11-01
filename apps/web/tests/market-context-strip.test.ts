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
 * `src/lib/components/marketContextCells.ts` are pinned by their own unit
 * suite, but those tests exercise the MODEL. The user-visible half — that an empty row and the
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
 *
 * ONE MORE RULE IS GATED HERE, for the same reason — it is template-only: a
 * flip point annotates the LABEL, so a driver that outlived its label renders
 * alone, with no "(volatile at 70)" beside `market · unknown`. The composed
 * 2026-07-06 payload is that shape.
 */

import { expect, test, type Page } from '@playwright/test';
import {
	MARKET_STATE_ATR_HIGH_PCTILE,
	MARKET_STATE_TREND_FLIP_PCT,
	MARKET_STATE_VIX_HIGH
} from '../src/lib/format';
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

/**
 * DRIVERS WITHOUT A LABEL, composed from the ONE real post-signal capture by
 * deleting the two VIX columns and stamping the producer's own `unknown`
 * sentinel. No number is introduced.
 *
 * This is the shape `classify_state` emits when the vol axis cannot settle: the
 * axis is an OR of a realized and an implied leg, so a realized leg below the
 * 70th percentile with no VIX print reaching the date leaves it undecided
 * (#1559). The function fills its telemetry dict BEFORE that decision, so it
 * returns the literal label `'unknown'` (`_UNKNOWN` in `market_state.py`) next
 * to a finite `dist200`, a finite `atr_pct_q` and a real `squeeze_on`, with
 * `vix` and `vix_decile` NaN. The column is stamped, not absent — which is why
 * `market_state` is SET here rather than deleted.
 *
 * 2026-07-06 is the first Monday after the `market_state` block shipped
 * (2026-07-05); any later date would do, since the cause is a missing VIX print
 * and not a short store.
 *
 * Every surviving value is the real `days/2026-09-05.json` capture. The deleted
 * keys go from EVERY candidate row, not just `candidates[0]`, so the payload
 * stays internally consistent — the block is index-level and identical on every
 * row.
 */
const NO_LABEL_DATE = '2026-07-06';
const VIX_KEYS = ['market_state_vix', 'market_state_vix_decile'];
/** What the real `market_state_dist200`, `market_state_atr_pct_q` and
 *  `market_state_vix` of the 2026-09-05 capture render as. */
const DIST200_CELL_VALUE = '+8.1%';
const ATR_PCTILE_CELL_VALUE = '10';
const VIX_CELL_VALUE = '16.6';
/** The two residual (drawer) cells of the same capture: `market_state_vix_decile`
 *  is `0.3968…` and `market_state_squeeze_on` is `false`. */
const VIX_PCTILE_CELL_VALUE = '40';
const SQUEEZE_CELL_VALUE = 'off';
const NO_LABEL_DAY = {
	...LABELLED_DAY,
	date: NO_LABEL_DATE,
	candidates: LABELLED_DAY.candidates.map((c: Record<string, unknown>) => {
		const row: Record<string, unknown> = { ...c, market_state: 'unknown' };
		for (const key of VIX_KEYS) delete row[key];
		return row;
	})
};

/** The day bodies this file composes rather than reads straight from disk. */
const COMPOSED_BODIES: Record<string, string> = {
	[RETURNS_ONLY_DATE]: RETURNS_ONLY_BODY,
	[NO_LABEL_DATE]: JSON.stringify(NO_LABEL_DAY)
};

// `NO_LABEL_DATE` is deliberately NOT in this index: it is reached by URL, and
// listing it would put a date WITH a drawer between the two the drawer-request
// walk below steps across.
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
		if (day && COMPOSED_BODIES[day[1]] !== undefined) {
			return route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: COMPOSED_BODIES[day[1]]
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

/**
 * The rendered horizontal distance, in CSS pixels, from the last glyph of each
 * `<dt>` to the first glyph of its own `<dd>` — one number per `<dt>`/`<dd>`
 * group matched by `selector`.
 *
 * Geometry, because every text and structure assertion in this file passes
 * whether the pair sits together or a hand's width apart, which is how the
 * drawer shipped with its terms parked at one edge and its values at the other.
 *
 * GLYPHS, not boxes. A box measurement would be fooled by the obvious
 * regression: let the `<dd>` stretch and right-align its text (`grow` plus
 * `text-right`, which `CandidateCard.svelte` does for its own value column) and
 * the two boxes still touch while the reader sees the same gap as before. A
 * `Range` over each element's contents measures where the text actually is.
 */
function pairGaps(page: Page, selector: string): Promise<number[]> {
	return page.locator(selector).evaluateAll((groups) =>
		groups.map((g) => {
			const textBox = (el: Element) => {
				const range = document.createRange();
				range.selectNodeContents(el);
				return range.getBoundingClientRect();
			};
			return textBox(g.querySelector('dd')!).left - textBox(g.querySelector('dt')!).right;
		})
	);
}

/**
 * The drawer at its two reachable cell counts, with the cells each one holds.
 *
 * `residualCells` returns at most TWO — the drivers were promoted to the face —
 * and often one. `NO_LABEL_DATE` is the one-cell shape for free: it already
 * deletes `market_state_vix_decile` and keeps the real `squeeze_on`, so no
 * extra payload is composed for it.
 */
/** The drawer list's accessible name, as the component sets it. */
const DRAWER_LIST_LABEL = 'market context residuals';

const DRAWER_SHAPES: [date: string, terms: string[], values: string[]][] = [
	[LABELLED_DATE, ['vix %ile', 'squeeze'], [VIX_PCTILE_CELL_VALUE, SQUEEZE_CELL_VALUE]],
	[NO_LABEL_DATE, ['squeeze'], [SQUEEZE_CELL_VALUE]]
];

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

		// Asserted on the <dd> itself, which also pins WHERE the annotation
		// sits. A <dl>'s group <div> may contain only <dt> and <dd>, so the
		// flip point lives inside the <dd>; as a sibling it would be invalid
		// markup and would be announced detached from the value. A locator on
		// the flip TEXT matches either way and cannot see the difference.
		await expect(drivers.locator('dd')).toHaveText([
			`${DIST200_CELL_VALUE} (bear below ${MARKET_STATE_TREND_FLIP_PCT}%)`,
			`${ATR_PCTILE_CELL_VALUE} (volatile at ${MARKET_STATE_ATR_HIGH_PCTILE})`,
			`${VIX_CELL_VALUE} (volatile at ${MARKET_STATE_VIX_HIGH})`
		]);
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

		// No cell survives in any of the strip's three lists. Two scopings are
		// deliberate here. On <dd> rather than free text, because the glossary
		// tooltip inside this same region carries em dashes and the phrase
		// "bear below" in its prose, so a text locator would match the
		// explanation rather than the data and could never fail. And on
		// `dl[aria-label]` rather than `dl`, because that tooltip renders its
		// threshold bands through `MetricGrid`, whose own <dl> is unlabelled and
		// holds 8 <dd> on this very date — a bare `dl dd` fails on the
		// explanation while the strip is empty exactly as required.
		//
		// It covers the index row and the driver row. It does NOT cover the
		// drawer, whatever its label: `{#if residual.length > 0}` keeps the
		// whole drawer out of the DOM on this date, which the zero-button
		// assertion above already states. Saying "all three" here would be a
		// property that is true and empty. The drawer's own list is gated by
		// the geometry test, which locates it by that label.
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

	// The drawer REQUEST must not outlive the drawer. The strip is not remounted
	// when the reader steps from one brief date to the next — same route, new
	// param — so a request made on a date with a residual used to survive a date
	// without one and re-open the drawer on return, in a view where nobody asked
	// for it. The walk below is therefore client-side (the prev/next links), not
	// `page.goto`, which would remount and prove nothing.
	test('the drawer request does not survive a date that has no drawer', async ({ page }) => {
		await installMockedApi(page);
		await page.goto(`/brief/${LABELLED_DATE}`);

		const s = strip(page);
		const toggle = s.getByRole('button');
		await expect(toggle).toHaveAttribute('aria-expanded', 'false');
		await toggle.click();
		await expect(toggle).toHaveAttribute('aria-expanded', 'true');
		await expect(s.getByText('vix %ile', { exact: false })).toBeVisible();

		// Step back one date, where market_state is absent and the toggle unmounts.
		await page.getByRole('link', { name: RETURNS_ONLY_DATE }).click();
		await expect(page.getByTestId('brief-date')).toHaveText(RETURNS_ONLY_DATE);
		await expect(s.getByRole('button')).toHaveCount(0);

		// ...and forward again. The drawer must be CLOSED: same mounted strip,
		// but this is a fresh view and the reader has not asked for it here.
		await page.getByRole('link', { name: LABELLED_DATE }).click();
		await expect(page.getByTestId('brief-date')).toHaveText(LABELLED_DATE);
		await expect(s.getByRole('button')).toHaveAttribute('aria-expanded', 'false');
		await expect(s.getByText('vix %ile', { exact: false })).toHaveCount(0);
	});
	// A flip point annotates the LABEL, so it must not outlive it. The drivers
	// can: the producer stamps its telemetry before it decides, so a date can
	// carry `vs ma200 +8.1%` and `atr %ile 10` beside `market · unknown`. Both
	// used to print their flip points there — the flip points of a label that is
	// not on the page.
	test('drivers with NO label beside them show their values and no flip point', async ({
		page
	}) => {
		await installMockedApi(page);
		await page.goto(`/brief/${NO_LABEL_DATE}`);

		const s = strip(page);
		const drivers = s.locator('dl[aria-label="market state drivers"]');
		// The values survive — they are real readings.
		await expect(drivers.locator('dd')).toHaveText([DIST200_CELL_VALUE, ATR_PCTILE_CELL_VALUE]);
		await expect(s.getByText('market · unknown', { exact: false })).toBeVisible();

		// No flip point, and no EMPTY bracket either. The template wraps the
		// whole "(…)" span in its own guard, so dropping that guard would print
		// "+8.1% ()" — which an assertion on the flip TEXT alone cannot see.
		await expect(drivers).not.toContainText('volatile at');
		await expect(drivers).not.toContainText('bear below');
		await expect(drivers).not.toContainText('(');
	});

	// THE DRAWER MUST READ LIKE THE FACE ROWS, gated by MEASUREMENT because the
	// defect it replaces was invisible to every other kind of assertion in this
	// file.
	//
	// The drawer used to be a `grid grid-cols-2 sm:grid-cols-4` whose every cell
	// was `flex items-center justify-between`. The four columns were a leftover:
	// they were sized when four residual cells existed, and once the drivers were
	// promoted to the face `residualCells` returns at most TWO. So each term was
	// pinned to the left edge of a quarter-width column and its value to the
	// right edge of it.
	//
	// Measured with this suite on 2026-11-01, as the gap from a term's right edge
	// to its value's left edge:
	//
	//            viewport   drawer (before)   drawer (after)   driver row
	//            1280px     217.9 px          6 px             6 px
	//            360px       75.1 px          6 px             6 px
	//
	// The bound is NOT a literal. The driver row on the SAME page is measured and
	// the drawer must match it, because both carry `gap-1.5` and the right answer
	// is equality — a token change then moves both together instead of making
	// this test lie. 1 CSS px of slack covers sub-pixel layout rounding; the
	// defect was 36× that bound at 1280px and 12× at 360px, so the slack is not
	// what decides the verdict.
	//
	// Checked at BOTH cell counts on purpose: the claim is that the count does
	// not change how a pair reads, which neither count can show on its own. And
	// at phone width, because the old gap shrank with the viewport without ever
	// closing.
	test('the drawer puts each term beside its value, at one cell and at two', async ({ page }) => {
		await installMockedApi(page);

		for (const width of [1280, 360]) {
			await page.setViewportSize({ width, height: 900 });

			for (const [date, terms, values] of DRAWER_SHAPES) {
				await page.goto(`/brief/${date}`);
				const s = strip(page);
				await s.getByRole('button', { name: /more/i }).click();

				// Located by its accessible NAME, not by the wrapper's id. The drawer
				// was the only one of the strip's three lists without one, and the
				// empty-date test cannot pin it (see there), so this is where the
				// label is held: delete it and these assertions stop matching.
				const drawer = s.locator(`dl[aria-label="${DRAWER_LIST_LABEL}"]`);
				await expect(drawer).toBeVisible();

				// The right cells, in order, still as <dt>/<dd> pairs — one group
				// each, so no column track can lay a pair out instead of the group.
				await expect(drawer.locator('dt')).toHaveText(terms);
				await expect(drawer.locator('dd')).toHaveText(values);
				await expect(drawer.locator('> div')).toHaveCount(terms.length);

				// ...and the geometry, calibrated against the row above it. The
				// calibration is asserted first: a date whose driver row is absent
				// (the 29% shape has none) would otherwise make every comparison
				// NaN and print "driver row is undefinedpx".
				const driverGaps = await pairGaps(page, 'dl[aria-label="market state drivers"] > div');
				expect(driverGaps.length, `${date}: no driver row to calibrate against`).toBeGreaterThan(
					0
				);
				const [reference] = driverGaps;
				const gaps = await pairGaps(page, `dl[aria-label="${DRAWER_LIST_LABEL}"] > div`);
				expect(gaps).toHaveLength(terms.length);
				for (const gap of gaps) {
					expect(
						Math.abs(gap - reference),
						`${date} at ${width}px: drawer term-to-value gap is ${gap}px, driver row is ${reference}px`
					).toBeLessThanOrEqual(1);
				}
			}
		}
	});
});
