<script lang="ts">
	import type { TradeSetup } from '$lib/types';
	import { fmtPrice, fmtPct } from '$lib/format';
	import {
		fullLadderBlendedEntry,
		stopDistanceFracFull,
		impliedRiskPctOfBook
	} from '$lib/tradeSetupRisk';
	import { Crosshair } from 'lucide-svelte';

	interface Props {
		setup: TradeSetup | null | undefined;
	}
	let { setup }: Props = $props();

	const hasStructure = $derived(
		setup != null && setup.status === 'OK' && setup.entry_tiers.length > 0
	);

	// Forward-looking risk geometry (ex-ante; computed from the setup the card
	// already shows). `riskPct` is the headline: how much of the book is at risk
	// on the disaster stop if the full ladder fills. See $lib/tradeSetupRisk.
	const fullBlended = $derived(setup ? fullLadderBlendedEntry(setup.entry_tiers) : null);
	const stopFrac = $derived(
		setup && fullBlended != null ? stopDistanceFracFull(fullBlended, setup.disaster_stop) : null
	);
	const riskPct = $derived(setup ? impliedRiskPctOfBook(setup.suggested_size_pct, stopFrac) : null);
</script>

<section data-testid="trade-setup" class="relative overflow-hidden">
	<!-- Decorative crosshair watermark (matches the live terminal aesthetic). -->
	<Crosshair
		class="pointer-events-none absolute -right-6 -top-6 size-40 text-grid opacity-40"
		aria-hidden="true"
	/>

	<div class="relative flex items-center justify-between gap-2 mb-3">
		<div class="text-[10px] uppercase tracking-widest text-cyan">trade.execution.setup</div>
		{#if setup?.order_ttl_days != null}
			<span
				class="px-2 py-0.5 border border-grid-strong text-[9px] uppercase tracking-widest text-fg-muted whitespace-nowrap"
			>
				ttl: {setup.order_ttl_days} days
			</span>
		{/if}
	</div>

	{#if !hasStructure}
		<!-- NO_STRUCTURE (downtrend / no valid long ladder) or no setup at all. -->
		<div class="relative border border-dashed border-grid-strong px-3 py-4 text-[11px]">
			<div class="text-fg-muted uppercase tracking-widest mb-1">no structured ladder</div>
			<p class="text-fg-dim leading-relaxed">
				No long entry/exit ladder generated — the deterministic setup engine found no valid
				structure (downtrend or insufficient ATR base).
			</p>
			{#if setup?.asof_close != null}
				<div class="mt-3 flex items-center gap-x-6 text-[10px] uppercase tracking-widest">
					<span class="text-fg-muted">
						ref close <span class="text-fg font-bold normal-case">{fmtPrice(setup.asof_close)}</span>
					</span>
					{#if setup?.atr != null}
						<span class="text-fg-muted">
							atr <span class="text-fg font-bold normal-case">{fmtPrice(setup.atr)}</span>
						</span>
					{/if}
				</div>
			{/if}
		</div>
	{:else if setup}
		<!-- Sizing + risk headline -->
		<div class="relative grid grid-cols-2 gap-x-6 gap-y-3 mb-4 text-[10px] uppercase tracking-widest">
			<div>
				<div class="text-fg-muted">suggested size</div>
				<div class="text-fg text-base font-bold normal-case">
					{setup.suggested_size_pct != null ? `${fmtPct(setup.suggested_size_pct, 2, false)} of book` : '—'}
				</div>
			</div>
			<div>
				<div class="text-fg-muted">disaster stop</div>
				<div class="text-red text-base font-bold normal-case whitespace-nowrap">
					{fmtPrice(setup.disaster_stop)}
				</div>
			</div>
			<div>
				<div class="text-fg-muted">risk at stop</div>
				<div class="text-fg text-base font-bold normal-case">
					{riskPct != null ? `${fmtPct(riskPct, 1, false)} of book` : '—'}
				</div>
			</div>
			<div>
				<div class="text-fg-muted">ref last close</div>
				<div class="text-fg text-base font-bold normal-case whitespace-nowrap">
					{fmtPrice(setup.asof_close)}
				</div>
			</div>
			<div>
				<div class="text-fg-muted">full-ladder entry</div>
				<div class="text-fg text-base font-bold normal-case whitespace-nowrap">
					{fmtPrice(fullBlended)}
				</div>
			</div>
			<div>
				<div class="text-fg-muted">to stop</div>
				<div class="text-fg-muted text-base font-bold normal-case whitespace-nowrap">
					{stopFrac != null ? fmtPct(-stopFrac * 100, 1) : '—'}
				</div>
			</div>
		</div>

		<!-- Entry tiering (limit buys below close) -->
		<div class="relative mb-4">
			<div class="text-[10px] uppercase tracking-widest text-fg-muted mb-1.5">
				entry tiering <span class="text-fg-muted/70 lowercase tracking-normal">(below close)</span>
			</div>
			<div class="flex flex-col gap-1">
				{#each setup.entry_tiers as tier, i}
					<div
						class="grid grid-cols-[3rem_auto_1fr] sm:grid-cols-[3rem_auto_auto_auto_1fr] items-center gap-x-2 sm:gap-x-3 px-3 py-1.5 bg-bg-2 border-l-2 border-violet text-[11px]"
					>
						<span class="text-violet uppercase tracking-widest text-[9px] whitespace-nowrap"
							>tier {i + 1}</span
						>
						<span class="text-fg font-bold whitespace-nowrap">{fmtPrice(tier.limit)}</span>
						<span class="text-fg-dim text-right sm:text-left whitespace-nowrap">{fmtPct(tier.alloc_pct, 0, false)}</span>
						<span class="hidden sm:block text-fg-muted whitespace-nowrap">{Number.isFinite(tier.atr_distance) ? `-${tier.atr_distance.toFixed(1)} ATR` : '—'}</span>
						<span class="hidden sm:block text-fg-muted italic text-right truncate">{tier.tag}</span>
					</div>
				{/each}
			</div>
		</div>

		<!-- Take-profit exits -->
		<div class="relative">
			<div class="text-[10px] uppercase tracking-widest text-fg-muted mb-1.5">take-profit exits</div>
			<div class="flex flex-col gap-1">
				{#each setup.tp_tranches as tp, i}
					<div
						class="grid grid-cols-[3rem_auto_1fr] sm:grid-cols-[3rem_auto_auto_auto_1fr] items-center gap-x-2 sm:gap-x-3 px-3 py-1.5 bg-green/5 border-l-2 border-green text-[11px]"
					>
						<span class="text-green uppercase tracking-widest text-[9px] whitespace-nowrap"
							>tp{i + 1}</span
						>
						<span class="text-fg font-bold whitespace-nowrap">{fmtPrice(tp.target)}</span>
						<span class="text-fg-dim text-right sm:text-left whitespace-nowrap">{fmtPct(tp.tranche_pct, 0, false)}</span>
						<span class="hidden sm:block text-fg-muted whitespace-nowrap">{Number.isFinite(tp.r_multiple) ? `${tp.r_multiple.toFixed(1)}R` : '—'}</span>
						<span class="hidden sm:block text-fg-muted italic text-right truncate">{tp.tag}</span>
					</div>
				{/each}
			</div>
		</div>
	{/if}
</section>
