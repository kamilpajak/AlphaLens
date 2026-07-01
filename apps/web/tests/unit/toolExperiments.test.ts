import { describe, expect, it } from 'vitest';
import {
	toolExperiments,
	toolStatusLegend,
	type ToolStatus
} from '../../src/lib/data/research-ledger';

// Pins the shape of the /experiments "tool.experiments" ledger — the sibling
// section for live-tool selection/exit tuning (distinct from the paradigm
// ledger). Every row's status must be defined in the legend, ids/displays must
// be unique, evidence must be null or a .md path (the Playwright smoke test
// asserts each rendered evidence file actually resolves 200), and the reader-
// facing prose fields must be present so no card renders half-empty.

describe('tool.experiments ledger data', () => {
	it('has rows and a legend', () => {
		expect(toolExperiments.length).toBeGreaterThan(0);
		expect(toolStatusLegend.length).toBeGreaterThan(0);
	});

	it('legend statuses are unique', () => {
		const seen = toolStatusLegend.map((s) => s.status);
		expect(new Set(seen).size).toBe(seen.length);
	});

	it('every row status is defined in the legend', () => {
		const defined = new Set<ToolStatus>(toolStatusLegend.map((s) => s.status));
		for (const r of toolExperiments) {
			expect(defined.has(r.status)).toBe(true);
		}
	});

	it('row ids and display labels are unique', () => {
		const ids = toolExperiments.map((r) => r.id);
		const displays = toolExperiments.map((r) => r.display);
		expect(new Set(ids).size).toBe(ids.length);
		expect(new Set(displays).size).toBe(displays.length);
	});

	it('reader-facing fields are present and non-empty', () => {
		for (const r of toolExperiments) {
			for (const f of [
				r.id,
				r.display,
				r.name,
				r.metric,
				r.date,
				r.hypothesis,
				r.mechanism,
				r.outcome,
				r.lesson
			]) {
				expect(typeof f).toBe('string');
				expect(f.trim().length).toBeGreaterThan(0);
			}
			expect(Array.isArray(r.prs)).toBe(true);
		}
	});

	it('evidence is null or a .md path', () => {
		for (const r of toolExperiments) {
			if (r.evidence !== null) {
				expect(r.evidence.endsWith('.md')).toBe(true);
			}
		}
	});
});
