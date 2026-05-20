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

	interface Props {
		term: string;
		full?: string;
		body: string;
		children?: () => unknown;
	}

	let { term, full = '', body, children }: Props = $props();

	// Unique id linking the focusable trigger to the tooltip body via
	// aria-describedby. Svelte 5 doesn't ship a built-in useId, so we
	// generate a stable per-instance id at construction time. Screen
	// readers (NVDA, VoiceOver, JAWS) announce the tooltip text when the
	// trigger receives focus.
	const tooltipId = `jargon-tip-${Math.random().toString(36).slice(2, 10)}`;
</script>

<span
	class="group relative inline-block hover:z-50 focus-within:z-50"
	tabindex="0"
	role="group"
	data-testid="jargon-tip"
	data-term={term}
	aria-describedby={tooltipId}
>
	<span class="cursor-help underline decoration-dotted decoration-fg-muted underline-offset-2">
		{#if children}{@render children()}{:else}{term}{/if}
	</span>

	<span
		id={tooltipId}
		class="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-[min(20rem,calc(100vw-2rem))] z-50 opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100"
		role="tooltip"
	>
		<span class="block border border-amber bg-bg-1 px-3 py-2 text-[11px] leading-snug text-fg-dim normal-case tracking-normal shadow-2xl">
			<span class="block text-amber font-bold uppercase tracking-widest text-[10px] mb-1">
				{term}{#if full} // {full}{/if}
			</span>
			<span class="block">{body}</span>
		</span>
		<span
			class="absolute left-1/2 -translate-x-1/2 top-full w-2 h-2 border-r border-b border-amber bg-bg-1 -mt-1 rotate-45"
		></span>
	</span>
</span>
