<script lang="ts">
	import { AlertTriangle, FlaskConical } from 'lucide-svelte';
	import type { EdgeSummary } from '$lib/types';
	import { fmtR, statsUnlocked } from '$lib/edge';
	import { hasWhatif, whatifLenses, type WhatIfLensView } from '$lib/edgeWhatif';

	// DISPLAY-ONLY counterfactual sandbox. Collapsed by default so the REALIZED
	// panels stay the default production view; the what-if is strictly opt-in and
	// every active lens carries an unmissable in-sample / not-validated banner.
	// Never rendered inside a primary KPI card (honest-presentation invariant).
	let { summary }: { summary: EdgeSummary } = $props();

	const wf = $derived(summary.whatif);
	const lenses = $derived<WhatIfLensView[]>(hasWhatif(wf) ? whatifLenses(wf) : []);
	const unlocked = $derived(statsUnlocked(wf.status));

	let selectedId = $state<string | null>(null);
	const selected = $derived<WhatIfLensView | null>(
		lenses.find((l) => l.lensId === selectedId) ?? lenses[0] ?? null
	);

	// The realized R headline (de-emphasised) the what-if is compared against —
	// always shown beside the what-if so the two are never confused.
	const realizedR = $derived(summary.edge.gross_realized_r_mean);

	// Drive the banner emphasis off the SELECTED lens's status (client registry),
	// not a hard-coded string: an in_sample lens warns "in-sample · not validated";
	// once a lens graduates to validated the banner softens to "validated forward"
	// instead of falsely warning. Every lens stays a COUNTERFACTUAL either way.
	const activeStatus = $derived(selected?.status ?? 'in_sample');
</script>

{#if hasWhatif(wf)}
	<details class="mb-6 border border-dashed border-violet/40 bg-violet/5 corners fade-up" data-testid="whatif-panel">
		<summary
			class="flex cursor-pointer items-center gap-2 px-4 py-3 text-[10px] uppercase tracking-widest text-violet select-none"
		>
			<FlaskConical class="size-3.5" />
			<span class="font-bold">// what-if · experimental</span>
			<span class="text-fg-muted normal-case tracking-normal">exit-stop · counterfactual</span>
		</summary>

		<div class="px-4 pb-4">
			<!-- Persistent epistemic banner — shown whenever the sandbox is open. -->
			<div
				class="mb-3 flex items-start gap-2 border border-violet/40 bg-violet/10 px-3 py-2 text-[11px] leading-snug text-fg-dim"
				data-testid="whatif-banner"
			>
				<AlertTriangle class="mt-0.5 size-3.5 shrink-0 text-violet" />
				<p>
					<span class="text-violet font-bold uppercase tracking-widest text-[10px]">
						{activeStatus === 'validated'
							? 'what-if · validated forward'
							: 'what-if · in-sample · not validated'}
					</span>
					— realized R recomputed under an alternative exit-stop on the
					<span class="whitespace-nowrap">SAME picks</span>{activeStatus === 'validated'
						? ''
						: '; tuned on this sample, so optimistic'}.
					<span class="font-bold">Never the realized result</span> — the panels above are what the tool
					actually emitted.
				</p>
			</div>

			<!-- Lens selector (registry-driven; one button per served lens). -->
			<div
				class="mb-3 flex flex-wrap gap-1.5 text-[10px] uppercase tracking-widest"
				role="group"
				aria-label="what-if lens"
			>
				{#each lenses as l (l.lensId)}
					<button
						type="button"
						onclick={() => (selectedId = l.lensId)}
						class="border px-2 py-1 transition-colors whitespace-nowrap {selected?.lensId === l.lensId
							? 'border-violet text-violet bg-violet/10'
							: 'border-grid text-fg-muted hover:text-fg-dim'}"
						aria-pressed={selected?.lensId === l.lensId}
					>
						{l.label}
						<span class="ml-1 text-fg-muted normal-case tracking-normal">[{l.status}]</span>
					</button>
				{/each}
			</div>

			{#if selected}
				{#if !unlocked}
					<div class="text-fg-muted text-[11px] uppercase tracking-widest" data-testid="whatif-gated">
						ⓘ insufficient — n matured = {wf.n_matured}
						<span class="text-fg-dim">(&lt; {wf.threshold})</span>
					</div>
				{:else}
					<div class="flex flex-wrap items-baseline gap-x-6 gap-y-2 text-[11px] uppercase tracking-widest">
						<span class="text-fg-muted">
							what-if mean R
							<span class="text-violet font-bold normal-case">{fmtR(selected.meanR)}</span>
						</span>
						<span class="text-fg-muted">
							median <span class="text-fg-dim font-bold normal-case">{fmtR(selected.medianR)}</span>
						</span>
						<span class="text-fg-muted whitespace-nowrap">n {selected.n}</span>
						<span class="text-fg-muted">
							vs realized
							<span class="text-fg-dim font-bold normal-case">{fmtR(realizedR)}</span>
						</span>
					</div>
				{/if}
			{/if}
		</div>
	</details>
{/if}
