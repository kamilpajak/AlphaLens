// Pure derivation/formatting helpers for the /edge dashboard. Kept out of
// the Svelte component so the arithmetic (centered excess-R bar scaling,
// classification → colour, R formatting) is unit-tested in isolation
// (tests/unit/edge.test.ts). No client-side aggregation lives here — the
// N-gate is server-side; these helpers only render what the API returns.

import { fmtPct } from './format';
import { toneClass } from './tone';
import type { EdgeStatus } from './types';

/** Tailwind palette tone for the terminal-ops language. A subset of the shared
 *  `SemanticTone` vocabulary in `./tone`. */
export type EdgeTone = 'green' | 'red' | 'amber' | 'cyan' | 'violet' | 'muted';

/**
 * Ladder-classification → chip tone, per memo §5:
 *   TP_FULL → green, SL_HIT → red, TIME_STOP → amber, OPEN → cyan,
 *   PARTIAL_TP_OPEN / PARTIAL_TP_THEN_SL → violet. Anything else (NO_FILL,
 *   BAD_GEOMETRY, NO_STRUCTURE, NO_DATA, unknown) → muted.
 * Matching is case-insensitive and tolerant of a leading/trailing space.
 */
export function classificationTone(classification: string | null | undefined): EdgeTone {
	if (!classification) return 'muted';
	const c = classification.trim().toUpperCase();
	if (c === 'TP_FULL') return 'green';
	if (c === 'SL_HIT') return 'red';
	if (c === 'TIME_STOP') return 'amber';
	if (c === 'OPEN') return 'cyan';
	if (c.startsWith('PARTIAL')) return 'violet';
	return 'muted';
}

/** A chip tone → its Tailwind `border-*` + `text-*` classes (border-first, the
 *  /edge outcomes table + ladder-legend convention). Thin wrapper over the shared
 *  `toneClass` so the /edge and /experiments palettes can never drift. */
export function toneClasses(tone: EdgeTone): string {
	return toneClass(tone, ['border', 'text']);
}

/**
 * Geometry for a CENTERED excess-R bar — zero sits in the middle, a positive
 * excess fills rightward and a negative excess fills leftward. Returns the
 * left edge and width of the coloured segment as percentages of the track
 * (0..100), so a component can render it as a single absolutely-positioned
 * div with `left` + `width`.
 *
 * `domain` is the symmetric half-range that maps to the full half-track
 * (default 1.0R → a ±1R move reaches the track edge). Values beyond the
 * domain clamp to the edge so an outlier never overflows the track.
 *
 * Null / non-finite values → a zero-width segment centered at 50% (so the
 * caller can still render an empty track).
 */
export interface ExcessBarGeometry {
	/** Left edge of the coloured segment, percent of track [0..100]. */
	left: number;
	/** Width of the coloured segment, percent of track [0..100]. */
	width: number;
	/** True when the value is positive (caller picks green vs red). */
	positive: boolean;
}

/**
 * Bar domain for terminal rows, whose value is a benchmark-EXCESS RETURN
 * (a fraction, e.g. 0.21 = +21% over the index), NOT an R-multiple. A ±25%
 * excess reaches the track edge; larger moves clamp. Ongoing rows keep the
 * default 1.0 domain because their value (`open_r`) is a true R-multiple.
 */
export const EXCESS_RETURN_BAR_DOMAIN = 0.25;

export function excessBarGeometry(
	value: number | null | undefined,
	domain = 1
): ExcessBarGeometry {
	if (value === null || value === undefined || !Number.isFinite(value) || domain <= 0) {
		return { left: 50, width: 0, positive: false };
	}
	// Fraction of the half-track the magnitude occupies, clamped to [0, 1].
	const frac = Math.min(1, Math.abs(value) / domain);
	const halfWidth = frac * 50; // half-track is 50% of the full track
	if (value >= 0) {
		return { left: 50, width: halfWidth, positive: true };
	}
	return { left: 50 - halfWidth, width: halfWidth, positive: false };
}

/**
 * Format an R-multiple with an explicit sign and an "R" suffix
 * (e.g. `+0.41R`, `-0.88R`, `0.00R`). Null / non-finite → an em dash.
 * Used for excess-R, gross-R, open-R surfaces — always carries
 * `whitespace-nowrap` at the call site (atomic-token rule).
 */
export function fmtR(value: number | null | undefined, digits = 2): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '—';
	const sign = value >= 0 ? '+' : '';
	return `${sign}${value.toFixed(digits)}R`;
}

/**
 * Format a FRACTION-of-book value (0..1 scale) as a percent string.
 *
 * The population-monitor size columns (`*_pct_of_book`, `*_risk_pct`,
 * `*_gross_weight_pct`) and the deployment fill-rate are all stored as
 * FRACTIONS (e.g. 0.0021 = 0.21%, 0.05 = 5%). `fmtPct` does NOT scale by 100,
 * so feeding it a fraction renders "+0.00%" — the % BOOK / risk% display bug.
 * This scales ×100 first, then delegates sign + digit handling to `fmtPct`.
 * Null / non-finite → an em dash.
 */
export function fmtFracPct(
	value: number | null | undefined,
	digits = 1,
	withSign = true
): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '—';
	return fmtPct(value * 100, digits, withSign);
}

/**
 * Copy for the per-name position-sizing panel. The former shared-book
 * aggregates (size-weighted R, book contribution) were removed: they assumed a
 * single fixed capital book that does NOT exist for this decision-support tool
 * — each WhatsApp-group member sizes independently. They were a vestige of the
 * decommissioned Alpaca paper-trade chain (ADR 0012). Only the per-name
 * suggested risk remains, which each member rescales by their own capital. The
 * capital-free portfolio lens (equal-weight benchmark-excess) is the separate
 * EDGE panel.
 */
export const SIZING_MODEL_RISK_LABEL = 'suggested risk per name (1% budget)';

/** Human label for the N-gate status used in panel chrome. */
export function statusLabel(status: EdgeStatus): string {
	if (status === 'insufficient') return 'insufficient data';
	if (status === 'early') return 'early · high-variance';
	return 'unlocked';
}

/** Whether the N-gated stat fields are renderable for a panel. The backend
 *  nulls them when insufficient, so this is purely a presentation switch. */
export function statsUnlocked(status: EdgeStatus): boolean {
	return status !== 'insufficient';
}
