// Plain-English glossary for the ladder-classification badges shown on /edge.
//
// Each `ladder_classification` value the population monitor can emit gets one
// entry here, so (a) the collapsible status legend and (b) the per-badge
// ChipTip tooltips read from a single source of truth. The `group` field drives
// the legend's section split (ongoing vs terminal vs not-measurable); `short`
// is the compact legend gloss, `body` the fuller tooltip sentence.
//
// The set of codes mirrors `ladder_replay._classify` + the `LadderOutcome`
// status values (TP_FULL / PARTIAL_TP_THEN_SL / SL_HIT / TIME_STOP /
// PARTIAL_TP_OPEN / OPEN / NO_FILL / BAD_GEOMETRY / NO_STRUCTURE / NO_DATA).
// `tests/unit/ladderStatus.test.ts` pins that every one of those is covered.

export type LadderGroup = 'ongoing' | 'terminal' | 'unmeasurable';

export interface LadderStatusEntry {
	/** The exact `ladder_classification` value from the API (upper-case). */
	code: string;
	/** Compact legend gloss (a few words). */
	short: string;
	/** Fuller one-sentence tooltip body. */
	body: string;
	/** Legend section the status belongs to. */
	group: LadderGroup;
}

// Order here is the order the legend renders within each group.
export const LADDER_STATUS: readonly LadderStatusEntry[] = [
	// --- Ongoing (position still open at the last close) ---------------------
	{
		code: 'OPEN',
		short: 'running, nothing hit yet',
		body: 'Entered, still running — neither a profit target nor the stop has been hit yet.',
		group: 'ongoing'
	},
	{
		code: 'PARTIAL_TP_OPEN',
		short: 'took some profit, still running',
		body: 'Hit at least one profit target; the remaining position is still running.',
		group: 'ongoing'
	},
	// --- Terminal (position fully resolved) ----------------------------------
	{
		code: 'TP_FULL',
		short: 'hit all targets (win)',
		body: 'Hit every profit target — the position closed fully in profit.',
		group: 'terminal'
	},
	{
		code: 'PARTIAL_TP_THEN_SL',
		short: 'some profit, then stopped out',
		body: 'Took some profit at a target, then the stop closed the rest of the position.',
		group: 'terminal'
	},
	{
		code: 'SL_HIT',
		short: 'stopped out (loss)',
		body: 'The stop was hit before any profit target — the position closed at a loss.',
		group: 'terminal'
	},
	{
		code: 'TIME_STOP',
		short: 'closed at hold limit',
		body: 'Neither a target nor the stop was hit within the hold window — closed at the time limit.',
		group: 'terminal'
	},
	{
		code: 'NO_FILL',
		short: 'never entered',
		body: 'The entry price was never reached within the entry window — the trade never opened.',
		group: 'terminal'
	},
	// --- Not measurable (no usable result) -----------------------------------
	{
		code: 'BAD_GEOMETRY',
		short: 'invalid setup',
		body: 'Invalid setup — the stop sits at or above the entry, so the return cannot be measured.',
		group: 'unmeasurable'
	},
	{
		code: 'NO_STRUCTURE',
		short: 'no trade plan',
		body: 'The brief had no entry / target / stop plan to evaluate.',
		group: 'unmeasurable'
	},
	{
		code: 'NO_DATA',
		short: 'no price data',
		body: 'No price history was available to replay the plan.',
		group: 'unmeasurable'
	}
];

export const LADDER_STATUS_BY_CODE: ReadonlyMap<string, LadderStatusEntry> = new Map(
	LADDER_STATUS.map((e) => [e.code, e])
);

/**
 * Tooltip body for a raw `ladder_classification` value. Case-insensitive and
 * tolerant of surrounding whitespace; returns a sensible fallback for an
 * unknown code so a badge always has a usable tooltip.
 */
export function ladderStatusBody(code: string | null | undefined): string {
	if (!code) return 'No status reported for this candidate.';
	const entry = LADDER_STATUS_BY_CODE.get(code.trim().toUpperCase());
	return entry?.body ?? `Status "${code}" — no description available.`;
}
