<script lang="ts">
	import { ArrowUpRight, Calendar, Layers, Sparkles, Target, TrendingUp } from 'lucide-svelte';
	import type { PageData } from './$types';
	import { confidenceTone, fmtUsdCompact, fmtPct } from '$lib/format';

	let { data }: { data: PageData } = $props();

	// Defensive: data.latestBrief is null on a fresh VPS where the pipeline
	// hasn't produced any briefs yet. Every derived value below either falls
	// back to empty or is gated behind {#if hasBrief} in the template.
	const hasBrief = $derived(data.latestBrief !== null);

	const totalCandidates = $derived(data.days.reduce((s, d) => s + d.n_candidates, 0));
	const avgConf = $derived(
		data.latestBrief && data.latestBrief.candidates.length
			? data.latestBrief.candidates.reduce((s, c) => s + c.llm_confidence, 0) /
				data.latestBrief.candidates.length
			: 0
	);

	const themeBars = $derived(
		data.latestBrief
			? Object.entries(data.latestBrief.theme_counts)
					.sort(([, a], [, b]) => b - a)
					.map(([theme, count]) => ({
						theme,
						count,
						pct:
							data.latestBrief!.n_candidates > 0
								? (count / data.latestBrief!.n_candidates) * 100
								: 0
					}))
			: []
	);

	const gateStats = $derived.by(() => {
		const stats: Record<string, { passed: number; failed: number; unknown: number }> = {};
		if (!data.latestBrief) return [];
		for (const c of data.latestBrief.candidates) {
			for (const g of c.gates_passed) stats[g] = { ...(stats[g] ?? { passed: 0, failed: 0, unknown: 0 }), passed: (stats[g]?.passed ?? 0) + 1 };
			for (const g of c.gates_failed) stats[g] = { ...(stats[g] ?? { passed: 0, failed: 0, unknown: 0 }), failed: (stats[g]?.failed ?? 0) + 1 };
			for (const g of c.gates_unknown) stats[g] = { ...(stats[g] ?? { passed: 0, failed: 0, unknown: 0 }), unknown: (stats[g]?.unknown ?? 0) + 1 };
		}
		return Object.entries(stats).sort(([a], [b]) => a.localeCompare(b));
	});

	const topCandidates = $derived(data.latestBrief?.candidates.slice(0, 8) ?? []);

	// Cap the CAPTURED.SESSIONS grid to the most recent runs; the full
	// history is reachable via the "all briefs" link. The index is newest-first.
	const MAX_DAY_TILES = 6;
	const dayTiles = $derived(data.days.slice(0, MAX_DAY_TILES));

	type Tone = 'amber' | 'cyan' | 'green' | 'magenta';
	const stats: { icon: typeof Calendar; label: string; value: string; tone: Tone }[] = $derived([
		{ icon: Calendar, label: 'days', value: String(data.days.length), tone: 'amber' },
		{ icon: Target, label: 'candidates', value: String(totalCandidates), tone: 'cyan' },
		{ icon: Layers, label: 'themes (latest)', value: hasBrief ? String(data.latestBrief!.n_themes) : '—', tone: 'green' },
		{ icon: Sparkles, label: 'avg conf', value: hasBrief ? (avgConf * 5).toFixed(1) + '/5' : '—', tone: 'magenta' }
	]);
</script>

<div class="px-3 sm:px-4 py-6 max-w-[1600px] mx-auto">
	<!-- Hero block -->
	<section class="grid grid-cols-12 gap-4 mb-6 fade-up">
		<div class="col-span-12 lg:col-span-8 border border-grid bg-bg-1 p-4 sm:p-6 corners relative">
			<div class="flex flex-wrap items-start justify-between gap-4 mb-4">
				<div class="min-w-0">
					<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted mb-2">// thematic event-driven research</div>
					<h1 class="font-display text-3xl sm:text-4xl lg:text-5xl font-bold leading-[1.05] tracking-tight text-fg">
						mega-cap news <span class="text-amber">→</span><br />
						second-order <span class="italic text-amber">beneficiaries</span>
					</h1>
				</div>
				<div class="text-right text-[11px] uppercase tracking-widest text-fg-muted">
					<div>latest brief</div>
					<div class="font-display font-bold text-2xl sm:text-3xl text-amber mt-1 whitespace-nowrap">
						{hasBrief ? data.latestBrief!.date : 'pending'}
					</div>
				</div>
			</div>

			<div class="max-w-2xl mt-6 space-y-3 text-fg-dim text-sm leading-relaxed">
				<p>
					Mega-caps catch the headline. The structural beneficiaries are often
					<span class="text-cyan">under-covered small-caps</span> nobody is writing about —
					that's where this pipeline looks.
				</p>
				<p>
					Six times a day (every 4h, at <span class="text-amber whitespace-nowrap">HH:30 UTC</span>) it scans S&amp;P 100 news,
					distills thematic narratives, and surfaces a ranked watchlist of small-cap
					beneficiaries.
				</p>
			</div>

			<div class="flex gap-3 mt-6">
				{#if hasBrief}
					<a
						href="/brief/{data.latestBrief!.date}"
						class="inline-flex items-center gap-2 px-4 py-2 bg-amber text-bg font-semibold text-xs uppercase tracking-widest hover:bg-amber-dim transition-colors whitespace-nowrap"
					>
						open {data.latestBrief!.date} brief
						<ArrowUpRight class="size-3" />
					</a>
					<a
						href="/briefs"
						class="inline-flex items-center gap-2 px-4 py-2 border border-grid-strong text-fg font-semibold text-xs uppercase tracking-widest hover:border-amber hover:text-amber transition-colors"
					>
						all briefs
					</a>
				{:else}
					<div
						class="inline-flex items-center gap-2 px-4 py-2 border border-grid-strong text-fg-dim text-xs uppercase tracking-widest"
					>
						no briefs yet — pipeline fires six times a day (every 4h, at <span class="whitespace-nowrap">HH:30 UTC</span>)
					</div>
				{/if}
			</div>
		</div>

		<div class="col-span-12 lg:col-span-4 grid grid-cols-2 gap-3">
			{#each stats as s, i}
				{@const toneCls =
					s.tone === 'amber'
						? 'text-amber'
						: s.tone === 'cyan'
							? 'text-cyan'
							: s.tone === 'green'
								? 'text-green'
								: 'text-magenta'}
				<div class="border border-grid bg-bg-1 p-4 fade-up" style="animation-delay: {0.1 + i * 0.05}s">
					<div class="flex items-center justify-between text-[10px] uppercase tracking-widest text-fg-muted mb-2">
						<span>{s.label}</span>
						<s.icon class="size-3 {toneCls}" />
					</div>
					<div class="font-display font-bold text-3xl {toneCls}">{s.value}</div>
				</div>
			{/each}
		</div>
	</section>

	<!-- Days timeline -->
	<section class="mb-6">
		<div class="flex items-baseline gap-3 mb-3">
			<h2 class="font-display font-bold text-lg tracking-tight">CAPTURED.SESSIONS</h2>
			<span class="text-[10px] uppercase tracking-widest text-fg-muted">[{data.days.length} runs]</span>
			<div class="flex-1 border-b border-dashed border-grid"></div>
		</div>

		{#if data.days.length === 0}
			<div class="border border-dashed border-grid bg-bg-1/40 p-8 text-center text-fg-dim text-sm">
				<div class="text-[10px] uppercase tracking-widest text-fg-muted mb-2">// no captured sessions yet</div>
				The pipeline writes briefs six times a day (every 4h, at <span class="whitespace-nowrap">HH:30 UTC</span>). After the first run completes,
				captured sessions and analytics appear here.
			</div>
		{/if}

		<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3" data-testid="session-tiles">
			{#each dayTiles as day, i (day.date)}
				<a
					href="/brief/{day.date}"
					class="group border border-grid bg-bg-1 p-4 hover:bg-bg-2 hover:border-amber transition-all fade-up block"
					style="animation-delay: {0.2 + i * 0.04}s"
				>
					<div class="flex items-start justify-between">
						<div>
							<div class="text-[10px] uppercase tracking-widest text-fg-muted">brief</div>
							<div class="font-display font-bold text-2xl text-fg group-hover:text-amber transition-colors whitespace-nowrap">
								{day.date}
							</div>
						</div>
						<ArrowUpRight class="size-4 text-fg-muted group-hover:text-amber transition-colors" />
					</div>
					<div class="mt-3 flex items-center gap-3 text-[11px] uppercase tracking-widest">
						<span class="text-cyan">{day.n_candidates}</span><span class="text-fg-muted">candidates</span>
					</div>
					<div class="mt-1 flex items-center gap-2 text-[11px]">
						<span class="text-fg-muted uppercase tracking-widest">top:</span>
						<span class="text-amber lowercase">{day.top_theme ?? '—'}</span>
					</div>
				</a>
			{/each}
		</div>
	</section>

	<!-- Two-column: top picks + theme distribution (only when we have a brief) -->
	{#if hasBrief}
	<section class="grid grid-cols-12 gap-4">
		<div class="col-span-12 lg:col-span-7 border border-grid bg-bg-1">
			<div class="flex items-baseline gap-3 px-4 py-3 border-b border-grid">
				<TrendingUp class="size-4 text-amber" />
				<h2 class="font-display font-bold text-sm tracking-widest uppercase">
					Top picks // <span class="whitespace-nowrap">{data.latestBrief!.date}</span>
				</h2>
				<span class="text-[10px] text-fg-muted uppercase tracking-widest ml-auto">rank by layer4</span>
			</div>
			<div class="divide-y divide-grid">
				{#each topCandidates as c, i}
					{@const ct = confidenceTone(c.llm_confidence)}
					<a
						href="/brief/{data.latestBrief!.date}#{c.ticker}"
						class="flex items-center gap-3 px-3 sm:px-4 py-3 hover:bg-bg-2 group transition-colors"
					>
						<div class="shrink-0 w-8 text-right font-display font-bold text-xl sm:text-2xl text-amber">
							{String(c.rank_in_day ?? i + 1).padStart(2, '0')}
						</div>
						<div class="min-w-0 flex-1">
							<div class="flex items-baseline gap-2 flex-wrap">
								<span class="font-bold text-base text-fg group-hover:text-amber transition-colors">
									{c.ticker}
								</span>
								<span class="text-[11px] text-cyan lowercase truncate">#{c.theme}</span>
							</div>
							<div class="text-[10px] text-fg-muted truncate uppercase tracking-wider">{c.company_name}</div>
						</div>
						<div class="hidden sm:block shrink-0 text-[11px] w-16">
							<div class="text-fg-muted uppercase tracking-widest">mcap</div>
							<div class="text-fg">{fmtUsdCompact(c.market_cap)}</div>
						</div>
						<div class="shrink-0 text-[11px] w-10 text-right">
							<div class="text-fg-muted uppercase tracking-widest">conf</div>
							<div
								class:text-green={ct === 'green'}
								class:text-amber={ct === 'amber'}
								class:text-cyan={ct === 'cyan'}
								class:text-fg-muted={ct === 'muted'}
							>
								{Math.round(c.llm_confidence * 5)}/5
							</div>
						</div>
						<div class="hidden md:block shrink-0 text-right text-[11px] w-16">
							<div class="text-fg-muted uppercase tracking-widest">52w hi</div>
							<div class={c.technical_pct_off_52w_high && c.technical_pct_off_52w_high < -20 ? 'text-red' : 'text-fg-dim'}>
								{fmtPct(c.technical_pct_off_52w_high)}
							</div>
						</div>
						<div class="shrink-0 flex justify-end gap-1 max-w-[60px] flex-wrap">
							{#each c.gates_passed as _g}
								<span class="dot bg-green" title="passed"></span>
							{/each}
							{#each c.gates_failed as _g}
								<span class="dot bg-red" title="failed"></span>
							{/each}
							{#each c.gates_unknown as _g}
								<span class="dot bg-fg-muted" title="unknown"></span>
							{/each}
						</div>
					</a>
				{/each}
			</div>
		</div>

		<div class="col-span-12 lg:col-span-5 grid gap-4">
			<div class="border border-grid bg-bg-1">
				<div class="flex items-baseline gap-3 px-4 py-3 border-b border-grid">
					<h2 class="font-display font-bold text-sm tracking-widest uppercase text-fg">
						theme.distribution
					</h2>
					<span class="text-[10px] text-fg-muted uppercase tracking-widest ml-auto whitespace-nowrap">
						{data.latestBrief!.date}
					</span>
				</div>
				<div class="p-4 space-y-2">
					{#each themeBars as bar, i}
						<div class="grid grid-cols-12 items-center gap-2 text-[11px]">
							<div class="col-span-4 text-fg uppercase truncate" title={bar.theme}>{bar.theme}</div>
							<div class="col-span-7 h-2 bg-bg-2 relative overflow-hidden">
								<div
									class="absolute inset-y-0 left-0 bg-amber"
									style="width: {bar.pct}%; animation: fade-up 0.5s ease-out {0.05 * i}s backwards"
								></div>
							</div>
							<div class="col-span-1 text-right text-cyan font-bold">{bar.count}</div>
						</div>
					{/each}
				</div>
			</div>

			<div class="border border-grid bg-bg-1">
				<div class="flex items-baseline gap-3 px-4 py-3 border-b border-grid">
					<h2 class="font-display font-bold text-sm tracking-widest uppercase text-fg">
						gates.outcome
					</h2>
					<span class="text-[10px] text-fg-muted uppercase tracking-widest ml-auto">
						<span class="text-green">●</span> pass
						<span class="text-red">●</span> fail
						<span class="text-fg-muted">●</span> unknown
					</span>
				</div>
				<table class="w-full text-[11px]">
					<thead>
						<tr class="text-fg-muted uppercase tracking-widest text-left">
							<th class="px-3 sm:px-4 py-2 border-b border-grid">gate</th>
							<th class="px-2 py-2 border-b border-grid text-right">pass</th>
							<th class="px-2 py-2 border-b border-grid text-right">fail</th>
							<th class="px-2 py-2 border-b border-grid text-right">unk</th>
							<th class="hidden sm:table-cell px-4 py-2 border-b border-grid w-32">distribution</th>
						</tr>
					</thead>
					<tbody>
						{#each gateStats as [gate, s]}
							{@const total = s.passed + s.failed + s.unknown}
							<tr class="hover:bg-bg-2">
								<td class="px-3 sm:px-4 py-2 text-amber uppercase font-bold">{gate}</td>
								<td class="px-2 py-2 text-right text-green">{s.passed}</td>
								<td class="px-2 py-2 text-right text-red">{s.failed}</td>
								<td class="px-2 py-2 text-right text-fg-muted">{s.unknown}</td>
								<td class="hidden sm:table-cell px-4 py-2">
									<div class="h-1.5 flex bg-bg-2">
										<div class="bg-green" style="width: {(s.passed / total) * 100}%"></div>
										<div class="bg-red" style="width: {(s.failed / total) * 100}%"></div>
										<div class="bg-fg-muted" style="width: {(s.unknown / total) * 100}%"></div>
									</div>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</div>
	</section>
	{/if}
</div>
