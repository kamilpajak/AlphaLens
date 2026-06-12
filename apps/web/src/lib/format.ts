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

export function fmtPctile(value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '—';
	return `${Math.round(value)}`;
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

export function technicalsTrend(slope: number | null | undefined): 'up' | 'down' | 'flat' {
	if (slope === null || slope === undefined || !Number.isFinite(slope)) return 'flat';
	if (slope > 0.05) return 'up';
	if (slope < -0.05) return 'down';
	return 'flat';
}
