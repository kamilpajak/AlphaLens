<script lang="ts">
	// Card-header chip for the insider-cluster event lane (epic #1293, #1298):
	// "insiders · 2 buyers · $610k" with the buyers behind the cluster in the
	// hover. Renders on a pure event row AND on an overlap row (a thematic card
	// whose ticker also completed a cluster — there the `#theme` chip alone would
	// hide the cluster). Facts only, as filed on the Form 4s: no tone colour, no
	// verdict word — the lane is pre-registered and its outcomes accrue on /edge,
	// where no statistic is shown before its floor.
	import type { Candidate } from '$lib/types';
	import { insiderChipLabel } from '$lib/lane';
	import { fmtDate, fmtUsdCompact } from '$lib/format';
	import { Users } from 'lucide-svelte';
	import ChipTip from './ChipTip.svelte';
	import MetricGrid from './MetricGrid.svelte';
	import TooltipNote from './TooltipNote.svelte';

	let { candidate: c }: { candidate: Candidate } = $props();

	const label = $derived(insiderChipLabel(c));

	// One row per buyer (as filed), then the lane facts. The name falls back to
	// the CIK so an unnamed filer still has a stable key.
	const rows = $derived([
		...(c.event_buyers ?? []).map((b) => ({
			key: `${b.name ?? `CIK ${b.cik}`} · ${b.role.replaceAll('_', '/')}`,
			value: `${fmtUsdCompact(b.usd)} · filed ${fmtDate(b.filed_date)}`
		})),
		{ key: 'arrival session', value: fmtDate(c.event_arrival_session) },
		{
			key: 'filing lag',
			value: c.event_filing_lag_bdays == null ? '—' : `${c.event_filing_lag_bdays} bdays`
		},
		{ key: 'gate', value: c.event_gate_version || '—' }
	]);
</script>

{#if label}
	<ChipTip term="insider purchase cluster">
		{#snippet chip()}
			<span
				data-testid="insider-cluster-chip"
				class="inline-flex items-center gap-1 whitespace-nowrap border border-fg-muted/40 px-1.5 py-0.5 text-[9px] uppercase tracking-widest text-fg-dim cursor-help"
			>
				<Users class="size-2.5" />
				{label}
			</span>
		{/snippet}
		{#snippet bodyRich()}
			<span class="block"
				>Open-market purchases by two or more officers / directors, filed within two sessions
				(SEC Form 4):</span
			>
			<MetricGrid {rows} align="right" class="mt-2" />
			<TooltipNote
				>facts as filed; pre-registered event lane — outcomes accrue on /edge, no statistic is
				shown before its floor</TooltipNote
			>
		{/snippet}
	</ChipTip>
{/if}
