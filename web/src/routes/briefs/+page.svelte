<script lang="ts">
	import type { PageData } from './$types';
	import { ChevronRight } from 'lucide-svelte';

	let { data }: { data: PageData } = $props();
</script>

<div class="px-4 py-8 max-w-[1200px] mx-auto">
	<div class="mb-8 flex items-baseline gap-3 fade-up">
		<h1 class="font-display font-bold text-3xl tracking-tight">BRIEFS.ARCHIVE</h1>
		<span class="text-[10px] uppercase tracking-widest text-fg-muted">[{data.days.length} runs]</span>
		<div class="flex-1 border-b border-dashed border-grid"></div>
	</div>

	<table class="w-full text-sm">
		<thead>
			<tr class="text-[10px] uppercase tracking-widest text-fg-muted text-left border-b border-grid">
				<th class="py-2 pr-4">date</th>
				<th class="py-2 pr-4 text-right">candidates</th>
				<th class="py-2 pr-4 text-right">themes</th>
				<th class="py-2 pr-4">top theme</th>
				<th class="py-2"></th>
			</tr>
		</thead>
		<tbody>
			{#each data.days as day, i}
				<tr class="border-b border-grid hover:bg-bg-2 group fade-up" style="animation-delay: {i * 0.05}s">
					<td class="py-3 pr-4">
						<a
							href="/brief/{day.date}"
							class="font-display font-bold text-xl text-fg group-hover:text-amber transition-colors"
						>
							{day.date}
						</a>
					</td>
					<td class="py-3 pr-4 text-right text-cyan font-bold">{day.n_candidates}</td>
					<td class="py-3 pr-4 text-right text-fg-dim">{day.n_themes}</td>
					<td class="py-3 pr-4 text-amber lowercase">{day.top_theme ?? '—'}</td>
					<td class="py-3 text-right">
						<a
							href="/brief/{day.date}"
							class="inline-flex items-center gap-1 text-[10px] uppercase tracking-widest text-fg-muted group-hover:text-amber transition-colors"
						>
							view
							<ChevronRight class="size-3" />
						</a>
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
</div>
