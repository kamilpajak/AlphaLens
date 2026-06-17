import { describe, expect, it } from 'vitest';
import { renderTex } from '../../src/lib/temmlRender.js';
import formulas from '../../src/lib/formulas.json';
import { GLOSSARY } from '../../src/lib/data/glossary';

// renderTex is the build-time LaTeX → MathML helper the virtual:formulas Vite
// plugin runs in Node. The browser only ever sees its pre-rendered output, so
// these tests guard the actual shipped strings: structure, an injection floor,
// and that every formula in the registry typesets without an error node (the
// red `color:#b22222` fallback Temml emits on a parse failure).

describe('renderTex', () => {
	it('renders a fraction to MathML', () => {
		const html = renderTex('\\dfrac{P}{V}');
		expect(html).toContain('<math');
		expect(html).toContain('mfrac');
	});

	it('keeps subscripts as MathML (msub)', () => {
		expect(renderTex('V_{i}')).toContain('msub');
	});

	it('resolves multi-letter control words (not just the first letter)', () => {
		// The esbuild-mangled browser build truncated \alpha to an error \a;
		// the Node helper must keep the whole command and emit the α glyph.
		const html = renderTex('\\alpha');
		expect(html).toContain('α');
		expect(html).not.toContain('b22222');
	});

	it('emits no <script> tag or inline event handler', () => {
		const html = renderTex('1 - P / V_{i}');
		expect(html).not.toContain('<script');
		expect(html).not.toMatch(/\son\w+=/i);
	});

	it('does not throw on a malformed formula (throwOnError:false)', () => {
		expect(() => renderTex('\\dfrac{')).not.toThrow();
	});

	it('display mode marks the math block as display', () => {
		expect(renderTex('x', true)).toContain('display="block"');
	});
});

describe('formulas.json registry', () => {
	it('has at least one formula', () => {
		expect(Object.keys(formulas).length).toBeGreaterThan(0);
	});

	for (const [name, tex] of Object.entries(formulas as Record<string, string>)) {
		it(`"${name}" typesets to MathML with no error node`, () => {
			const html = renderTex(tex);
			expect(html).toContain('<math');
			// Temml colours a failed parse #b22222 — none of the shipped formulas
			// may regress into that fallback.
			expect(html).not.toContain('b22222');
		});
	}
});

describe('glossary formula references', () => {
	const referenced = GLOSSARY.filter((e) => e.formula);

	it('at least one glossary term carries a formula', () => {
		expect(referenced.length).toBeGreaterThan(0);
	});

	for (const entry of referenced) {
		it(`"${entry.term}" → formula key "${entry.formula}" exists in formulas.json`, () => {
			// A dangling reference would render a blank `= ` in the tooltip; pin it.
			expect(Object.keys(formulas)).toContain(entry.formula);
		});
	}
});

describe('glossary threshold bands', () => {
	const banded = GLOSSARY.filter((e) => e.bands);

	it('at least one glossary term carries bands', () => {
		expect(banded.length).toBeGreaterThan(0);
	});

	for (const entry of banded) {
		it(`"${entry.term}" bands are non-empty range/label pairs`, () => {
			// A blank range or label would render a confusing dangling bullet.
			expect(entry.bands!.length).toBeGreaterThan(0);
			for (const band of entry.bands!) {
				expect(band.range.trim()).not.toBe('');
				expect(band.label.trim()).not.toBe('');
			}
		});
	}
});
