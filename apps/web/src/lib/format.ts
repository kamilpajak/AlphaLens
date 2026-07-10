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

// The disagreement bands over the RAW expert_spread (0-100). UNVALIDATED, hand-
// chosen cutoffs — used ONLY inside the opened drawer with a visible "not a
// buy/avoid signal" label, NEVER on the resting card face. The cutoffs are folded
// into panel_config_version; the deferred Expert×EDGE study correlates the raw
// scalar, never the bucket. consensusBand returns the descriptive word, consensusTone
// the colour. `null`/non-finite -> 'muted' / '—' (no band).
export type ConsensusTone = 'green' | 'amber' | 'red' | 'muted';

export function consensusTone(spread: number | null | undefined): ConsensusTone {
	if (spread == null || !Number.isFinite(spread)) return 'muted';
	if (spread < 20) return 'green';
	if (spread < 50) return 'amber';
	return 'red';
}

export function consensusBand(spread: number | null | undefined): string {
	if (spread == null || !Number.isFinite(spread)) return '—';
	if (spread < 20) return 'consensus';
	if (spread < 50) return 'mixed';
	return 'split';
}

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
