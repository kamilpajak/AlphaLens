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
// THE OTHER HALF OF THE SAME RULE: a flip point annotates the LABEL, so it is
// printed only while the label is one of the four real states. A driver value
// CAN outlive its label, and not rarely: `classify_state` in `market_state.py`
// fills its telemetry dict before it decides anything, so any subset of the
// three drivers can arrive beside `unknown` — most importantly `vs ma200` and
// `atr %ile` together, when the realized vol leg is below 70 and no VIX print
// reaches the date, which is the one case the vol axis cannot settle (#1559).
// Those values then render alone, with no flip point. `driverCells` is told
// which case it is in (`labelKnown`); it never decides it, so the label
// vocabulary stays in `$lib/format` alone.
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
	 *  only ever beside a value, and only while a label EXISTS: a flip point
	 *  with nothing to annotate — "(volatile at 70)" next to nothing, or beside
	 *  `market · unknown` — is the defect this shape removes. */
	flip?: string;
}

/**
 * Format `value` to `digits` decimals WITHOUT letting rounding carry it across
 * `flipAt`.
 *
 * A driver is printed beside the level at which the label would change whenever
 * there IS a label, so a rounded value is not only a display choice:
 * `atr_pct_q = 0.695` rounds to `70` and reads as
 * `atr %ile 70 (volatile at 70)` while the classifier compares
 * `atr_pct_q >= 0.70` and the label is still quiet. A driver sitting
 * exactly on its stated flip point with no flip reads as a classifier bug —
 * the same class of defect as a flip point beside an absent value.
 *
 * Rounding is kept everywhere else, deliberately. Flooring unconditionally
 * would print `28` for `0.29`, because `0.29 * 100` is `28.999999999999996` in
 * binary floating point. So this truncates ONLY on the step that would cross
 * the threshold, and the threshold sits exactly on the display grid (`70`,
 * `25.0`), which is why the one-sided check is enough.
 */
function belowFlipSafe(value: number, digits: number, flipAt: number): string {
	const rounded = value.toFixed(digits);
	if (value < flipAt && Number(rounded) >= flipAt) {
		const scale = 10 ** digits;
		return (Math.trunc(value * scale) / scale).toFixed(digits);
	}
	return rounded;
}

/** `0`-`1` quantile rank → a whole-number percentile string, or null when the
 *  input is not a finite number. The null-returning sibling of the old local
 *  helper that folded absence into `'—'`. Pass `flipAt` on a quantile that is
 *  printed beside a threshold; the drawer's `vix %ile` has none. */
function pctile01(v: number | null | undefined, flipAt?: number): string | null {
	if (!Number.isFinite(v)) return null;
	const pct = (v as number) * 100;
	return flipAt === undefined ? `${Math.round(pct)}` : belowFlipSafe(pct, 0, flipAt);
}

/** `fmtFracSignedPct` only for a finite fraction; null otherwise. */
function fracPct(v: number | null | undefined, digits: number): string | null {
	return Number.isFinite(v) ? fmtFracSignedPct(v, digits) : null;
}

/** A level formatted against its own flip point: finite, `digits` decimals, and
 *  never rounded up onto or past `flipAt`. The formatting does not depend on
 *  whether the flip point is PRINTED — see `driverCells`. */
function levelBelowFlip(
	v: number | null | undefined,
	digits: number,
	flipAt: number
): string | null {
	return Number.isFinite(v) ? belowFlipSafe(v as number, digits, flipAt) : null;
}

/** `on` / `off` for a real squeeze reading, `null` for an absence.
 *
 *  `=== true` / `=== false`, never truthiness: `false` is a real reading ("no
 *  squeeze today") and only null / undefined is an absence. Two guards rather
 *  than one expression, because a three-way answer does not read as a chain of
 *  ternaries. */
function squeezeLabel(squeezeOn: boolean | null | undefined): string | null {
	if (squeezeOn === true) return 'on';
	if (squeezeOn === false) return 'off';
	return null;
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
 * The three label drivers, each annotated with its flip point — while there is
 * a label to flip.
 *
 * The annotation is what makes a driver actionable — `+8.1%` says much less
 * without "bear below 0%" beside it — so it travels WITH its cell and vanishes
 * with it. The three thresholds are the classifier's own frozen constants,
 * pinned against `market_state.py` by `tests/unit/format.test.ts`.
 *
 * The cost of this rule is real and is accepted: on an `unknown` date the
 * reader sees `atr %ile 65` with no "volatile at 70" beside it, and 65 is
 * harder to read without the threshold. The alternative was worse — stating
 * where a label would flip when there is no label to flip, on exactly the
 * dates where the classifier failed to decide.
 *
 * `labelKnown` is the caller's answer to "is the regime word real?", passed in
 * rather than computed here: this module stays pure and knows nothing about the
 * label vocabulary, so the rule has exactly one home (`isKnownMarketState` in
 * `$lib/format`). On `false` the VALUES still render — they are real readings —
 * but no flip point does, because "(volatile at 25)" beside `market · unknown`
 * annotates the flip of a label that does not exist.
 *
 * A driver outliving its label is not a corner case. The producer stamps the
 * telemetry before it decides, so each of these is reachable: `vix` alone (a
 * store under 200 sessions), `vs ma200` + `vix` (200-251 sessions, no ATR
 * percentile yet), and `vs ma200` + `atr %ile` (the vol axis cannot settle
 * because the realized leg is low and no VIX print reaches the date). What is
 * NOT reachable is all three finite beside `unknown` — three finite drivers
 * decide the label.
 *
 * The threshold-safe rounding stays on BOTH sides, deliberately: how a reading
 * is formatted is a property of the number, so the two label states must never
 * print the same VIX differently.
 */
export function driverCells(
	{ dist200, atrPctQ, vix }: DriverInputs,
	labelKnown: boolean
): MarketContextCell[] {
	const flipIfLabelled = (flip: string) => (labelKnown ? flip : undefined);
	return present([
		{
			key: 'vs ma200',
			value: fracPct(dist200, 1),
			flip: flipIfLabelled(`bear below ${MARKET_STATE_TREND_FLIP_PCT}%`)
		},
		{
			key: 'atr %ile',
			value: pctile01(atrPctQ, MARKET_STATE_ATR_HIGH_PCTILE),
			flip: flipIfLabelled(`volatile at ${MARKET_STATE_ATR_HIGH_PCTILE}`)
		},
		{
			key: 'vix',
			value: levelBelowFlip(vix, 1, MARKET_STATE_VIX_HIGH),
			flip: flipIfLabelled(`volatile at ${MARKET_STATE_VIX_HIGH}`)
		}
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
		{ key: 'squeeze', value: squeezeLabel(squeezeOn) }
	]);
}
