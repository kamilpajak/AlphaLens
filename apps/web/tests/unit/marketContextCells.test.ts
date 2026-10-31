import { describe, expect, it } from 'vitest';
import {
	indexCells,
	driverCells,
	residualCells,
	type MarketContextCell
} from '../../src/lib/components/marketContextCells';
import {
	MARKET_STATE_ATR_HIGH_PCTILE,
	MARKET_STATE_TREND_FLIP_PCT,
	MARKET_STATE_VIX_HIGH
} from '../../src/lib/format';

// The MARKET CONTEXT strip builds eight cells from two independent column
// families (`market_ret5_*` and the `market_state*` block), and either family
// can be absent on a published date. An absent cell DISAPPEARS — label, value
// and flip point together. It is never an em dash and never a placeholder.
//
// These builders are the code the component imports, not a copy of its
// template guard, so a later edit to the rule turns these red.
//
// THE RULE: a numeric cell is present when its input passes `Number.isFinite`;
// the squeeze cell is present when its input is exactly `true` or `false`.
// Never truthiness — `ret5 = 0`, `dist200 = 0`, `atr_pct_q = 0` and
// `squeeze_on = false` are all real readings, and the only day fixture in the
// repo carrying a market-state payload carries `squeeze_on: false`.

/** The real full-precision trio the production `five_session_index_returns`
 *  answered for 2026-05-19 against the grouped-daily store on this machine on
 *  2026-10-31. Rounds to the -0.60% / -0.81% / -3.39% the owner saw. */
const RET5_2026_05_19 = {
	ret5Spy: -0.006028339971280605,
	ret5Qqq: -0.00807363836887054,
	ret5Iwm: -0.033867714194712795
};

/** The real market-state payload on `tests/fixtures/api-mock/days/2026-09-05.json`
 *  (candidates[0]). `squeezeOn` is genuinely `false` there. */
const STATE_2026_09_05 = {
	atrPctQ: 0.0992063492063492,
	dist200: 0.08149528001641221,
	vix: 16.59,
	vixDecile: 0.3968253968253968,
	squeezeOn: false
};

const ABSENT = [null, undefined, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY];

const keys = (cells: MarketContextCell[]) => cells.map((c) => c.key);
const byKey = (cells: MarketContextCell[], key: string) => cells.find((c) => c.key === key);

describe('indexCells — the three 5-session index returns', () => {
	it('keeps all three when all three resolve, rendered as signed percents', () => {
		const cells = indexCells(RET5_2026_05_19);
		expect(keys(cells)).toEqual(['spy', 'qqq', 'iwm']);
		expect(byKey(cells, 'spy')?.value).toBe('-0.60%');
		expect(byKey(cells, 'qqq')?.value).toBe('-0.81%');
		expect(byKey(cells, 'iwm')?.value).toBe('-3.39%');
	});

	it('drops a cell for every flavour of absence, never an em dash', () => {
		for (const absent of ABSENT) {
			const cells = indexCells({ ...RET5_2026_05_19, ret5Qqq: absent });
			expect(keys(cells)).toEqual(['spy', 'iwm']);
			expect(cells.map((c) => c.value)).not.toContain('—');
		}
	});

	it('is EMPTY when the whole family is absent', () => {
		expect(indexCells({})).toEqual([]);
	});

	it('keeps a ZERO return — truthiness would delete a real reading', () => {
		const cells = indexCells({ ret5Spy: 0, ret5Qqq: 0, ret5Iwm: 0 });
		expect(keys(cells)).toEqual(['spy', 'qqq', 'iwm']);
		expect(byKey(cells, 'spy')?.value).toBe('+0.00%');
	});

	it('carries no flip point — the returns have no threshold to flip', () => {
		for (const cell of indexCells(RET5_2026_05_19)) {
			expect(cell.flip).toBeUndefined();
		}
	});
});

describe('driverCells — each value beside the level that would flip the label', () => {
	it('keeps a resolved driver WITH its flip point', () => {
		const cells = driverCells(STATE_2026_09_05);
		expect(keys(cells)).toEqual(['vs ma200', 'atr %ile', 'vix']);
		expect(byKey(cells, 'vs ma200')?.value).toBe('+8.1%');
		expect(byKey(cells, 'vs ma200')?.flip).toBe(`bear below ${MARKET_STATE_TREND_FLIP_PCT}%`);
		expect(byKey(cells, 'atr %ile')?.value).toBe('10');
		expect(byKey(cells, 'atr %ile')?.flip).toBe(`volatile at ${MARKET_STATE_ATR_HIGH_PCTILE}`);
		expect(byKey(cells, 'vix')?.value).toBe('16.6');
		expect(byKey(cells, 'vix')?.flip).toBe(`volatile at ${MARKET_STATE_VIX_HIGH}`);
	});

	it('drops an absent driver, so its flip point goes with it', () => {
		for (const absent of ABSENT) {
			const cells = driverCells({ ...STATE_2026_09_05, atrPctQ: absent });
			expect(keys(cells)).toEqual(['vs ma200', 'vix']);
		}
	});

	it('NEVER yields a flip point without a value', () => {
		const shapes = [
			{},
			STATE_2026_09_05,
			{ dist200: 0 },
			{ vix: Number.NaN, atrPctQ: 0 },
			{ dist200: null, atrPctQ: undefined, vix: 25 }
		];
		for (const shape of shapes) {
			for (const cell of driverCells(shape)) {
				expect(cell.value).toBeTruthy();
				expect(cell.value).not.toBe('—');
				expect(cell.flip).toBeTruthy();
			}
		}
	});

	it('keeps ZERO drivers — 0% from the MA200 and a 0th ATR %ile are readings', () => {
		const cells = driverCells({ dist200: 0, atrPctQ: 0, vix: 0 });
		expect(keys(cells)).toEqual(['vs ma200', 'atr %ile', 'vix']);
		expect(byKey(cells, 'vs ma200')?.value).toBe('+0.0%');
		expect(byKey(cells, 'atr %ile')?.value).toBe('0');
		expect(byKey(cells, 'vix')?.value).toBe('0.0');
	});

	it('is EMPTY on the 29% shape — returns published, market_state absent', () => {
		expect(driverCells({})).toEqual([]);
	});
});

describe('residualCells — what the drawer would reveal', () => {
	it('keeps both when both resolve, and `false` renders as "off"', () => {
		const cells = residualCells(STATE_2026_09_05);
		expect(keys(cells)).toEqual(['vix %ile', 'squeeze']);
		expect(byKey(cells, 'vix %ile')?.value).toBe('40');
		expect(byKey(cells, 'squeeze')?.value).toBe('off');
	});

	it('renders a TRUE squeeze as "on"', () => {
		expect(byKey(residualCells({ squeezeOn: true }), 'squeeze')?.value).toBe('on');
	});

	it('drops the squeeze cell only for null / undefined, never for `false`', () => {
		expect(keys(residualCells({ squeezeOn: null }))).toEqual([]);
		expect(keys(residualCells({ squeezeOn: undefined }))).toEqual([]);
		expect(keys(residualCells({ squeezeOn: false }))).toEqual(['squeeze']);
	});

	it('drops an absent vix %ile for every flavour of absence', () => {
		for (const absent of ABSENT) {
			expect(keys(residualCells({ vixDecile: absent, squeezeOn: false }))).toEqual(['squeeze']);
		}
	});

	it('is EMPTY when the drawer would reveal nothing — the toggle predicate', () => {
		// The strip offers its HIDE/MORE toggle only while this list is non-empty.
		expect(residualCells({})).toEqual([]);
		expect(residualCells(RET5_2026_05_19 as object)).toEqual([]);
	});
});

describe('the two states a reader actually meets', () => {
	it('the 29% shape (2026-05-19): three returns, no drivers, no drawer', () => {
		expect(indexCells(RET5_2026_05_19)).toHaveLength(3);
		expect(driverCells({})).toHaveLength(0);
		expect(residualCells({})).toHaveLength(0);
	});

	it('everything absent: three empty lists, so the strip is label + caption only', () => {
		expect(indexCells({})).toHaveLength(0);
		expect(driverCells({})).toHaveLength(0);
		expect(residualCells({})).toHaveLength(0);
	});
});
