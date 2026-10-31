// Pure cell builders behind <MarketContextBanner> — the eight cells of the
// brief-level MARKET CONTEXT strip, built from two INDEPENDENT column families
// that each go absent on their own schedule:
//
//   * `market_ret5_{spy,qqq,iwm}` — the three 5-session index returns. Reach
//     back to 2024-09-11, the first session in the grouped-daily store.
//   * the `market_state*` block — the four-state label plus its three drivers,
//     the VIX percentile and the squeeze flag. Ships from 2026-07-05 only.
//
// So the DOMINANT historical shape is returns WITHOUT a label: measured on
// production Postgres on 2026-10-30, 47 of 164 brief dates (the window
// 2026-05-19 … 2026-07-04) land there — 29% of everything a reader can scroll
// back to, and the first thing the owner hit.
//
// THE RULE: an absent cell DISAPPEARS — its label, its value and its flip point
// together. It is never an em dash, never greyed, never a placeholder. Nothing
// in the strip is read positionally (each cell carries its own `dt`), so a row
// of two cells is as legible as a row of three, and a kept-but-empty slot would
// be the same defect wearing a quieter coat.
//
// PRESENCE IS DECIDED FROM THE INPUT, NEVER FROM THE RENDERED STRING. Filtering
// on the `'—'` sentinel would couple three unrelated formatters to one glyph and
// would delete a genuine value the day any of them legitimately emits an em
// dash. And presence is `Number.isFinite`, never truthiness: `ret5 = 0`
// (`+0.00%`), `dist200 = 0` (`+0.0%`), `atr_pct_q = 0` and `squeeze_on = false`
// are all real readings — the one day fixture in the repo that carries a
// market-state payload carries `squeeze_on: false`. Note `Number.isFinite`, not
// the global `isFinite`: `isFinite(null)` is `true`, so the global would keep an
// absent driver and print `0`.
//
// Extracted from the component rather than left as template guards so the unit
// suite exercises the code the component imports instead of a copy of it.
// Unit-tested in tests/unit/marketContextCells.test.ts.
import {
	fmtNum,
	fmtFracSignedPct,
	MARKET_STATE_ATR_HIGH_PCTILE,
	MARKET_STATE_VIX_HIGH,
	MARKET_STATE_TREND_FLIP_PCT
} from '$lib/format';

export interface MarketContextCell {
	/** Short `dt` key, lower-case: `spy`, `vs ma200`, `vix %ile`. */
	key: string;
	/** Already-formatted value. Present by construction — an absent value
	 *  yields no cell at all, so this is never the `'—'` sentinel. */
	value: string;
	/** The level at which the label would flip. Carried ONLY by the drivers,
	 *  and only ever beside a value: a flip point with nothing to annotate
	 *  ("(volatile at 70)" next to nothing) is the defect this shape removes. */
	flip?: string;
}

/** `0`-`1` quantile rank → a whole-number percentile string, or null when the
 *  input is not a finite number. The null-returning sibling of the old local
 *  helper that folded absence into `'—'`. */
function pctile01(v: number | null | undefined): string | null {
	return Number.isFinite(v) ? `${Math.round((v as number) * 100)}` : null;
}

/** `fmtFracSignedPct` only for a finite fraction; null otherwise. */
function fracPct(v: number | null | undefined, digits: number): string | null {
	return Number.isFinite(v) ? fmtFracSignedPct(v, digits) : null;
}

/** `fmtNum` only for a finite number; null otherwise. */
function num(v: number | null | undefined, digits: number): string | null {
	return Number.isFinite(v) ? fmtNum(v, digits) : null;
}

/** Keep only the cells whose value resolved, dropping the flip point with it. */
function present(
	rows: { key: string; value: string | null; flip?: string }[]
): MarketContextCell[] {
	return rows
		.filter((r): r is { key: string; value: string; flip?: string } => r.value !== null)
		.map(({ key, value, flip }) => (flip === undefined ? { key, value } : { key, value, flip }));
}

export interface IndexReturnInputs {
	ret5Spy?: number | null;
	ret5Qqq?: number | null;
	ret5Iwm?: number | null;
}

/**
 * The three raw broad-index 5-session returns, as the face row.
 *
 * They carry NO flip point, deliberately: they are raw
 * `close(t)/close(t-5) - 1` with no threshold and no emission rule, which is
 * also why they carry no `config_version`.
 */
export function indexCells({ ret5Spy, ret5Qqq, ret5Iwm }: IndexReturnInputs): MarketContextCell[] {
	return present([
		{ key: 'spy', value: fracPct(ret5Spy, 2) },
		{ key: 'qqq', value: fracPct(ret5Qqq, 2) },
		{ key: 'iwm', value: fracPct(ret5Iwm, 2) }
	]);
}

export interface DriverInputs {
	dist200?: number | null;
	atrPctQ?: number | null;
	vix?: number | null;
}

/**
 * The three label drivers, each annotated with its flip point.
 *
 * The annotation is what makes a driver actionable — `+8.1%` says nothing
 * without "bear below 0%" beside it — so it travels WITH its cell and vanishes
 * with it. The three thresholds are the classifier's own frozen constants,
 * pinned against `market_state.py` by `tests/unit/format.test.ts`.
 */
export function driverCells({ dist200, atrPctQ, vix }: DriverInputs): MarketContextCell[] {
	return present([
		{
			key: 'vs ma200',
			value: fracPct(dist200, 1),
			flip: `bear below ${MARKET_STATE_TREND_FLIP_PCT}%`
		},
		{
			key: 'atr %ile',
			value: pctile01(atrPctQ),
			flip: `volatile at ${MARKET_STATE_ATR_HIGH_PCTILE}`
		},
		{ key: 'vix', value: num(vix, 1), flip: `volatile at ${MARKET_STATE_VIX_HIGH}` }
	]);
}

export interface ResidualInputs {
	vixDecile?: number | null;
	squeezeOn?: boolean | null;
}

/**
 * What the residual drawer would reveal — everything NOT on the face.
 *
 * The promoted drivers are deliberately not repeated here: two copies of one
 * number twenty lines apart is a silent doubling the moment one is edited.
 *
 * An EMPTY list is also the toggle predicate. The strip offers its HIDE/MORE
 * button only while this is non-empty, because an expander that opens onto
 * nothing is worse than no expander.
 */
export function residualCells({ vixDecile, squeezeOn }: ResidualInputs): MarketContextCell[] {
	return present([
		{ key: 'vix %ile', value: pctile01(vixDecile) },
		// `=== true` / `=== false`, never truthiness: `false` is a real reading
		// ("no squeeze today"), and only null / undefined is an absence.
		{ key: 'squeeze', value: squeezeOn === true ? 'on' : squeezeOn === false ? 'off' : null }
	]);
}
