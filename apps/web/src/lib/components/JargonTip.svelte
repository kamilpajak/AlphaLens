<script module lang="ts">
	// Module-level counter for hydration-stable tooltip ids. SvelteKit
	// adapter-static prerenders pages at build time, then the client
	// re-instantiates the same components in the same order; both runs see
	// the counter increment deterministically, so SSR HTML and hydrated
	// DOM agree on every `id` / `aria-describedby` linkage. (The earlier
	// Math.random() approach risked desync between SSR and client init.)
	//
	// Svelte 5.55 does NOT export a built-in useId — verified against
	// node_modules/svelte/src/index-client.js. When a future minor adds
	// it, swap this counter for `useId()`.
	let __jargonTipIdCounter = 0;
</script>

<script lang="ts">
	// Hover/focus tooltip for inline jargon terms. Mirrors the GatePill
	// tooltip pattern (group wrapper + cursor-help + pointer-events-none popover
	// + arrow) so the existing CSS regression guard in tests/smoke.test.ts
	// extends to this component too.
	//
	// Usage:
	//   <JargonTip term="αt" full="t-statistic on Carhart-4F α" body="Measures...">αt</JargonTip>
	//
	// The visible text comes from the slot (so callers can render the term
	// inside a sentence without DOM gymnastics); `term` is the canonical
	// label shown in the tooltip header, `full` is the expanded acronym (if
	// any), `body` is the 1-2 sentence definition.

	import type { Snippet } from 'svelte';
	import { clampToViewport } from '$lib/actions/clampToViewport';
	import TooltipBubble from './TooltipBubble.svelte';
	import Formula from './Formula.svelte';

	interface Props {
		term: string;
		full?: string;
		/** Plain-text definition. Ignored when `bodyRich` is supplied. */
		body?: string;
		/** Rich body snippet (lists / formulas) — takes precedence over `body`. */
		bodyRich?: Snippet;
		/** Optional formulas.json key — typesets the formula under the text body
		 *  (glossary-backed ratio terms like PE/ROE; ignored when `bodyRich` is set). */
		formula?: string;
		children?: Snippet;
	}

	let { term, full = '', body, bodyRich, formula, children }: Props = $props();

	// Per-instance id linking the focusable trigger to the tooltip body via
	// aria-describedby. Sourced from the module-level counter so SSR and
	// client hydration produce the same value (no hydration mismatch).
	const tooltipId = `jargon-tip-${__jargonTipIdCounter++}`;

	// Touch-device support: iOS Safari + Android Chrome don't reliably trigger
	// :hover or auto-focus when tapping an element with tabindex=0. Without an
	// explicit pointerdown→focus call, mobile users would have no way to
	// surface the tooltip — which becomes a hard regression now that the
	// upfront architecture primer block on /experiments is gone. Focusing on
	// pointerdown activates `group-focus-within:opacity-100` on the popover.
	function onPointerDown(e: PointerEvent) {
		(e.currentTarget as HTMLElement).focus();
	}
</script>

<span
	class="group relative inline-block hover:z-50 focus-within:z-50"
	tabindex="0"
	role="group"
	onpointerdown={onPointerDown}
	data-testid="jargon-tip"
	data-term={term}
	aria-describedby={tooltipId}
	use:clampToViewport
>
	<span class="cursor-help underline decoration-dotted decoration-fg-muted underline-offset-2">
		{#if children}{@render children()}{:else}{term}{/if}
	</span>

	<TooltipBubble id={tooltipId}>
		{#snippet header()}{term}{#if full} // {full}{/if}{/snippet}
		{#if bodyRich}
			{@render bodyRich()}
		{:else}
			<span class="block">{body}</span>
			{#if formula}<span class="block mt-1.5 text-fg-muted">= <Formula name={formula} /></span>{/if}
		{/if}
	</TooltipBubble>
</span>
