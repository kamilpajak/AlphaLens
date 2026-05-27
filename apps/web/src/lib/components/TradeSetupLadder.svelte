<script lang="ts">
	import type { TradeSetup } from '$lib/types';

	interface Props {
		setup: TradeSetup;
	}
	let { setup }: Props = $props();

	const ok = $derived(setup.status === 'OK' && setup.disaster_stop != null);

	const close = $derived(setup.asof_close);
	const stop = $derived(setup.disaster_stop ?? 0);
	const entries = $derived(setup.entry_tiers);
	// Highest target at the top; render TP rungs top→bottom.
	const tpsTopDown = $derived([...setup.tp_tranches].reverse());

	// Proportional vertical axis: bottom = disaster stop, top = highest target.
	// 8% padding top+bottom so the LAST/STOP rules never clip their chips.
	const lo = $derived(stop);
	const hi = $derived(Math.max(close, ...setup.tp_tranches.map((t) => t.target)));
	const pad = $derived((hi - lo) * 0.08 || 1);
	const top = $derived(hi + pad);
	const span = $derived(top - (lo - pad) || 1);
	// 0% = top of the rail (highest price), 100% = bottom.
	const yPct = (price: number): number => ((top - price) / span) * 100;

	// Allocation/tranche bar width (share of the rail), capped.
	const barW = (pct: number): number => Math.min(pct * 1.5, 100);
</script>

<section class="border-t border-grid">
	<div
		class="flex flex-col md:flex-row md:items-center justify-between gap-1.5 md:gap-0 px-4 sm:px-5 py-2.5 border-b border-grid bg-bg-2"
	>
		<span class="text-[10px] uppercase tracking-widest text-cyan">trade setup</span>
		{#if ok}
			<span class="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] uppercase tracking-widest">
				<span class="text-amber font-bold whitespace-nowrap">size {setup.suggested_size_pct?.toFixed(1)}%</span>
				<span class="text-red font-bold whitespace-nowrap">disaster stop ${setup.disaster_stop?.toFixed(2)}</span>
				<span class="text-fg-muted whitespace-nowrap">order ttl {setup.order_ttl_days}d</span>
			</span>
		{/if}
	</div>

	{#if !ok}
		<p class="px-4 sm:px-5 py-6 text-center text-fg-muted text-[11px] italic">
			no trade setup (insufficient price structure)
		</p>
	{:else}
		<!-- Vertical price ladder: every level at its true proportional price
		     position, so the vertical gaps map to real price distances. Each rung
		     spans full width — price in the left gutter, a glow bar sized by
		     commitment %, and a detail chip on the rail. Reference map of where
		     orders sit vs the last close — NOT a forecast. -->
		<div
			class="relative w-full h-[520px] bg-bg-1 overflow-hidden text-[11px] sm:text-xs"
			role="figure"
			aria-label="Price ladder — stop ${stop.toFixed(2)}, close ${close.toFixed(2)}, {entries.length} entry tier(s), {tpsTopDown.length} take-profit target(s) up to ${hi.toFixed(2)}."
		>
			<!-- zone tints: green upside above LAST, red risk below STOP -->
			<div
				class="absolute inset-x-0 top-0 pointer-events-none bg-gradient-to-t from-transparent to-green/[0.05]"
				style="bottom: {100 - yPct(close)}%"
			></div>
			<div
				class="absolute inset-x-0 bottom-0 pointer-events-none bg-gradient-to-b from-transparent to-red/[0.06]"
				style="top: {yPct(stop)}%"
			></div>

			<!-- spine + direction caps -->
			<div class="absolute left-[68px] sm:left-28 top-6 bottom-6 w-px bg-grid"></div>
			<div class="absolute left-[68px] sm:left-28 top-1 -translate-x-[5px] text-grid-strong text-[10px]">▲</div>
			<div class="absolute left-[68px] sm:left-28 bottom-1 -translate-x-[5px] text-grid-strong text-[10px]">▼</div>

			<!-- take-profit rungs -->
			{#each tpsTopDown as tp, i}
				<div
					class="absolute left-0 right-0 flex items-center"
					style="top: {yPct(tp.target)}%; transform: translateY(-50%)"
				>
					<span class="w-[60px] sm:w-28 shrink-0 pr-2 sm:pr-4 text-right text-green font-medium tabular-nums whitespace-nowrap">
						${tp.target.toFixed(2)}
					</span>
					<span class="relative flex-1 flex items-center min-w-0">
						<span class="absolute left-0 right-0 h-px bg-green/20"></span>
						<span class="absolute left-0 size-1.5 rounded-full bg-green -translate-x-[3px]"></span>
						<span
							class="absolute left-0 h-0.5 bg-green/40 shadow-[0_0_8px_var(--color-green)]"
							style="width: {barW(tp.tranche_pct)}%"
						></span>
						<span class="absolute left-2 sm:left-5 flex items-baseline gap-2 sm:gap-4 px-1.5 sm:px-2.5 py-0.5 bg-bg-1 border border-green/40 tabular-nums whitespace-nowrap">
							<span class="text-green font-bold">TP{tpsTopDown.length - i}</span>
							<span class="text-fg">{tp.tranche_pct.toFixed(0)}%</span>
							<span class="text-green">{tp.r_multiple.toFixed(1)}R</span>
							<span class="hidden sm:inline text-green/70 text-[10px] uppercase tracking-wide truncate max-w-[180px]">{tp.tag}</span>
						</span>
					</span>
				</div>
			{/each}

			<!-- LAST rule (the anchor) -->
			<div
				class="absolute left-0 right-0 flex items-center z-10"
				style="top: {yPct(close)}%; transform: translateY(-50%)"
			>
				<span class="w-[60px] sm:w-28 shrink-0 pr-2 sm:pr-4 text-right text-amber font-bold tabular-nums whitespace-nowrap">
					${close.toFixed(2)}
				</span>
				<span class="relative flex-1 flex items-center min-w-0">
					<span class="absolute left-0 right-0 border-t border-dashed border-amber/80"></span>
					<span class="absolute left-0 size-2 rounded-full bg-amber -translate-x-[4px]"></span>
					<span class="absolute left-2 sm:left-5 px-1.5 sm:px-2.5 py-0.5 bg-bg-1 border border-amber/40 text-amber font-bold text-[10px] uppercase tracking-widest whitespace-nowrap">
						close
					</span>
				</span>
			</div>

			<!-- entry rungs -->
			{#each entries as entry, i}
				<div
					class="absolute left-0 right-0 flex items-center"
					style="top: {yPct(entry.limit)}%; transform: translateY(-50%)"
				>
					<span
						class="w-[60px] sm:w-28 shrink-0 pr-2 sm:pr-4 text-right font-medium tabular-nums whitespace-nowrap"
						class:text-amber={i === 0}
						class:text-amber-dim={i > 0}
					>
						${entry.limit.toFixed(2)}
					</span>
					<span class="relative flex-1 flex items-center min-w-0">
						<span class="absolute left-0 right-0 h-px bg-amber/20"></span>
						<span
							class="absolute left-0 size-1.5 rounded-full -translate-x-[3px]"
							class:bg-amber={i === 0}
							class:bg-amber-dim={i > 0}
						></span>
						{#if i === 0}
							<span class="absolute left-0 h-0.5 bg-amber/40 shadow-[0_0_8px_var(--color-amber)]" style="width: {barW(entry.alloc_pct)}%"></span>
						{:else}
							<span class="absolute left-0 h-0.5 bg-amber-dim/40" style="width: {barW(entry.alloc_pct)}%"></span>
						{/if}
						<span class="absolute left-2 sm:left-5 flex items-baseline gap-2 sm:gap-4 px-1.5 sm:px-2.5 py-0.5 bg-bg-1 border border-amber/40 tabular-nums whitespace-nowrap">
							<span class="font-bold" class:text-amber={i === 0} class:text-amber-dim={i > 0}>E{i + 1}</span>
							<span class="text-fg">{entry.alloc_pct.toFixed(0)}%</span>
							<span class="text-amber whitespace-nowrap">−{entry.atr_distance.toFixed(1)} ATR</span>
							<span class="hidden sm:inline text-amber/70 text-[10px] uppercase tracking-wide truncate max-w-[180px]">{entry.tag}</span>
						</span>
					</span>
				</div>
			{/each}

			<!-- disaster stop rule -->
			<div
				class="absolute left-0 right-0 flex items-center"
				style="top: {yPct(stop)}%; transform: translateY(-50%)"
			>
				<span class="w-[60px] sm:w-28 shrink-0 pr-2 sm:pr-4 text-right text-red font-bold tabular-nums whitespace-nowrap">
					${stop.toFixed(2)}
				</span>
				<span class="relative flex-1 flex items-center min-w-0">
					<span class="absolute left-0 right-0 border-t border-dashed border-red/80"></span>
					<span class="absolute left-0 size-2 rotate-45 bg-red -translate-x-[4px] border border-bg-1"></span>
					<span class="absolute left-2 sm:left-5 px-1.5 sm:px-2.5 py-0.5 bg-bg-1 border border-red/40 text-red font-bold text-[10px] uppercase tracking-widest whitespace-nowrap">
						stop
					</span>
				</span>
			</div>
		</div>
	{/if}
</section>
