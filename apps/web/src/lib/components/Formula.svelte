<script lang="ts">
	// Inline MathML formula, looked up by name from the build-time-rendered
	// `virtual:formulas` map (see vite.config.ts + src/lib/formulas.json). The
	// LaTeX is typeset by Temml in Node at build time, so this ships only a
	// static MathML string — no temml runtime in the browser. MathML inherits
	// the surrounding mono terminal font instead of a serif math face.
	//
	// The whitespace-nowrap wrapper keeps a formula from breaking across lines
	// mid-token (the CLAUDE.md atomic-token rule). The {@html} sink only ever
	// sees a build-time string from our own formulas.json — never user input.
	import formulas from 'virtual:formulas';

	let { name }: { name: string } = $props();

	const html = $derived(formulas[name]);
</script>

{#if html}<span class="whitespace-nowrap">{@html html}</span>{/if}
