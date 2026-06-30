#!/usr/bin/env node
// Tooltip-policy audit for src/routes/experiments/+page.svelte.
//
// Reads the shared GLOSSARY ($lib/data/glossary.ts) as the single source of
// truth. Exits non-zero on any of:
//   1. orphan [term] markup (term not in GLOSSARY)
//   2. definition drift (inline JargonTip body= prop differs from glossary)
//   3. glossary entry with zero inline references anywhere on the page
//   4. ALWAYS-category term with unwrapped occurrence in non-story data text
//      (policy: every occurrence must be wrapped)
//   5. FIRST-per-section term over-wrapped (>1 wrap in same section)
//      (policy: wrap first occurrence per section only)
//
// Sections are: each paradigm row entry, each pattern entry, the architecture
// layers block, status legend, how.to.read block, header, infrastructure.live,
// methodology.artifacts. Glossary section is excluded from policy enforcement.
//
// Run via `pnpm run audit:tooltips`.

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(__dirname, '..', 'src', 'routes', 'experiments', '+page.svelte');
const LEDGER_TS = resolve(__dirname, '..', 'src', 'lib', 'data', 'research-ledger.ts');
const GLOSSARY_TS = resolve(__dirname, '..', 'src', 'lib', 'data', 'glossary.ts');

// The paradigm / pattern / status-legend data arrays (which carry the `[term]`
// markup and the `audit-tooltips:dynamic-terms` hint) were extracted to
// `$lib/data/research-ledger.ts`; the JSX template + inline JargonTip prose
// stays in the route. Scan BOTH as one buffer: the ledger portion supplies the
// data-field markup + the dynamic hint, the svelte portion supplies the JSX
// JargonTip wraps. `sectionForLine` walks backward over the combined buffer, so
// its `id:` / `n:` / `status:` anchors (ledger) and `<section>` anchors (svelte)
// both resolve correctly.
const ledgerSrc = readFileSync(LEDGER_TS, 'utf-8');
const svelteSrc = readFileSync(SRC, 'utf-8');
const src = `${ledgerSrc}\n${svelteSrc}`;
const glossarySrc = readFileSync(GLOSSARY_TS, 'utf-8');

// ---------- Parse glossary ----------
const glossaryEntries = new Map();
{
	// Match {...category: 'X'} as before, then optionally capture a following
	// pages: [...] array. Terms with pages NOT including 'experiments' are
	// scoped to other routes (e.g. /brief/[date]) and the unreferenced-check
	// below skips them — this audit script scans the /experiments page only.
	// An optional `formula: '...'` line may sit between `full:` and `body:`
	// (glossary ratio terms point at a src/lib/formulas.json key); it is
	// non-capturing so the term/full/body/category capture indices are unchanged.
	const entryRe =
		/\{\s*\n\s*term:\s*'([^']+)',\s*\n\s*full:\s*'([^']*)',\s*\n\s*(?:formula:\s*'[^']*',\s*\n\s*)?body:\s*'((?:[^'\\]|\\.)*)'[\s,\n]*category:\s*'(always|first-per-section)'([^}]*)/g;
	let m;
	while ((m = entryRe.exec(glossarySrc)) !== null) {
		const trailing = m[5] ?? '';
		const pagesMatch = trailing.match(/pages:\s*\[([^\]]+)\]/);
		let pages = ['experiments'];
		if (pagesMatch) {
			pages = pagesMatch[1]
				.split(',')
				.map((s) => s.trim().replace(/^['"]|['"]$/g, ''))
				.filter(Boolean);
		}
		glossaryEntries.set(m[1], { full: m[2], body: m[3], category: m[4], pages });
	}
}
const glossaryKeys = new Set(glossaryEntries.keys());
const alwaysTerms = new Set(
	[...glossaryEntries.entries()].filter(([, v]) => v.category === 'always').map(([k]) => k)
);
const experimentsScopedTerms = new Set(
	[...glossaryEntries.entries()]
		.filter(([, v]) => v.pages.includes('experiments'))
		.map(([k]) => k)
);

// ---------- Identify sections (line-range based) ----------
// Sections give a coarse boundary for over-wrap detection. Strategy:
//   - Each line containing `id: 'P##'` or `id: 'R##'` or `id: 'S##'` starts a
//     paradigm section that extends until the next sibling entry or `]`.
//   - Each line containing `n: 'XX',` (in patterns array) starts a pattern
//     section.
//   - For JSX template (post-</script>): each `<section class=...>` or
//     `<details>` opens a section; ID by section title text.
//
// A simpler scheme — for each line in the file, identifySectionForLine returns
// the section id. We walk backwards looking for the nearest section anchor.
const lines = src.split('\n');

function sectionForLine(lineIndex) {
	for (let i = lineIndex; i >= 0; i--) {
		const line = lines[i];
		// Data entry anchors:
		const idMatch = line.match(/\bid:\s*'(P\d\d|R\d\d|S\d\d|L\d)'/);
		if (idMatch) return `entry-${idMatch[1]}`;
		const nMatch = line.match(/\bn:\s*'(\d\d)'/);
		if (nMatch) return `pattern-${nMatch[1]}`;
		// statusLegend entries — each row is its own section so a term cited in
		// one definition doesn't bleed into the over-wrap count for adjacent
		// definitions. The `status:` field also appears in paradigm rows where
		// `id:` is the canonical anchor — peek back a few lines for an `id:` to
		// disambiguate (paradigm row → use id, standalone status row → use
		// status).
		const statusMatch = line.match(/\bstatus:\s*'(FAIL|SLIPPAGE-FAIL|IN-FLIGHT|INCONCLUSIVE|PASS_MARGINAL)'/);
		if (statusMatch) {
			for (let k = i - 1; k >= Math.max(0, i - 12); k--) {
				const idPeek = lines[k].match(/\bid:\s*'(P\d\d|R\d\d|S\d\d|L\d)'/);
				if (idPeek) return `entry-${idPeek[1]}`;
				// Stop peek at the row-opening brace so we don't bleed into the
				// previous row. Anchor to start-of-line so a stray `{` inside a
				// string literal (story / mechanism / lesson) doesn't terminate
				// the peek prematurely.
				if (/^\s*\{/.test(lines[k])) break;
			}
			return `status-${statusMatch[1]}`;
		}
		// JSX section anchors — find <section ...> or <details ...>
		if (line.match(/<section\b/) || line.match(/<details\b/)) {
			// Look ahead a few lines for the section title text inside the header
			for (let j = i; j < Math.min(i + 12, lines.length); j++) {
				const titleMatch = lines[j].match(/>([a-z][a-z.]+)<\/span>/);
				if (titleMatch) return `jsx-${titleMatch[1]}`;
			}
			return `jsx-section-L${i + 1}`;
		}
		if (line.match(/<header\b/)) {
			return 'jsx-header';
		}
	}
	return 'unknown';
}

// ---------- Find ALL wraps (data markup + JSX) ----------
const wraps = []; // [{ term, line, source: 'markup'|'jsx', section }]
const orphans = [];

// Data string fields (excluding story per policy)
const dataFieldRe =
	/\b(story|hypothesis|mechanism|metric|lesson|name|body|what|definition):\s*'((?:[^'\\]|\\.)*)'/g;
const markupRe = /\[([^|\]]+)(?:\|[^\]]+)?\]/g;
let m;
while ((m = dataFieldRe.exec(src)) !== null) {
	const field = m[1];
	const text = m[2];
	const fieldOffset = m.index + m[0].indexOf(m[2]);
	if (field === 'story') continue;
	markupRe.lastIndex = 0;
	let mm;
	while ((mm = markupRe.exec(text)) !== null) {
		const term = mm[1];
		const absOffset = fieldOffset + mm.index;
		const lineNo = src.slice(0, absOffset).split('\n').length;
		if (!glossaryKeys.has(term)) {
			orphans.push({ line: lineNo, field, term });
		} else {
			wraps.push({
				term,
				line: lineNo,
				source: 'markup',
				section: sectionForLine(lineNo - 1)
			});
		}
	}
}

// JSX JargonTip wraps
const jsxJargonRe = /<JargonTip\s+(?:term="([^"]+)"|\{\.\.\.tipProps\('([^']+)'\)\})/g;
let jm;
while ((jm = jsxJargonRe.exec(src)) !== null) {
	const term = jm[1] || jm[2];
	const lineNo = src.slice(0, jm.index).split('\n').length;
	if (!glossaryKeys.has(term)) {
		orphans.push({ line: lineNo, field: 'JSX', term });
	} else {
		wraps.push({
			term,
			line: lineNo,
			source: 'jsx',
			section: sectionForLine(lineNo - 1)
		});
	}
}

// Dynamic-term hints. Paradigm headers render
//   <JargonTip {...tipProps(VAR)}>...</JargonTip>
// where VAR is a Svelte expression (e.g. `p.layer_id`, `p.axis_a`, `b` from
// an each-block). The literal-only `jsxJargonRe` above can't credit these
// uses, so authors declare them via a comment of the form:
//   // audit-tooltips:dynamic-terms TERM1 TERM2 ...
// Each listed term is credited with one reference at the comment's line for
// the "unreferenced terms" check. Over-wrap detection still runs from the
// literal/markup wraps (each paradigm header is its own section, so the
// dynamic L2/L4/axis tooltips can never over-wrap by construction).
const dynamicHintRe = /\/\/\s*audit-tooltips:dynamic-terms\s+([^\n]+)/g;
let dm;
while ((dm = dynamicHintRe.exec(src)) !== null) {
	const lineNo = src.slice(0, dm.index).split('\n').length;
	const terms = dm[1].trim().split(/\s+/);
	for (const term of terms) {
		if (!glossaryKeys.has(term)) {
			orphans.push({ line: lineNo, field: 'dynamic-hint', term });
		} else {
			wraps.push({
				term,
				line: lineNo,
				source: 'dynamic',
				section: `dynamic-hint-L${lineNo}`
			});
		}
	}
}

// ---------- Find unwrapped occurrences (ALWAYS terms only) ----------
// For each ALWAYS term, scan data text fields (non-story) for occurrences not
// inside [term] markup. Also scan JSX prose text outside JargonTip elements.
const unwrappedAlways = []; // [{ term, line, field, context }]

dataFieldRe.lastIndex = 0;
while ((m = dataFieldRe.exec(src)) !== null) {
	const field = m[1];
	if (field === 'story') continue;
	const text = m[2];
	const fieldOffset = m.index + m[0].indexOf(m[2]);
	// Build a mask of positions covered by [term] markup → mark those positions
	const masked = text.split('');
	markupRe.lastIndex = 0;
	let mk;
	while ((mk = markupRe.exec(text)) !== null) {
		for (let i = mk.index; i < markupRe.lastIndex; i++) masked[i] = '\0';
	}
	const cleanText = masked.join('');
	for (const term of alwaysTerms) {
		const pat = wordPatternFor(term);
		let occ;
		while ((occ = pat.exec(cleanText)) !== null) {
			const absOffset = fieldOffset + occ.index;
			const lineNo = src.slice(0, absOffset).split('\n').length;
			unwrappedAlways.push({
				term,
				line: lineNo,
				field,
				context: text.slice(Math.max(0, occ.index - 20), occ.index + 30)
			});
		}
	}
}

// JSX template — strip <JargonTip ...>...</JargonTip> blocks then scan
const scriptEnd = src.indexOf('</script>');
const jsxText = scriptEnd > -1 ? src.slice(scriptEnd) : '';
// Strip JargonTip blocks, HTML comments, and any data-term="..." attributes
// (those carry the term verbatim but are tooltip metadata, not visible prose).
// Iterate each replace until stable — a single .replace() pass can leave a
// dangerous sequence (e.g. `<!--<!---->` collapses to `<!--` after one pass)
// which CodeQL flags as js/incomplete-multi-character-sanitization. This is
// a dev-only build script over trusted repo source, but cheap to fix right.
function stripUntilStable(text, pattern) {
	let prev;
	let curr = text;
	do {
		prev = curr;
		curr = curr.replace(pattern, '');
	} while (curr !== prev);
	return curr;
}
const jsxStripped = stripUntilStable(
	stripUntilStable(stripUntilStable(jsxText, /<JargonTip[\s\S]*?<\/JargonTip>/g), /<!--[\s\S]*?-->/g),
	/data-term="[^"]*"/g
);
// Skip glossary section <dl>...</dl> when scanning unwrapped (terms inside
// glossary `term` display are intentional — not "unwrapped" in policy sense).
// Locate glossary section by its header "glossary.terms" anchor.
const glossarySectionStart = jsxStripped.indexOf('glossary.terms');
const jsxScanText =
	glossarySectionStart > -1
		? jsxStripped.slice(0, glossarySectionStart) // exclude glossary section onwards
		: jsxStripped;
for (const term of alwaysTerms) {
	const pat = wordPatternFor(term);
	let occ;
	while ((occ = pat.exec(jsxScanText)) !== null) {
		// Compute approximate line in original src by counting newlines up to
		// the relative position in original JSX text (without strip).
		// Heuristic: find approximate match position in jsxText
		const approxIdx = jsxText.indexOf(occ[0]);
		const absOffset = scriptEnd + (approxIdx >= 0 ? approxIdx : 0);
		const lineNo = src.slice(0, absOffset).split('\n').length;
		// Skip occurrences inside data-term=" attribute (those are tooltip data)
		// or in markdown comments
		const surrounding = jsxScanText.slice(Math.max(0, occ.index - 30), occ.index + 30);
		if (surrounding.includes('data-term=') || surrounding.includes('//')) continue;
		unwrappedAlways.push({
			term,
			line: lineNo,
			field: 'JSX template',
			context: surrounding
		});
	}
}

function wordPatternFor(term) {
	// Build a regex that matches `term` as a whole token. For `αt` use literal
	// (Greek + ASCII boundary); for ASCII tokens use word boundaries.
	// The boundary classes include `-` so an acronym embedded in a hyphenated
	// identifier (e.g. `IS` inside the RunPod region code `EUR-IS-1`, or `FL`
	// inside `IN-FLIGHT`) is NOT mistaken for a bare jargon occurrence — those
	// are tokens, not the standalone term that policy asks authors to wrap.
	const esc = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	if (/^[A-Za-z][A-Za-z0-9]*$/.test(term)) {
		return new RegExp(`(?<![A-Za-z0-9_-])${esc}(?![A-Za-z0-9_-])`, 'g');
	}
	// αt and similar: rely on negative lookarounds for letters
	return new RegExp(`(?<![A-Za-z\\[-])${esc}(?![A-Za-z\\]-])`, 'g');
}

// ---------- Definition drift ----------
const drifts = [];
const inlineBodyRe = /<JargonTip\s+term="([^"]+)"[^>]*body="([^"]*)"/g;
let bm;
while ((bm = inlineBodyRe.exec(src)) !== null) {
	const term = bm[1];
	const body = bm[2];
	const glossary = glossaryEntries.get(term);
	if (glossary && glossary.body !== body) {
		drifts.push({ term, inline: body.slice(0, 70), glossary: glossary.body.slice(0, 70) });
	}
}

// ---------- Reference counts ----------
const refCounts = new Map();
for (const w of wraps) {
	refCounts.set(w.term, (refCounts.get(w.term) ?? 0) + 1);
}
// Only fail unreferenced for terms scoped to /experiments — brief-only terms
// (e.g. PE, PS, EV/EBITDA) live in CandidateCard.svelte which this script
// doesn't scan. The auto-discovery smoke tests cover those per-page.
const unreferenced = [...experimentsScopedTerms].filter((t) => (refCounts.get(t) ?? 0) === 0);

// ---------- Over-wrap detection for FIRST-per-section ----------
// Group wraps by (term, section). For each FIRST-per-section term with >1 wrap
// in same section, flag as over-wrap.
const overWraps = []; // [{ term, section, count, lines }]
const sectionTermWraps = new Map(); // term::section → [line, ...]
for (const w of wraps) {
	const cat = glossaryEntries.get(w.term)?.category;
	if (cat !== 'first-per-section') continue;
	const key = `${w.term}::${w.section}`;
	if (!sectionTermWraps.has(key)) sectionTermWraps.set(key, []);
	sectionTermWraps.get(key).push(w.line);
}
for (const [key, lines2] of sectionTermWraps.entries()) {
	if (lines2.length > 1) {
		const [term, section] = key.split('::');
		overWraps.push({ term, section, count: lines2.length, lines: lines2 });
	}
}

// ---------- Report ----------
let exitCode = 0;
const totalRefs = [...refCounts.values()].reduce((a, b) => a + b, 0);
console.log(`\nTooltip audit for /experiments\n${'='.repeat(60)}`);
console.log(`Glossary: ${glossaryKeys.size} terms (${alwaysTerms.size} ALWAYS, ${glossaryKeys.size - alwaysTerms.size} FIRST-per-section)`);
console.log(`Inline references found: ${totalRefs}\n`);

function reportCheck(label, items, formatter) {
	if (items.length === 0) {
		console.log(`✓ ${label}`);
		return;
	}
	console.log(`✗ ${label} (${items.length}):`);
	for (const it of items) console.log(`    ${formatter(it)}`);
	console.log();
	exitCode = 1;
}

reportCheck('no orphan markup', orphans, (o) => `L${o.line} ${o.field}: [${o.term}]`);
reportCheck(
	'no definition drift',
	drifts,
	(d) => `${d.term}: inline=«${d.inline}…» vs glossary=«${d.glossary}…»`
);
reportCheck(
	'every glossary term has ≥1 inline reference',
	unreferenced.map((t) => ({ t })),
	(o) => `unreferenced: ${o.t}`
);
reportCheck(
	'ALWAYS terms — no unwrapped occurrences',
	unwrappedAlways,
	(u) => `L${u.line} ${u.field}: ${u.term} unwrapped — …${u.context.replace(/\n/g, ' ')}…`
);
reportCheck(
	'FIRST-per-section terms — no over-wrap',
	overWraps,
	(o) => `${o.term} wrapped ${o.count}× in ${o.section} (lines ${o.lines.join(', ')})`
);

console.log(`\nPer-term reference count (/experiments scan only):`);
for (const t of [...glossaryKeys].sort()) {
	const entry = glossaryEntries.get(t);
	const n = refCounts.get(t) ?? 0;
	const tag = entry.category === 'always' ? '[ALW]' : '[FPS]';
	const scope = entry.pages.includes('experiments') ? '' : ` (page-scope: ${entry.pages.join(',')})`;
	// Brief-only terms never count against /experiments, so 0 is expected.
	const required = entry.pages.includes('experiments');
	const mark = !required ? '·' : n === 0 ? '✗' : '✓';
	console.log(`  ${mark} ${tag} ${t}: ${n}${scope}`);
}

process.exit(exitCode);
