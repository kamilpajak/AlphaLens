<script lang="ts">
	import { ArrowUpRight, Calendar, Layers, Sparkles, Target, TrendingUp } from 'lucide-svelte';
	import type { PageData } from './$types';
	import { confidenceTone, fmtUsdCompact, fmtPct } from '$lib/format';

	let { data }: { data: PageData } = $props();

	const totalCandidates = $derived(data.days.reduce((s, d) => s + d.n_candidates, 0));
	const avgConf = $derived(
		data.latestBrief.candidates.length
			? data.latestBrief.candidates.reduce((s, c) => s + c.gemini_confidence, 0) / data.latestBrief.candidates.length
			: 0
	);

	const themeBars = $derived(
		Object.entries(data.latestBrief.theme_counts)
			.sort(([, a], [, b]) => b - a)
			.map(([theme, count]) => ({
				theme,
				count,
				pct: (count / data.latestBrief.n_candidates) * 100
			}))
	);

	const gateStats = $derived.by(() => {
		const stats: Record<string, { passed: number; failed: number; unknown: number }> = {};
		for (const c of data.latestBrief.candidates) {
			for (const g of c.gates_passed) stats[g] = { ...(stats[g] ?? { passed: 0, failed: 0, unknown: 0 }), passed: (stats[g]?.passed ?? 0) + 1 };
			for (const g of c.gates_failed) stats[g] = { ...(stats[g] ?? { passed: 0, failed: 0, unknown: 0 }), failed: (stats[g]?.failed ?? 0) + 1 };
			for (const g of c.gates_unknown) stats[g] = { ...(stats[g] ?? { passed: 0, failed: 0, unknown: 0 }), unknown: (stats[g]?.unknown ?? 0) + 1 };
		}
		return Object.entries(stats).sort(([a], [b]) => a.localeCompare(b));
	});

	const topCandidates = $derived(data.latestBrief.candidates.slice(0, 8));

	type Tone = 'amber' | 'cyan' | 'green' | 'magenta';
	const stats: { icon: typeof Calendar; label: string; value: string; tone: Tone }[] = $derived([
		{ icon: Calendar, label: 'days', value: String(data.days.length), tone: 'amber' },
		{ icon: Target, label: 'candidates', value: String(totalCandidates), tone: 'cyan' },
		{ icon: Layers, label: 'themes (latest)', value: String(data.latestBrief.n_themes), tone: 'green' },
		{ icon: Sparkles, label: 'avg conf', value: (avgConf * 5).toFixed(1) + '/5', tone: 'magenta' }
	]);
</script>

<div class="px-4 py-6 max-w-[1600px] mx-auto">
	<!-- Hero block -->
	<section class="grid grid-cols-12 gap-4 mb-6 fade-up">
		<div class="col-span-12 lg:col-span-8 border border-grid bg-bg-1 p-6 corners relative">
			<div class="flex items-start justify-between mb-4">
				<div>
					<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted mb-2">// thematic event-driven research</div>
					<h1 class="font-display text-4xl lg:text-5xl font-bold leading-[1.05] tracking-tight text-fg">
						mega-cap news <span class="text-amber">→</span><br />
						second-order <span class="italic text-amber">beneficiaries</span>
					</h1>
				</div>
				<div class="text-right text-[11px] uppercase tracking-widest text-fg-muted">
					<div>latest brief</div>
					<div class="font-display font-bold text-3xl text-amber mt-1">{data.latestBrief.date}</div>
				</div>
			</div>

			<p class="text-fg-dim max-w-2xl text-sm leading-relaxed mt-6">
				Layer 1 detects S&amp;P 100 news. Layer 2 (gemini-2.5-flash) extracts themes. Layer 3
				(gemini-3-pro-preview) maps to small-cap beneficiaries.
				<span class="text-cyan">Four verification gates</span> — press · insider · ETF · 10-K — gate
				the cohort. Layer 4 ranks survivors via insider × FCFF × Magic Formula × technicals ×
				catalyst-floor. Layer 5 renders briefs for the WhatsApp investing group.
			</p>

			<div class="flex gap-3 mt-6">
				<a
					href="/brief/{data.latestBrief.date}"
					class="inline-flex items-center gap-2 px-4 py-2 bg-amber text-bg font-semibold text-xs uppercase tracking-widest hover:bg-amber-dim transition-colors"
				>
					open {data.latestBrief.date} brief
					<ArrowUpRight class="size-3" />
				</a>
				<a
					href="/briefs"
					class="inline-flex items-center gap-2 px-4 py-2 border border-grid-strong text-fg font-semibold text-xs uppercase tracking-widest hover:border-amber hover:text-amber transition-colors"
				>
					all briefs
				</a>
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

		<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
			{#each data.days as day, i}
				<a
					href="/brief/{day.date}"
					class="group border border-grid bg-bg-1 p-4 hover:bg-bg-2 hover:border-amber transition-all fade-up block"
					style="animation-delay: {0.2 + i * 0.04}s"
				>
					<div class="flex items-start justify-between">
						<div>
							<div class="text-[10px] uppercase tracking-widest text-fg-muted">brief</div>
							<div class="font-display font-bold text-2xl text-fg group-hover:text-amber transition-colors">
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

	<!-- Two-column: top picks + theme distribution -->
	<section class="grid grid-cols-12 gap-4">
		<div class="col-span-12 lg:col-span-7 border border-grid bg-bg-1">
			<div class="flex items-baseline gap-3 px-4 py-3 border-b border-grid">
				<TrendingUp class="size-4 text-amber" />
				<h2 class="font-display font-bold text-sm tracking-widest uppercase">
					Top picks // {data.latestBrief.date}
				</h2>
				<span class="text-[10px] text-fg-muted uppercase tracking-widest ml-auto">rank by layer4</span>
			</div>
			<div class="divide-y divide-grid">
				{#each topCandidates as c, i}
					<a
						href="/brief/{data.latestBrief.date}#{c.ticker}"
						class="grid grid-cols-12 items-center gap-3 px-4 py-3 hover:bg-bg-2 group transition-colors"
					>
						<div class="col-span-1 text-right font-display font-bold text-2xl text-amber">
							{String(c.rank_in_day ?? i + 1).padStart(2, '0')}
						</div>
						<div class="col-span-3">
							<div class="font-bold text-base text-fg group-hover:text-amber transition-colors">
								{c.ticker}
							</div>
							<div class="text-[10px] text-fg-muted truncate uppercase tracking-wider">{c.company_name}</div>
						</div>
						<div class="col-span-2 text-[11px]">
							<div class="text-fg-muted uppercase tracking-widest">theme</div>
							<div class="text-cyan lowercase truncate">{c.theme}</div>
						</div>
						<div class="col-span-2 text-[11px]">
							<div class="text-fg-muted uppercase tracking-widest">mcap</div>
							<div class="text-fg">{fmtUsdCompact(c.market_cap)}</div>
						</div>
						{@const ct = confidenceTone(c.gemini_confidence)}
						<div class="col-span-1 text-[11px]">
							<div class="text-fg-muted uppercase tracking-widest">conf</div>
							<div
								class:text-green={ct === 'green'}
								class:text-amber={ct === 'amber'}
								class:text-cyan={ct === 'cyan'}
								class:text-fg-muted={ct === 'muted'}
							>
								{Math.round(c.gemini_confidence * 5)}/5
							</div>
						</div>
						<div class="col-span-2 text-right text-[11px]">
							<div class="text-fg-muted uppercase tracking-widest">52w high</div>
							<div class={c.technical_pct_off_52w_high && c.technical_pct_off_52w_high < -20 ? 'text-red' : 'text-fg-dim'}>
								{fmtPct(c.technical_pct_off_52w_high)}
							</div>
						</div>
						<div class="col-span-1 flex justify-end gap-1">
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
					<span class="text-[10px] text-fg-muted uppercase tracking-widest ml-auto">
						{data.latestBrief.date}
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
							<th class="px-4 py-2 border-b border-grid">gate</th>
							<th class="px-2 py-2 border-b border-grid text-right">pass</th>
							<th class="px-2 py-2 border-b border-grid text-right">fail</th>
							<th class="px-2 py-2 border-b border-grid text-right">unknown</th>
							<th class="px-4 py-2 border-b border-grid w-32">distribution</th>
						</tr>
					</thead>
					<tbody>
						{#each gateStats as [gate, s]}
							{@const total = s.passed + s.failed + s.unknown}
							<tr class="hover:bg-bg-2">
								<td class="px-4 py-2 text-amber uppercase font-bold">{gate}</td>
								<td class="px-2 py-2 text-right text-green">{s.passed}</td>
								<td class="px-2 py-2 text-right text-red">{s.failed}</td>
								<td class="px-2 py-2 text-right text-fg-muted">{s.unknown}</td>
								<td class="px-4 py-2">
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
</div>
