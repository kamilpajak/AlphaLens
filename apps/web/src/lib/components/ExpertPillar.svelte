<script module lang="ts">
	// Module-level counter for hydration-stable tooltip ids (same approach as
	// ChipTip / JargonTip): adapter-static prerenders then the client re-
	// instantiates in the same order, so the counter matches in both runs.
	let __expertPillarIdCounter = 0;
</script>

<script lang="ts">
	// One expert pillar / audit badge — a Buffett qualitative pillar (moat / trend /
	// candor / understood) OR an O'Neil audit flag (split-suspected / near-zero base) —
	// as a tone-coloured badge with a hover tooltip. Cloned from GatePill (same
	// border-badge + amber popover styling, same viewport-clamp action); the caller
	// supplies the label, the display value, the tone, and the hover body so it stays generic.
	import type { Snippet } from 'svelte';
	import type { PillarTone } from '$lib/format';
	import { clampToViewport } from '$lib/actions/clampToViewport';
	import TooltipBubble from './TooltipBubble.svelte';

	interface Props {
		label: string;
		value: string;
		tone: PillarTone;
		/** Plain-text body. Ignored when `bodyRich` is supplied. */
		body?: string;
		/** Rich body snippet (lists / formulas) — takes precedence over `body`. */
		bodyRich?: Snippet;
	}
	let { label, value, tone, body, bodyRich }: Props = $props();

	const tooltipId = `expert-pillar-${__expertPillarIdCounter++}`;
</script>

<span
	class="group relative inline-block hover:z-50 focus-within:z-50"
	tabindex="0"
	role="group"
	aria-describedby={tooltipId}
	use:clampToViewport
>
	<span
		class="inline-flex items-center gap-1 px-2 py-0.5 border text-[10px] uppercase tracking-widest cursor-help whitespace-nowrap"
		class:border-green={tone === 'good'}
		class:text-green={tone === 'good'}
		class:border-amber={tone === 'mixed'}
		class:text-amber={tone === 'mixed'}
		class:border-red={tone === 'bad'}
		class:text-red={tone === 'bad'}
		class:border-fg-muted={tone === 'muted'}
		class:text-fg-muted={tone === 'muted'}
	>
		<span class="text-fg-muted">{label}</span>
		<span class="font-bold normal-case">{value}</span>
	</span>

	<TooltipBubble id={tooltipId}>
		{#snippet header()}{label}{/snippet}
		{#if bodyRich}{@render bodyRich()}{:else}<span class="block">{body}</span>{/if}
	</TooltipBubble>
</span>
