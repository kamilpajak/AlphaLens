export function fmtUsdCompact(value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '—';
	const abs = Math.abs(value);
	if (abs >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
	if (abs >= 1e6) return `$${(value / 1e6).toFixed(1)}M`;
	if (abs >= 1e3) return `$${(value / 1e3).toFixed(0)}k`;
	return `$${value.toFixed(0)}`;
}

export function fmtPct(value: number | null | undefined, digits = 1): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '—';
	const sign = value >= 0 ? '+' : '';
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

export function confidenceColor(conf: number | null | undefined): string {
	if (conf === null || conf === undefined) return 'text-fg-muted';
	if (conf >= 0.8) return 'text-green';
	if (conf >= 0.6) return 'text-amber';
	if (conf >= 0.4) return 'text-cyan';
	return 'text-fg-muted';
}

export function technicalsTrend(slope: number | null | undefined): 'up' | 'down' | 'flat' {
	if (slope === null || slope === undefined || !Number.isFinite(slope)) return 'flat';
	if (slope > 0.05) return 'up';
	if (slope < -0.05) return 'down';
	return 'flat';
}
