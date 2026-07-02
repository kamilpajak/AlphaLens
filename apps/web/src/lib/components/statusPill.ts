// Pure class-composition for <StatusPill> — the shared bordered status/verdict
// pill shell (a `px-1.5 py-0.5 border … uppercase tracking-widest {tone}` span)
// that was hand-rolled at ~9 sites across /experiments, /edge, the ladder
// legend, and TemplateFacts. The `tone` (a precomputed "text-X border-X" class
// string) is supplied by the domain — this only owns the shell + variant flags.
// Unit-tested in tests/unit/statusPill.test.ts.

export interface StatusPillOpts {
	/** Precomputed "text-X border-X" (or e.g. "border-grid text-fg-muted") tone. */
	tone: string;
	/** Label font size: '10' (default, dense ledgers) or '9' (compact rows). */
	size?: '9' | '10';
	/** Add `whitespace-nowrap` (atomic token that must not wrap). */
	nowrap?: boolean;
	/** Add `border-dashed` (e.g. pending / forward-looking statuses). */
	dashed?: boolean;
	/** Add `cursor-help` (the pill carries a tooltip). */
	interactive?: boolean;
	/** Extra utilities appended last (e.g. `inline-block shrink-0`). */
	extra?: string;
}

export function statusPillClass({
	tone,
	size = '10',
	nowrap = false,
	dashed = false,
	interactive = false,
	extra = ''
}: StatusPillOpts): string {
	return [
		'px-1.5 py-0.5 border uppercase tracking-widest',
		size === '9' ? 'text-[9px]' : 'text-[10px]',
		nowrap && 'whitespace-nowrap',
		dashed && 'border-dashed',
		interactive && 'cursor-help',
		tone,
		extra
	]
		.filter(Boolean)
		.join(' ');
}
