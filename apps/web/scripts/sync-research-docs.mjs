#!/usr/bin/env node
// Copies the markdown/JSON evidence files referenced by /experiments into
// static/docs/research/ so the in-page Evidence drawer can fetch them.
//
// Runs as a prebuild hook (and predev). Idempotent — silently overwrites.
// If a referenced file is missing, prints a warning and continues (drawer
// will show a friendly "not found" message at runtime).

import { mkdirSync, copyFileSync, existsSync, rmSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

// Keep this list in sync with the `evidence` field in
// src/routes/experiments/+page.svelte. A test in tests/smoke.test.ts asserts
// every referenced file is reachable at runtime, so a missing entry surfaces
// loudly in CI rather than silently as a 404 in the drawer.
const REFERENCED = [
	'paradigm_failures_postmortem.md',
	'layer2b_audit_final.md',
	'tri_factor_multi_phase_verdict.md',
	'strategy_validation_playbook.md',
	'regime_gate_phase1_diagnostic.md',
	'quality_momentum_multi_phase_audit.json',
	'vol_target_overlay_multi_phase_audit.json',
	'distress_credit/phase_a_verdict_2026_05_04.md',
	'insider_pc_compound_oos_2026-05-11.json',
	'ev_fcff_yield_audit_verdict_2026_05_12.md',
	'paradigm14_pead_v2_design_2026_05_13.md',
	'idiosyncratic_momentum_audit_verdict_2026_05_14.md',
	'v9d_retrospective_pre_2018_postmortem_2026_05_05.md',
	'pc_abnormal_retrospective_pre_2018_verdict.json',
	'insider_form4_opportunistic_slippage_stress_postmortem_2026_05_12.md'
];

const __dirname = dirname(fileURLToPath(import.meta.url));
// Path layout: apps/web/scripts/ → repo root is 3 levels up. The web/ tree
// moved under apps/ in commit ca378a5 "collapse seams" but this script's
// relative path was left at the pre-refactor depth — every build hook
// silently emitted "0/15 synced" so the Evidence drawer 404'd on each
// referenced file. Pinning the correct depth restores the sync.
const SRC = resolve(__dirname, '..', '..', '..', 'docs', 'research');
const DST = resolve(__dirname, '..', 'static', 'docs', 'research');

// Wipe the destination so a deleted reference doesn't linger as dead weight
// in the bundle.
if (existsSync(DST)) {
	rmSync(DST, { recursive: true, force: true });
}
mkdirSync(DST, { recursive: true });

let copied = 0;
let missing = 0;
for (const f of REFERENCED) {
	const src = resolve(SRC, f);
	const dst = resolve(DST, f);
	if (existsSync(src)) {
		mkdirSync(dirname(dst), { recursive: true });
		copyFileSync(src, dst);
		copied++;
	} else {
		console.warn(`[sync-research-docs] missing: ${f}`);
		missing++;
	}
}
console.log(`[sync-research-docs] synced ${copied}/${REFERENCED.length} files${missing ? ` (${missing} missing)` : ''}`);
