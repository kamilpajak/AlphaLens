// Gate-badge ordering. The card renders each candidate's gate badges in a FIXED
// per-gate slot order (below) rather than grouping them passed → failed →
// unknown. A gate therefore keeps the same position across cards no matter its
// outcome — only the GatePill colour / ✓ ✗ ? symbol signals pass/fail/unknown.

export type GateStatus = 'passed' | 'failed' | 'unknown';

export interface OrderedGate {
	name: string;
	status: GateStatus;
}

// Canonical left-to-right slot order: the pipeline's gate-evaluation sequence
// (orchestrator `GATE_NAMES` = tenk, press, insider) plus the designed-but-
// unwired `etf` gate so it already has a home if it is ever turned on.
export const GATE_ORDER = ['tenk', 'press', 'insider', 'etf'] as const;

interface GateArrays {
	gates_passed: string[];
	gates_failed: string[];
	gates_unknown: string[];
}

/**
 * Flatten a candidate's three status arrays into a single list ordered by
 * {@link GATE_ORDER}. Known gates come first in fixed slot order; any gate name
 * not in `GATE_ORDER` (a future / unrecognised gate) is appended afterwards in
 * passed → failed → unknown order so it is surfaced rather than silently
 * dropped. A gate absent from all three arrays (never evaluated) is omitted.
 */
export function orderedGates(c: GateArrays): OrderedGate[] {
	const status = new Map<string, GateStatus>();
	// First-wins: a gate lives in exactly one array in practice; if a bad payload
	// listed it twice, the earliest status (passed > failed > unknown) is kept so
	// the result stays deterministic.
	for (const g of c.gates_passed) if (!status.has(g)) status.set(g, 'passed');
	for (const g of c.gates_failed) if (!status.has(g)) status.set(g, 'failed');
	for (const g of c.gates_unknown) if (!status.has(g)) status.set(g, 'unknown');

	const out: OrderedGate[] = [];
	const seen = new Set<string>();
	for (const name of GATE_ORDER) {
		const s = status.get(name);
		if (s !== undefined) {
			out.push({ name, status: s });
			seen.add(name);
		}
	}
	// Map preserves insertion order, so leftovers come in passed → failed →
	// unknown then array order — stable and never dropped.
	for (const [name, s] of status) {
		if (!seen.has(name)) out.push({ name, status: s });
	}
	return out;
}
