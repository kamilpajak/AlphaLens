<script lang="ts">
	// Shared bordered section panel + its `// section.name` header row — the
	// `border border-grid bg-bg-1 fade-up` wrapper with a
	// `px-4 sm:px-5 py-3 border-b border-grid text-[10px] uppercase tracking-widest
	// text-fg-muted flex flex-wrap items-center justify-between` header (an <h2>
	// title on the left, a `normal-case tracking-normal` meta on the right) that was
	// hand-rolled identically at 4 /experiments sections. Centralizing it means a
	// header-typography tweak is one edit, not four.
	//
	// `titleClass` overrides ONLY the <h2>'s typography: default `font-normal`
	// keeps the quiet utility label; a caller can pass the prominent heading class
	// (a primary ledger) to override the inherited size/colour/tracking while the
	// meta stays small + muted. The header `flex-wrap`s so a louder title drops the
	// meta to the next line instead of overflowing on a narrow viewport.
	//
	// Pure layout: no interaction. `meta` and the body are snippets so callers can
	// keep live counts / links / JargonTips. NOT used for the non-uniform sections
	// (paradigms' track-band, the about plain-text labels, or the how.to.read /
	// glossary Disclosures) — those aren't this exact shape.

	import type { Snippet } from 'svelte';

	interface Props {
		/** Section anchor id (for the TOC / deep-links). */
		id?: string;
		/** Left-aligned <h2> title (the dot-separated `section.name`). */
		title: string;
		/** <h2> typography classes (default = the quiet utility label). */
		titleClass?: string;
		/** Extra classes on the <section> (default `mb-8`; last section drops it). */
		sectionClass?: string;
		/** Inline style — the staggered `animation-delay`. */
		style?: string;
		/** Right-aligned header meta (count / hint / links). */
		meta: Snippet;
		/** Panel body. */
		children: Snippet;
	}

	let {
		id,
		title,
		titleClass = 'font-normal',
		sectionClass = 'mb-8',
		style = '',
		meta,
		children
	}: Props = $props();
</script>

<section {id} class="border border-grid bg-bg-1 fade-up {sectionClass}" {style}>
	<div
		class="px-4 sm:px-5 py-3 border-b border-grid text-[10px] uppercase tracking-widest text-fg-muted flex flex-wrap items-center justify-between gap-x-3 gap-y-0.5"
	>
		<h2 class={titleClass}>{title}</h2>
		{@render meta()}
	</div>
	{@render children()}
</section>
