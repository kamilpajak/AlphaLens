<script module lang="ts">
	// Module-level counter for hydration-stable tooltip ids — same rationale as
	// JargonTip: adapter-static prerenders then the client re-instantiates in
	// the same order, so the deterministic counter keeps SSR and hydrated
	// `id` / `aria-describedby` linkages in agreement.
	let __signalBarTipIdCounter = 0;
</script>

<script lang="ts">
	import type { Snippet } from 'svelte';
	import { clampToViewport } from '$lib/actions/clampToViewport';
	import TooltipBubble from './TooltipBubble.svelte';

	interface Props {
		label: string;
		value: number | null | undefined;
		min?: number;
		max?: number;
		format?: (v: number) => string;
		inverted?: boolean;
		/** Optional plain-text tooltip shown on hover. Ignored when `tooltipRich` is set. */
		tooltip?: string;
		/** Rich tooltip snippet (lists / formulas) — takes precedence over `tooltip`. */
		tooltipRich?: Snippet;
		/** Text shown (muted, empty bar) when `value` is null/non-finite. Defaults
		 *  to an em-dash. Used to render an honest "no buys" / "net selling" state
		 *  instead of a misleading percentile. */
		placeholder?: string;
		/** Pre-formatted secondary annotation rendered (dimmed, normal weight) just
		 *  left of the main tone-coloured value on the value line. Used by the FCFF
		 *  YIELD bar to show the raw yield (e.g. "+8.36%") beside its sector-%ile.
		 *  The main value stays right-anchored so a column of stacked bars keeps a
		 *  flush right edge whether or not each carries a subValue. Omitted/empty →
		 *  nothing renders. */
		subValue?: string | null;
	}
	let {
		label,
		value,
		min = 0,
		max = 100,
		format = (v) => v.toFixed(1),
		inverted = false,
		tooltip,
		tooltipRich,
		placeholder = '—',
		subValue
	}: Props = $props();

	// A bubble shows when either body form is supplied. The trigger is the LABEL
	// itself (the dotted-underlined name) — NOT the whole bar row — so the
	// tooltip only opens on the name a reader would hover, not on the bar track.
	const hasTooltip = $derived(Boolean(tooltip || tooltipRich));

	// Per-instance id linking the focusable label trigger to its tooltip body
	// via aria-describedby (mirrors JargonTip; module counter ⇒ SSR/hydration
	// stable).
	const tooltipId = `signal-bar-tip-${__signalBarTipIdCounter++}`;

	// Touch-device support: tapping focuses the label so the popover's
	// `group-focus-within:opacity-100` reveals it (iOS/Android don't fire :hover
	// on tap). Mirrors JargonTip.
	function onPointerDown(e: PointerEvent) {
		(e.currentTarget as HTMLElement).focus();
	}

	const pct = $derived(
		value === null || value === undefined || !Number.isFinite(value)
			? 0
			: Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100))
	);
	const display = $derived(
		value === null || value === undefined || !Number.isFinite(value) ? placeholder : format(value)
	);

	type Tone = 'green' | 'amber' | 'red' | 'muted';
	const tone: Tone = $derived.by(() => {
		if (value === null || value === undefined || !Number.isFinite(value)) return 'muted';
		if (inverted) {
			if (pct > 70) return 'red';
			if (pct > 40) return 'amber';
			return 'green';
		}
		if (pct > 70) return 'green';
		if (pct > 40) return 'amber';
		return 'red';
	});
</script>

<div data-testid="signal-bar" class="text-[10px] uppercase tracking-widest">
	<div class="flex items-center justify-between mb-1.5 gap-2">
		{#if hasTooltip}
			<!-- Tooltip trigger scoped to the label only: hovering/focusing the
			     dotted-underlined name opens the bubble; the bar track below is
			     inert. `group relative` anchors the popover to this span (mirrors
			     JargonTip). -->
			<span
				class="group relative min-w-0 hover:z-50 focus-within:z-50"
				tabindex="0"
				role="group"
				onpointerdown={onPointerDown}
				aria-describedby={tooltipId}
				use:clampToViewport
			>
				<span
					class="block truncate cursor-help text-fg-muted underline decoration-dotted decoration-fg-muted underline-offset-2"
					>{label}</span
				>
				<TooltipBubble id={tooltipId}>
					{#snippet header()}{label}{/snippet}
					{#if tooltipRich}{@render tooltipRich()}{:else}<span class="block">{tooltip}</span>{/if}
				</TooltipBubble>
			</span>
		{:else}
			<span class="text-fg-muted truncate">{label}</span>
		{/if}
		<span class="flex items-baseline gap-2 whitespace-nowrap">
			{#if subValue}
				<span class="text-fg-dim">{subValue}</span>
			{/if}
			<span
				class="font-bold"
				class:text-green={tone === 'green'}
				class:text-amber={tone === 'amber'}
				class:text-red={tone === 'red'}
				class:text-fg-muted={tone === 'muted'}
			>{display}</span>
		</span>
	</div>
	<div data-testid="signal-bar-track" class="h-1.5 bg-bg-3 relative overflow-hidden">
		<div
			class="absolute inset-y-0 left-0"
			class:bg-green={tone === 'green'}
			class:bg-amber={tone === 'amber'}
			class:bg-red={tone === 'red'}
			class:bg-fg-muted={tone === 'muted'}
			style="width: {pct}%"
		></div>
	</div>
</div>
