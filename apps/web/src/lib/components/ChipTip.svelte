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

	interface Props {
		term: string;
		body: string;
		chip: Snippet;
	}

	let { term, body, chip }: Props = $props();

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
	class="group relative inline-block hover:z-50 focus-within:z-50"
	tabindex="0"
	role="group"
	onpointerdown={onPointerDown}
	data-testid="chip-tip"
	data-term={term}
	aria-describedby={tooltipId}
>
	{@render chip()}

	<span
		id={tooltipId}
		class="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-[min(20rem,calc(100vw-2rem))] z-50 opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100"
		role="tooltip"
	>
		<span class="block border border-amber bg-bg-1 px-3 py-2 text-[11px] leading-snug text-fg-dim normal-case tracking-normal shadow-2xl">
			<span class="block text-amber font-bold uppercase tracking-widest text-[10px] mb-1">
				{term}
			</span>
			<span class="block">{body}</span>
		</span>
		<span
			class="absolute left-1/2 -translate-x-1/2 top-full w-2 h-2 border-r border-b border-amber bg-bg-1 -mt-1 rotate-45"
		></span>
	</span>
</span>
