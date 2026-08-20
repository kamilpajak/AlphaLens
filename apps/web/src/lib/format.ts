export function fmtUsdCompact(value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '—';
	const abs = Math.abs(value);
	if (abs >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
	if (abs >= 1e6) return `$${(value / 1e6).toFixed(1)}M`;
	if (abs >= 1e3) return `$${(value / 1e3).toFixed(0)}k`;
	return `$${value.toFixed(0)}`;
}

/** Exact dollar price with 2 decimals — for trade-setup levels ($312.50),
 *  distinct from fmtUsdCompact which abbreviates to B/M/k for market caps. */
export function fmtPrice(value: number | null | undefined, digits = 2): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '—';
	return `$${value.toFixed(digits)}`;
}

/** Format a percentage. Default prepends a sign (suited to signed deltas /
 *  yields like FCFF yield, MA distance). Pass withSign=false for unsigned
 *  ratios such as position-size or risk-allocation %, where a leading "+"
 *  reads as a quote-style change indicator and is misleading. */
export function fmtPct(value: number | null | undefined, digits = 1, withSign = true): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '—';
	const sign = withSign && value >= 0 ? '+' : '';
	return `${sign}${value.toFixed(digits)}%`;
}

export function fmtNum(value: number | null | undefined, digits = 1): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '—';
	return value.toFixed(digits);
}

/** A signed fixed-decimal number: `+1.20` / `-1.20` (the built-in minus, no
 *  double sign) / `—` for null/non-finite. The unit-less sibling of `fmtPct` —
 *  used for the αt bar values and the vol z-score chip (append a suffix like
 *  `σ` at the call site). */
export function fmtSigned(value: number | null | undefined, digits = 2): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '—';
	return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`;
}

export function fmtPctile(value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '—';
	return `${Math.round(value)}`;
}

/** Decide how to render the insider 90d signal honestly.
 *
 * `insider_score_sector_percentile` is a `<=`-rank: a ticker with ZERO net
 * opportunistic buying lands at ~100th percentile whenever its sector peers
 * are net sellers (`0 <= 0` counts). Rendering that as a green "100%ile" bar
 * reads as "strong insider buying" when it really means "not selling, in a
 * selling sector". Gate the bullish percentile bar on actual net buying
 * (`insider_score_usd > 0`); for zero / net-selling / no-data, return a muted
 * state so the card never shows a high percentile on a 0/negative dollar
 * signal. (Phase 1 of the insider-signal redesign — display-only; the netting
 * and ranking math are addressed separately in the pipeline.) */
export type InsiderDisplay =
	| { mode: 'bar'; percentile: number | null; netUsd: number }
	| { mode: 'muted'; label: string };

export function insiderDisplay(
	scoreUsd: number | null | undefined,
	percentile: number | null | undefined
): InsiderDisplay {
	if (scoreUsd === null || scoreUsd === undefined || !Number.isFinite(scoreUsd)) {
		return { mode: 'muted', label: '—' };
	}
	if (scoreUsd > 0) {
		const pct =
			percentile === null || percentile === undefined || !Number.isFinite(percentile)
				? null
				: percentile;
		return { mode: 'bar', percentile: pct, netUsd: scoreUsd };
	}
	if (scoreUsd < 0) return { mode: 'muted', label: 'net selling' };
	return { mode: 'muted', label: 'no buys' };
}

/** Magic-formula cell display. A candidate that fails the health gate (no PE /
 * negative equity) never gets a Greenblatt rank, so the cell has no value. Every
 * sibling FUNDAMENTALS row renders a muted "—" for a missing value; this helper
 * does the same for the unranked case instead of the old verbose "health-gate
 * fail" phrase, which read inconsistently in a column of numbers/dashes (the
 * reason lives in the cell's tooltip / glossary). A finite rank returns the
 * rounded rank plus its sector-cohort size. */
export type MagicFormulaDisplay =
	| { mode: 'rank'; rank: number; cohortN: number | null }
	| { mode: 'muted'; label: string };

export function magicFormulaDisplay(
	rank: number | null | undefined,
	cohortN: number | null | undefined
): MagicFormulaDisplay {
	if (rank === null || rank === undefined || !Number.isFinite(rank)) {
		return { mode: 'muted', label: '—' };
	}
	const n =
		cohortN === null || cohortN === undefined || !Number.isFinite(cohortN) ? null : cohortN;
	return { mode: 'rank', rank: Math.round(rank), cohortN: n };
}

export function fmtDate(value: string | null | undefined): string {
	if (!value) return '—';
	return value.slice(0, 10);
}

export function confidenceLabel(conf: number | null | undefined): string {
	if (conf === null || conf === undefined) return '—';
	const stars = Math.round(conf * 5);
	return `${stars}/5`;
}

export type ConfidenceTone = 'green' | 'amber' | 'cyan' | 'muted';

export function confidenceTone(conf: number | null | undefined): ConfidenceTone {
	if (conf == null) return 'muted';
	if (conf >= 0.8) return 'green';
	if (conf >= 0.6) return 'amber';
	if (conf >= 0.4) return 'cyan';
	return 'muted';
}

export type BuffettTone = 'green' | 'amber' | 'muted';

/** Tone for the Buffett quality chip (0-100). Three-state per the card design:
 *  green >= 70, amber 40-69, muted < 40 (and muted when null). The score is a
 *  hand-chosen screening heuristic, display-only — see the design memo. */
export function buffettTone(score: number | null | undefined): BuffettTone {
	if (score == null || !Number.isFinite(score)) return 'muted';
	if (score >= 70) return 'green';
	if (score >= 40) return 'amber';
	return 'muted';
}

// --- Buffett deep-read drawer pillars (card PR-4) ---------------------------
// Map each qualitative LLM enum / bool to a badge tone. Absent values (the ""
// enums from the no-10-K path, or a null `understandable`) read as `muted` —
// never a false verdict.
export type PillarTone = 'good' | 'mixed' | 'bad' | 'muted';

export function moatTone(moatType: string | null | undefined): PillarTone {
	if (!moatType) return 'muted';
	return moatType === 'none' ? 'bad' : 'good';
}

export function moatTrendTone(trend: string | null | undefined): PillarTone {
	switch (trend) {
		case 'widening':
			return 'good';
		case 'stable':
			return 'mixed';
		case 'narrowing':
			return 'bad';
		default:
			return 'muted'; // unclear / "" / null
	}
}

export function candorTone(candor: string | null | undefined): PillarTone {
	switch (candor) {
		case 'candid':
			return 'good';
		case 'mixed':
			return 'mixed';
		case 'promotional':
			return 'bad';
		default:
			return 'muted'; // unclear / "" / null
	}
}

export function understoodTone(understandable: boolean | null | undefined): PillarTone {
	if (understandable === true) return 'good';
	if (understandable === false) return 'bad';
	return 'muted';
}

export function understoodLabel(understandable: boolean | null | undefined): string {
	if (understandable === true) return 'yes';
	if (understandable === false) return 'no';
	return '—';
}

export function technicalsTrend(slope: number | null | undefined): 'up' | 'down' | 'flat' {
	if (slope === null || slope === undefined || !Number.isFinite(slope)) return 'flat';
	if (slope > 0.05) return 'up';
	if (slope < -0.05) return 'down';
	return 'flat';
}

// --- Expert panel: O'Neil tone + disagreement bands (PR-8b) -----------------
// O'Neil's own 0-100 score colour (same three-state shape as buffettTone; its own
// helper so the two experts' cutoffs are independently documented + catalogued in
// panel_config_version). Display-only; never translated into a buy/avoid word.
export function oneilTone(score: number | null | undefined): BuffettTone {
	if (score == null || !Number.isFinite(score)) return 'muted';
	if (score >= 70) return 'green';
	if (score >= 40) return 'amber';
	return 'muted';
}

// NOTE: the disagreement `expert_spread` (0-100) is surfaced RAW in the expert
// drawer (the gap number + a "two uncalibrated heuristics" caveat) — NOT bucketed
// into a consensus/mixed/split verdict word or an authority colour. The old
// consensusBand/consensusTone display helpers were removed (they signalled a
// confidence the scalar does not have); the deferred Expert×EDGE study correlates
// the raw scalar, never a bucket.

// --- Market-state context banner (PR-3) -------------------------------------
// The index-level regime label (SPY trend × volatility) is DISPLAY-ONLY, frozen
// a-priori, UNVALIDATED, and held out of every candidate sort/selection. The
// tone is a descriptive colour, NOT a buy/avoid signal. Its own domain enum (the
// PillarTone precedent) so the 5 states map independently; `bear_quiet` uses the
// desaturated `red-dim` token (bearish but calm) to stay distinct from the full
// `red` of `bear_volatile`. Any unrecognised / absent value → `muted` (never a
// false regime colour) — this also covers dates that predate the signal.
export type MarketStateTone = 'green' | 'amber' | 'red' | 'red-dim' | 'muted';

const _MARKET_STATE_TONES: Record<string, MarketStateTone> = {
	bull_quiet: 'green',
	bull_volatile: 'amber',
	bear_volatile: 'red',
	bear_quiet: 'red-dim'
};

export function marketStateTone(state: string | null | undefined): MarketStateTone {
	if (!state) return 'muted';
	return _MARKET_STATE_TONES[state] ?? 'muted';
}

/** Hyphenated chip label for a regime state (`bull_quiet` → `bull-quiet`). Any
 *  null / empty / unrecognised value → `"unknown"` (the first-class no-signal
 *  state, shown on dates that predate the label). */
export function marketStateLabel(state: string | null | undefined): string {
	if (!state || !(state in _MARKET_STATE_TONES)) return 'unknown';
	return state.replaceAll('_', '-');
}

/**
 * Raw fcff-yield annotation for the merged Valuation row. The sector-%ile drives
 * the SignalBar headline; this returns the raw % shown beneath it — null (no
 * annotation row) when the raw value is absent/non-finite.
 */
export function fcffYieldRawDisplay(rawPct: number | null | undefined): string | null {
	return Number.isFinite(rawPct) ? fmtPct(rawPct, 2) : null;
}

/**
 * Whether a 10-K exists for the ticker, read from the gate arrays. The `tenk`
 * gate is `passed` when theme keywords matched the 10-K and `failed` when the
 * 10-K exists but no keyword hit — both mean the filing is available; only
 * `unknown` (absent from both) means no 10-K. Used to explain an absent Buffett
 * qualitative read (which reads the 10-K).
 */
export function tenkAvailable(
	gatesPassed: string[] | null | undefined,
	gatesFailed: string[] | null | undefined
): boolean {
	return Boolean(gatesPassed?.includes('tenk') || gatesFailed?.includes('tenk'));
}

/**
 * The meta-bar headline score. The brief is ranked by `selection_score`
 * (= layer4_weighted_score − atr_penalty), so the badge next to "RANK" shows
 * that operative score, not the raw layer4 input (which lives in the drawer's
 * SCORER BREAKDOWN). Falls back to layer4 for older briefs without a
 * selection_score. Integer-valued scores render without decimals (3.0 → "3");
 * an ATR tilt makes it fractional (1.49).
 */
export function selectionBadge(
	selectionScore: number | null | undefined,
	layer4: number | null | undefined
): string {
	let v: number | null = null;
	if (Number.isFinite(selectionScore)) v = selectionScore as number;
	else if (Number.isFinite(layer4)) v = layer4 as number;
	if (v === null) return '—';
	return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

// Acronyms the generic underscore→space rule would mangle. The card uppercases
// the label via CSS, so these are stored in the form they should READ as.
const CATALYST_LABELS: Record<string, string> = { m_and_a: 'M&A', ipo: 'IPO' };

/**
 * Humanise a raw `catalyst_event_type` enum for display: `m_and_a` → "M&A",
 * `ipo` → "IPO", otherwise replace underscores with spaces (`product_launch` →
 * "product launch"; the card's CSS uppercases it). Empty/absent → null so the
 * caller drops the " · <type>" suffix entirely.
 */
export function catalystLabel(eventType: string | null | undefined): string | null {
	if (!eventType) return null;
	return CATALYST_LABELS[eventType] ?? eventType.replaceAll('_', ' ');
}

/**
 * The `brief_error_kind` the pipeline uses when it WITHHELD prose it had
 * already generated, rather than failing to generate any.
 */
const WITHHELD_KIND = 'unsupported_benefit_claim';

/**
 * True when the row's prose was withheld by the support guard.
 *
 * The withhold reuses the graceful-degradation path (`brief_status:
 * "unavailable"`), so this kind is the only thing separating "the model failed"
 * from "the model succeeded and the pipeline suppressed the text". The card
 * needs the distinction twice: for the label, and to suppress the stage-A
 * `rationale` fallback. Falling back to `rationale` here would promote prose
 * the guard never scanned into the slot the guarded sentence just vacated —
 * and on a misrouted row the rationale IS the manufactured theme link.
 */
export function proseWithheld(errorKind: string | null | undefined): boolean {
	return errorKind === WITHHELD_KIND;
}

/**
 * The honest "brief unavailable" label for the catalyst.event blockquote
 * (#921). Returns the tone-neutral label ONLY when `brief_status` is
 * `"unavailable"`. For `"ok"` and for null/undefined (legacy pre-feature rows)
 * it returns null so the card renders byte-identical to the pre-#921
 * behaviour. Deliberately verdict-free wording — the status is a pipeline
 * fact, not a signal.
 *
 * Two shapes, because two different things happen:
 * - the LLM never produced a usable brief: "generation failed", with the
 *   terminal `brief_error_kind` in parentheses (e.g. "(truncated)");
 * - the support guard withheld a brief that WAS produced: saying "generation
 *   failed" there is factually wrong, and it would be wrong on exactly the
 *   honest-uncertainty rows this display exists to keep visible.
 */
export function briefUnavailableLabel(
	status: string | null | undefined,
	errorKind: string | null | undefined
): string | null {
	if (status !== 'unavailable') return null;
	if (proseWithheld(errorKind)) {
		return 'brief withheld — the wording asserted support the evidence record does not carry';
	}
	const base = 'brief unavailable — generation failed';
	return errorKind ? `${base} (${errorKind})` : base;
}

/**
 * The one sentence that bounds what the causal-support scale claims (#1069).
 *
 * Mirrored VERBATIM from the pipeline, where it is single-sourced into the
 * assessor prompt as
 * ``channel_assessor.CAUSAL_SUPPORT_NOT_A_FORECAST`` — so the sentence the model
 * is held to and the sentence the reader is shown cannot drift apart. The
 * research suite pins the two copies against each other
 * (``test_channel_not_a_forecast_string_matches_spa``); change one and CI names
 * the other.
 */
export const CAUSAL_SUPPORT_NOT_A_FORECAST =
	'Causal support describes how well the event text supports a mechanism; ' +
	'it is not a forecast of the share price.';

/** Quiet = the measurement worked; flag = it did not, or the guard had to act. */
export type ChannelEmphasis = 'quiet' | 'flag';

/** Structural, not `Pick<Candidate, …>`: format.ts imports nothing by design. */
export interface ChannelRecordSource {
	brief_causal_support: string | null;
	brief_channel_grounding: string | null;
	brief_support_guard_status: string | null;
}

export interface ChannelRecordDisplay {
	emphasis: ChannelEmphasis;
	/** Plain-language evidence status — the line the card shows at rest. */
	headline: string;
	/** Raw pipeline tokens, kept for the drawer + the audit trail. */
	support: string;
	grounding: string;
	/** Why the measurement did not apply; null when the row is grounded. */
	groundingNote: string | null;
	/** What the prose guard did; null when it never applied or found nothing. */
	guardNote: string | null;
}

// Evidence-status framing, deliberately NOT a confidence framing: every line
// describes what the EVENT TEXT does, never how likely anything is. "implies"
// vs "does not state" is the whole distinction between the two middle levels,
// so the words carry it — a reader must not have to decode a colour.
const SUPPORT_HEADLINES: Record<string, string> = {
	established: 'the event states a link to this company',
	suggestive: 'the event implies a link to this company',
	not_established: 'the event does not state a link to this company',
	no_record: 'the link was not assessed'
};

// A grounding failure is a PIPELINE defect, not a finding about the company, and
// the wording says so — the reader should discount the thesis and the operator
// should be able to act on it.
const GROUNDING_NOTES: Record<string, string> = {
	theme_misroute: 'the event does not concern this theme — a routing defect upstream',
	candidate_misfit: 'the event does not involve this company',
	unknown: 'grounding was not assessed'
};

// `withheld` is deliberately ABSENT. That outcome always ships with
// brief_status "unavailable" + brief_error_kind "unsupported_benefit_claim"
// (the orchestrator sets all three from the same terminal kind), and
// briefUnavailableLabel already states it in the blockquote — a second sentence
// saying the same thing two lines below reads as two separate problems.
//
// `fired_unrecovered` IS here: there the row's brief_error_kind names the LLM's
// own failure (truncated, transport), so nothing else on the card would ever
// mention that the guard also fired.
const GUARD_NOTES: Record<string, string> = {
	fired_unrecovered:
		'a draft asserted support the record does not carry; no compliant rewrite was produced',
	repaired: 'a first draft asserted support the record does not carry and was rewritten'
};

// Guard outcomes that mean the reader is looking at (or missing) text the guard
// had to act on. `repaired` is absent ON PURPOSE: the shipped sentence is the
// compliant rewrite, so the note belongs in the record but the alarm does not.
const GUARD_FLAGS = new Set(['withheld', 'fired_unrecovered']);

const GROUNDED = 'grounded';
const NO_RECORD = 'no_record';

/**
 * Project a candidate's causal-support record into what the card renders.
 *
 * Returns null for a row written before the assessor existed (NULL status) —
 * such a row must render nothing, because "not assessed" is itself a claim and
 * the instrument never looked at it.
 *
 * Emphasis is QUIET by default, including for `not_established`. That level is
 * the instrument working correctly and reporting a weak link — the honest
 * uncertainty the #1068 split exists to keep separate from a broken
 * measurement. Emphasis is reserved for a measurement that failed (grounding is
 * not `grounded`, or nothing was assessed) and for prose the guard withheld.
 */
export function channelRecord(c: ChannelRecordSource): ChannelRecordDisplay | null {
	const support = c.brief_causal_support;
	if (!support) return null;

	const grounding = c.brief_channel_grounding || '';
	const guard = c.brief_support_guard_status || '';

	// An unrecognised token (a pipeline vocabulary change the SPA has not caught
	// up with) degrades to "not assessed" rather than to silence: showing the
	// reader nothing would be indistinguishable from a grounded, established row.
	const headline = SUPPORT_HEADLINES[support] ?? SUPPORT_HEADLINES[NO_RECORD];
	const groundingNote = grounding === GROUNDED ? null : (GROUNDING_NOTES[grounding] ?? null);
	const measurementFailed = grounding !== GROUNDED || support === NO_RECORD;

	return {
		emphasis: measurementFailed || GUARD_FLAGS.has(guard) ? 'flag' : 'quiet',
		headline,
		support,
		grounding,
		groundingNote,
		guardNote: GUARD_NOTES[guard] ?? null
	};
}
