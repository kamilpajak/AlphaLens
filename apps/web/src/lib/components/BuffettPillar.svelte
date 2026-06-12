<script module lang="ts">
	// Module-level counter for hydration-stable tooltip ids (same approach as
	// ChipTip / JargonTip): adapter-static prerenders then the client re-
	// instantiates in the same order, so the counter matches in both runs.
	let __buffettPillarIdCounter = 0;
</script>

<script lang="ts">
	// One Buffett qualitative pillar (MOAT / TREND / CANDOR / UNDERSTOOD) as a
	// tone-coloured badge with a hover tooltip explaining the LLM's classification.
	// Cloned from GatePill (same border-badge + amber popover styling, same
	// viewport-clamp action); the caller supplies the label, the display value,
	// the tone, and the hover body so the badge stays generic.
	import type { PillarTone } from '$lib/format';
	import { clampToViewport } from '$lib/actions/clampToViewport';

	interface Props {
		label: string;
		value: string;
		tone: PillarTone;
		body: string;
	}
	let { label, value, tone, body }: Props = $props();

	const tooltipId = `buffett-pillar-${__buffettPillarIdCounter++}`;
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

	<span
		id={tooltipId}
		class="pointer-events-none absolute bottom-full left-1/2 mb-2 w-[min(20rem,calc(100vw-2rem))] z-50 opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100"
		style="transform: translateX(calc(-50% + var(--tt-shift, 0px)))"
		role="tooltip"
	>
		<span class="block border border-amber bg-bg-1 px-3 py-2 text-[11px] leading-snug text-fg-dim normal-case tracking-normal shadow-2xl">
			<span class="block text-amber font-bold uppercase tracking-widest text-[10px] mb-1">
				{label}
			</span>
			<span class="block">{body}</span>
		</span>
		<span
			class="absolute left-1/2 top-full w-2 h-2 border-r border-b border-amber bg-bg-1 -mt-1"
			style="transform: translateX(calc(-50% + var(--tt-arrow, 0px))) rotate(45deg)"
		></span>
	</span>
</span>
