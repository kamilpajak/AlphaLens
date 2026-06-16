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

// Synthetic display state for a placeholder row whose `ladder_classification`
// is blank/null. The candidate is plannable but has NOT been priced/replayed
// yet — its first fetch failed or (more often) was deferred by the nightly
// fetch budget, so the monitor stores an empty classification and retries it on
// a later sweep. This is NOT a pipeline classification, so it deliberately lives
// OUTSIDE `LADDER_STATUS` (and its pipeline-parity test): the UI synthesises it
// from an empty value rather than the pipeline emitting it.
export const PENDING_STATUS: LadderStatusEntry = {
	code: 'PENDING',
	short: 'not priced yet (queued)',
	body: 'Not priced yet — this candidate is plannable but its price replay is still queued (each nightly run prices a bounded number of names). It gets a real status on a later sweep.',
	group: 'ongoing'
};

/** True when the classification is a blank/null not-yet-priced placeholder. */
export function isPendingStatus(code: string | null | undefined): boolean {
	return !code?.trim();
}

/** Badge label for a raw `ladder_classification`: the value itself, or
 *  `PENDING` for a blank/null placeholder (so the badge is never empty). */
export function ladderStatusLabel(code: string | null | undefined): string {
	return isPendingStatus(code) ? PENDING_STATUS.code : code!.trim();
}

/**
 * Tooltip body for a raw `ladder_classification` value. Case-insensitive and
 * tolerant of surrounding whitespace. A blank/null value is the PENDING
 * placeholder; an unrecognised non-empty code gets a safe generic fallback so a
 * badge always has a usable tooltip.
 */
export function ladderStatusBody(code: string | null | undefined): string {
	if (isPendingStatus(code)) return PENDING_STATUS.body;
	const entry = LADDER_STATUS_BY_CODE.get(code!.trim().toUpperCase());
	return entry?.body ?? `Status "${code}" — no description available.`;
}
