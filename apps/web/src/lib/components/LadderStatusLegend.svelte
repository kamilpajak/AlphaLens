<script lang="ts">
	// Collapsible "what do these mean?" legend for the /edge outcome badges.
	// Reads the same gloss source as the per-badge ChipTip tooltips
	// (ladderStatus.ts) and the same tone → class map as the table chips
	// (edge.ts), so colours and wording never drift between the two surfaces.

	import { LADDER_STATUS, PENDING_STATUS, type LadderGroup } from '$lib/data/ladderStatus';
	import { classificationTone, toneClasses } from '$lib/edge';

	const GROUPS: { key: LadderGroup; label: string }[] = [
		{ key: 'ongoing', label: 'ongoing' },
		{ key: 'terminal', label: 'closed' },
		{ key: 'unmeasurable', label: 'not measurable' }
	];

	function entriesFor(group: LadderGroup) {
		const base = LADDER_STATUS.filter((e) => e.group === group);
		// The synthetic PENDING placeholder belongs with the ongoing states.
		return group === 'ongoing' ? [...base, PENDING_STATUS] : base;
	}
</script>

<details class="group/legend mb-6 border border-grid bg-bg-1 fade-up">
	<summary
		class="cursor-pointer select-none list-none [&::-webkit-details-marker]:hidden px-3 py-2 text-[10px] uppercase tracking-widest text-fg-muted hover:text-fg-dim flex items-center gap-2"
	>
		<span
			class="inline-block text-amber transition-transform duration-150 group-open/legend:rotate-90"
			aria-hidden="true">▸</span
		>
		<span>what do these statuses mean?</span>
	</summary>
	<div class="border-t border-grid px-3 py-3 grid grid-cols-1 sm:grid-cols-3 gap-x-5 gap-y-3">
		{#each GROUPS as g (g.key)}
			<div>
				<div class="text-[9px] uppercase tracking-widest text-fg-muted mb-2">{g.label}</div>
				<ul class="flex flex-col gap-1.5">
					{#each entriesFor(g.key) as e (e.code)}
						<li class="flex items-baseline gap-2 text-[11px] leading-snug">
							<span
								class="inline-block shrink-0 px-1.5 py-0.5 border text-[9px] uppercase tracking-widest whitespace-nowrap {toneClasses(
									classificationTone(e.code)
								)} {e.code === PENDING_STATUS.code ? 'border-dashed' : ''}"
							>
								{e.code}
							</span>
							<span class="text-fg-dim">{e.short}</span>
						</li>
					{/each}
				</ul>
			</div>
		{/each}
	</div>
</details>
