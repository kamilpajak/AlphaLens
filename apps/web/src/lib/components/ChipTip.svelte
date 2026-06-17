<script module lang="ts">
	// Module-level counter for hydration-stable tooltip ids. Same approach
	// as JargonTip — SvelteKit adapter-static prerenders pages, then the
	// client re-instantiates components in the same order, so the counter
	// produces matching ids in both runs (no SSR/hydration mismatch).
	let __chipTipIdCounter = 0;
</script>

<script lang="ts">
	// Custom-styled tooltip wrapper for chip-shaped triggers (peer-cohort
	// warnings, pattern badges like REVERSAL, …). Mirrors the JargonTip +
	// GatePill popover styling (amber border, term header, body), but the
	// caller supplies the chip via the `chip` snippet so chip color /
	// content / icon stay local to the call site.
	//
	// Migrating chips off HTML `title=` removes the touch-device dead-zone
	// (mobile browsers don't surface native title=) and gives a real
	// aria-describedby link from the focusable trigger to the tooltip.

	import type { Snippet } from 'svelte';
	import { clampToViewport } from '$lib/actions/clampToViewport';
	import TooltipBubble from './TooltipBubble.svelte';

	interface Props {
		term: string;
		/** Plain-text body. Ignored when `bodyRich` is supplied. */
		body?: string;
		/** Rich body snippet (lists / formulas) — takes precedence over `body`. */
		bodyRich?: Snippet;
		chip: Snippet;
	}

	let { term, body, bodyRich, chip }: Props = $props();

	const tooltipId = `chip-tip-${__chipTipIdCounter++}`;

	// iOS Safari / Android Chrome don't auto-focus tabindex=0 elements on
	// tap, so the group-focus-within selector wouldn't fire. Explicit
	// pointerdown→focus surfaces the popover on touch the same way it does
	// on hover.
	function onPointerDown(e: PointerEvent) {
		(e.currentTarget as HTMLElement).focus();
	}
</script>

<span
	class="group/chip relative inline-block cursor-help hover:z-50 focus-within:z-50"
	tabindex="0"
	role="group"
	onpointerdown={onPointerDown}
	data-testid="chip-tip"
	data-term={term}
	aria-describedby={tooltipId}
	use:clampToViewport
>
	{@render chip()}

	<TooltipBubble id={tooltipId} group="chip">
		{#snippet header()}{term}{/snippet}
		{#if bodyRich}{@render bodyRich()}{:else}<span class="block">{body}</span>{/if}
	</TooltipBubble>
</span>
