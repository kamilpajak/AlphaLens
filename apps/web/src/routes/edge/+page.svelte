<script lang="ts">
	import type { PageData } from './$types';
	import type { EdgeOutcome } from '$lib/types';
	import { AlertTriangle, Clock, Lock } from 'lucide-svelte';
	import JargonTip from '$lib/components/JargonTip.svelte';
	import ChipTip from '$lib/components/ChipTip.svelte';
	import LadderStatusLegend from '$lib/components/LadderStatusLegend.svelte';
	import { ladderStatusBody } from '$lib/data/ladderStatus';
	import { fmtNum } from '$lib/format';
	import {
		classificationTone,
		EXCESS_RETURN_BAR_DOMAIN,
		excessBarGeometry,
		fmtFracPct,
		fmtR,
		statsUnlocked,
		toneClasses
	} from '$lib/edge';

	let { data }: { data: PageData } = $props();

	const summary = $derived(data.summary);
	const hasSummary = $derived(summary !== null);

	// Per-candidate table filter: matured (terminal) vs ongoing (open).
	type Filter = 'terminal' | 'ongoing';
	let filter = $state<Filter>('terminal');

	const rows = $derived(
		(data.outcomes ?? []).filter((o) => (filter === 'terminal' ? o.terminal : !o.terminal))
	);

	// Counts for the filter chips — computed off the full outcome list so both
	// counts are stable as the user toggles.
	const nTerminal = $derived((data.outcomes ?? []).filter((o) => o.terminal).length);
	const nOngoing = $derived((data.outcomes ?? []).filter((o) => !o.terminal).length);

</script>

<div class="px-3 sm:px-4 py-8 max-w-[1200px] mx-auto">
	<!-- Header -->
	<div class="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1 fade-up">
		<h1 class="font-display font-bold text-2xl sm:text-3xl tracking-tight">EDGE</h1>
		<span class="text-[10px] uppercase tracking-widest text-fg-muted">
			// market-behavior ledger
		</span>
		<span class="text-[10px] uppercase tracking-widest text-fg-muted">
			(exploratory · gross · telemetry)
		</span>
		<div class="flex-1 border-b border-dashed border-grid"></div>
		{#if hasSummary && summary}
			<span class="text-[10px] uppercase tracking-widest text-fg-muted whitespace-nowrap">
				[pop {summary.n_plannable} · matured {summary.n_matured}]
			</span>
		{/if}
	</div>

	<!-- Top caveat strip (§3.1 / §3.4 / §3.11). Always shown — these are the
	     binding interpretation guardrails for everything below. -->
	<div
		class="mb-6 flex items-start gap-2 border border-amber/50 bg-amber/5 px-3 py-2 text-[11px] leading-snug text-fg-dim fade-up"
	>
		<AlertTriangle class="mt-0.5 size-3.5 shrink-0 text-amber" />
		<p>
			<span class="text-amber font-bold uppercase tracking-widest text-[10px]">exploratory</span>
			— hypothesis-generation only, never confirmatory. All R is
			<span class="whitespace-nowrap">gross / pre-cost</span>
			(no spread, slippage or commission). Headline is
			<span class="whitespace-nowrap">excess-of-benchmark</span>
			({summary?.benchmark ?? 'SPY'}) over the same window — raw R mostly reports market beta.
			This is a mechanical ladder triggered by the screener: ladder ≠ actual execution ≠ group
			P&amp;L.
		</p>
	</div>

	{#if !hasSummary || !summary}
		<!-- Whole-page fallback: API offline / empty. -->
		<div class="border border-dashed border-grid-strong px-4 py-10 text-center fade-up">
			<div class="text-[10px] uppercase tracking-widest text-fg-muted mb-2">no edge data</div>
			<p class="text-sm text-fg-dim">
				The market-behavior ledger is unavailable. The nightly population-monitor sweep populates it
				as candidate windows mature.
			</p>
		</div>
	{:else}
		<!-- Three top panels: EDGE · PORTFOLIO · DEPLOYMENT -->
		<div class="grid grid-cols-1 lg:grid-cols-12 gap-4 mb-6">
			<!-- EDGE panel (N-gated) -->
			<section
				class="lg:col-span-5 border border-grid bg-bg-1 p-4 sm:p-5 corners fade-up"
				data-testid="edge-panel"
			>
				<div class="flex items-center justify-between gap-2 mb-3">
					<div class="text-[10px] uppercase tracking-widest text-cyan">
						// edge (excess return, matured)
					</div>
					{#if !statsUnlocked(summary.edge.status)}
						<Lock class="size-3 text-fg-muted" />
					{:else if summary.edge.status === 'early'}
						<span class="text-[9px] uppercase tracking-widest text-amber whitespace-nowrap">
							early
						</span>
					{/if}
				</div>

				{#if !statsUnlocked(summary.edge.status)}
					<!-- §3.2 hard N-gate: hide all stats, show counts only. -->
					<div class="text-fg-muted text-[11px] uppercase tracking-widest mb-1">
						ⓘ insufficient data
					</div>
					<div class="text-2xl font-display font-bold text-fg-dim whitespace-nowrap">
						n matured = {summary.edge.n_matured}
						<span class="text-sm text-fg-muted">(&lt; {summary.edge.threshold})</span>
					</div>
					<div class="mt-2 text-[10px] uppercase tracking-widest text-fg-muted">
						[unlocks at n ≥ {summary.edge.threshold}]
					</div>
				{:else}
					<!-- Unlocked: excess expectancy + median + 10/50/90 quantiles. -->
					<div class="grid grid-cols-2 gap-x-6 gap-y-3 text-[10px] uppercase tracking-widest mb-3">
						<div>
							<div class="text-fg-muted">
								<JargonTip
									term="excess expectancy"
									body="Average excess return across matured trades — the candidate's raw window return minus the benchmark's return over the same window. The headline edge estimate. Gross of cost."
									>excess expectancy</JargonTip
								>
							</div>
							<div class="text-fg text-xl font-bold normal-case whitespace-nowrap">
								{fmtFracPct(summary.edge.market_excess_mean, 1)}
							</div>
						</div>
						<div>
							<div class="text-fg-muted">
								<JargonTip
									term="median"
									body="The middle matured excess-return value. Less swayed by a single outlier than the mean — read alongside it."
									>median</JargonTip
								>
							</div>
							<div class="text-fg text-xl font-bold normal-case whitespace-nowrap">
								{fmtFracPct(summary.edge.market_excess_median, 1)}
							</div>
						</div>
					</div>
					<div class="grid grid-cols-3 gap-x-4 text-[10px] uppercase tracking-widest mb-3">
						<div>
							<div class="text-fg-muted">
								<JargonTip
									term="p10"
									body="10th-percentile excess return — the left tail. A tenth of matured trades did worse than this."
									>p10</JargonTip
								>
							</div>
							<div class="text-red font-bold normal-case whitespace-nowrap">
								{fmtFracPct(summary.edge.market_excess_quantiles.p10, 1)}
							</div>
						</div>
						<div>
							<div class="text-fg-muted">
								<JargonTip
									term="p50"
									body="Median excess return (50th percentile)."
									>p50</JargonTip
								>
							</div>
							<div class="text-fg font-bold normal-case whitespace-nowrap">
								{fmtFracPct(summary.edge.market_excess_quantiles.p50, 1)}
							</div>
						</div>
						<div>
							<div class="text-fg-muted">
								<JargonTip
									term="p90"
									body="90th-percentile excess return — the right tail. A tenth of matured trades did better than this."
									>p90</JargonTip
								>
							</div>
							<div class="text-green font-bold normal-case whitespace-nowrap">
								{fmtFracPct(summary.edge.market_excess_quantiles.p90, 1)}
							</div>
						</div>
					</div>
					<div class="border-t border-grid pt-2 flex items-center justify-between text-[10px] uppercase tracking-widest text-fg-muted">
						<span>
							<JargonTip
								term="gross R"
								full="raw realized R"
								body="Raw long-only realized R (NOT excess of benchmark). Includes market beta and regime, so it flatters in a bull market — shown only as a de-emphasized gross-P&L proxy, not the edge."
								>gross R</JargonTip
							>
							<span class="text-fg-dim font-bold normal-case whitespace-nowrap">
								{fmtR(summary.edge.gross_realized_r_mean)}
							</span>
						</span>
						<span class="whitespace-nowrap">
							hold p50 {fmtNum(summary.edge.holding_days_p50, 0)}d · p95
							{fmtNum(summary.edge.holding_days_p95, 0)}d
						</span>
					</div>
				{/if}
			</section>

			<!-- PORTFOLIO panel (N-gated) -->
			<section
				class="lg:col-span-4 border border-grid bg-bg-1 p-4 sm:p-5 corners fade-up"
				data-testid="portfolio-panel"
			>
				<div class="flex items-center justify-between gap-2 mb-3">
					<div class="text-[10px] uppercase tracking-widest text-cyan">// portfolio (size-wtd)</div>
					{#if !statsUnlocked(summary.portfolio.status)}
						<Lock class="size-3 text-fg-muted" />
					{/if}
				</div>

				{#if !statsUnlocked(summary.portfolio.status)}
					<div class="text-fg-muted text-[11px] uppercase tracking-widest mb-1">
						ⓘ insufficient data
					</div>
					<div class="text-2xl font-display font-bold text-fg-dim whitespace-nowrap">
						n matured = {summary.portfolio.n_matured}
						<span class="text-sm text-fg-muted">(&lt; {summary.portfolio.threshold})</span>
					</div>
				{:else}
					<div class="grid grid-cols-1 gap-y-3 text-[10px] uppercase tracking-widest">
						<div>
							<div class="text-fg-muted">
								<JargonTip
									term="size-weighted R"
									body="Realized R weighted by each trade's risk-as-percent-of-book — what the population's edge looks like once position sizing is folded in, rather than equal-weighting every name."
									>size-weighted R</JargonTip
								>
							</div>
							<div class="text-fg text-xl font-bold normal-case whitespace-nowrap">
								{fmtR(summary.portfolio.size_weighted_realized_r)}
							</div>
						</div>
						<div>
							<div class="text-fg-muted">
								<JargonTip
									term="book contribution"
									body="Sum of every matured trade's realized return as a percent of the whole book — the aggregate P&L contribution of the surfaced population. Gross of cost."
									>book contribution</JargonTip
								>
							</div>
							<div class="text-fg text-lg font-bold normal-case whitespace-nowrap">
								{fmtFracPct(summary.portfolio.total_realized_contribution_pct_of_book, 2)}
							</div>
						</div>
						<div class="flex items-center justify-between text-fg-muted">
							<span>
								<JargonTip
									term="mean risk%"
									body="Average risk-at-stop per trade as a percent of the book: position size × distance from blended entry to the disaster stop."
									>mean risk%</JargonTip
								>
								<span class="text-fg-dim font-bold normal-case whitespace-nowrap">
									{fmtFracPct(summary.portfolio.mean_realized_risk_pct, 2, false)}
								</span>
							</span>
							<span class="whitespace-nowrap">
								tiers x̄ {fmtNum(summary.portfolio.mean_tiers_filled_count, 1)}
							</span>
						</div>
					</div>
				{/if}
			</section>

			<!-- DEPLOYMENT panel (N-INDEPENDENT — live from day one) -->
			<section
				class="lg:col-span-3 border border-grid bg-bg-1 p-4 sm:p-5 corners fade-up"
				data-testid="deployment-panel"
			>
				<div class="text-[10px] uppercase tracking-widest text-cyan mb-3">// deployment</div>
				<div class="grid grid-cols-1 gap-y-3 text-[10px] uppercase tracking-widest">
					<div class="flex items-center justify-between">
						<span class="text-fg-muted">
							<JargonTip
								term="fill-rate"
								body="Share of terminal candidates where at least one entry tier filled. Measures how often the limit-buy ladder actually engaged — independent of the N-gate, so it is live from day one."
								>fill-rate</JargonTip
							>
						</span>
						<span class="text-fg text-base font-bold normal-case whitespace-nowrap">
							{fmtFracPct(summary.deployment.fill_rate, 0, false)}
						</span>
					</div>
					<div class="flex items-center justify-between">
						<span class="text-fg-muted">
							<JargonTip
								term="no-fill"
								body="Share of terminal candidates where no entry tier filled — the limit ladder never engaged within its time-to-live."
								>no-fill</JargonTip
							>
						</span>
						<span class="text-fg-dim font-bold normal-case whitespace-nowrap">
							{fmtFracPct(summary.deployment.no_fill_rate, 0, false)}
						</span>
					</div>
					<div class="flex items-center justify-between">
						<span class="text-fg-muted">
							<JargonTip
								term="tiers x̄"
								body="Average number of the three entry tiers that filled across terminal candidates."
								>tiers x̄</JargonTip
							>
						</span>
						<span class="text-fg-dim font-bold normal-case whitespace-nowrap">
							{fmtNum(summary.deployment.mean_tiers_filled_count, 1)}
						</span>
					</div>
					<div class="border-t border-grid pt-2 text-fg-muted whitespace-nowrap">
						{summary.deployment.n_filled}/{summary.deployment.n_terminal} filled
					</div>
				</div>
			</section>
		</div>

		<!-- OPEN POSITIONS — descriptive only (§3.3). Never a mean open_R. -->
		<section class="mb-6 fade-up" data-testid="open-positions">
			<div class="mb-2 flex items-baseline gap-2">
				<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted">
					// open positions (descriptive only — excluded from expectancy)
				</div>
				<div class="flex-1 border-b border-dashed border-grid"></div>
			</div>
			<div class="flex flex-wrap items-center gap-x-6 gap-y-2 text-[11px] uppercase tracking-widest">
				<span class="text-fg-muted">
					open <span class="text-cyan font-bold">{summary.open_positions.n_open}</span>
				</span>
				<span class="text-fg-muted">
					<JargonTip
						term="near-TP"
						body="Open positions trading close to their take-profit target. A descriptive count of where live positions sit — never reduced to an average return."
						>near-TP</JargonTip
					>
					<span class="text-green font-bold">{summary.open_positions.near_tp}</span>
				</span>
				<span class="text-fg-muted">
					<JargonTip
						term="near-SL"
						body="Open positions trading close to their stop. Descriptive only — open positions are censoring-biased (fast losers close, slow winners stay open), so they are never pooled into expectancy."
						>near-SL</JargonTip
					>
					<span class="text-red font-bold">{summary.open_positions.near_sl}</span>
				</span>
				<span class="text-fg-muted italic lowercase tracking-normal normal-case">
					{summary.open_positions.note}
				</span>
			</div>
		</section>

		<!-- PER-CANDIDATE OUTCOMES table -->
		<section class="fade-up" data-testid="outcomes-table">
			<LadderStatusLegend />
			<div class="mb-3 flex flex-wrap items-center justify-between gap-2">
				<div class="flex items-baseline gap-2 flex-1 min-w-[12rem]">
					<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted">
						// per-candidate outcomes
					</div>
					<div class="flex-1 border-b border-dashed border-grid"></div>
				</div>
				<div class="flex items-center gap-1 text-[10px] uppercase tracking-widest">
					<button
						type="button"
						class="px-2 py-1 border transition-colors"
						class:border-amber={filter === 'terminal'}
						class:text-amber={filter === 'terminal'}
						class:border-grid={filter !== 'terminal'}
						class:text-fg-muted={filter !== 'terminal'}
						onclick={() => (filter = 'terminal')}
					>
						terminal [{nTerminal}]
					</button>
					<button
						type="button"
						class="px-2 py-1 border transition-colors"
						class:border-amber={filter === 'ongoing'}
						class:text-amber={filter === 'ongoing'}
						class:border-grid={filter !== 'ongoing'}
						class:text-fg-muted={filter !== 'ongoing'}
						onclick={() => (filter = 'ongoing')}
					>
						ongoing [{nOngoing}]
					</button>
				</div>
			</div>

			{#if rows.length === 0}
				<div class="border border-dashed border-grid-strong px-4 py-8 text-center text-sm text-fg-muted">
					no {filter} outcomes in window
				</div>
			{:else}
				<table class="w-full text-sm">
					<thead>
						<tr
							class="text-[10px] uppercase tracking-widest text-fg-muted text-left border-b border-grid"
						>
							<th class="py-2 pr-3">ticker</th>
							<th class="py-2 pr-3">class</th>
							<th class="py-2 pr-3">
								{#if filter === 'terminal'}
									<JargonTip
										term="excess return"
										body="Raw window return minus the benchmark return over the same arrival-to-exit window. Centered bar: zero in the middle, green right (beat the benchmark), red left (trailed it). Gross of cost."
										>excess return</JargonTip
									>
								{:else}
									<JargonTip
										term="open R"
										body="Unrealized R-multiple of the open position — current paper P&L in units of the trade's initial risk. Not yet benchmark-excess; the row matures to an excess-return figure on exit."
										>open R</JargonTip
									>
								{/if}
							</th>
							<th class="hidden sm:table-cell py-2 pr-3 text-right">hold</th>
							<th class="hidden md:table-cell py-2 pr-3 text-right">% book</th>
							<th class="hidden md:table-cell py-2 pr-3">theme</th>
						</tr>
					</thead>
					<tbody>
						{#each rows as o (o.brief_date + o.ticker)}
							{@const tone = classificationTone(o.ladder_classification)}
							{@const rValue = o.terminal ? o.market_excess_return : o.open_r}
							<!-- Terminal value is an excess RETURN (fraction → % units); ongoing is an
							     R-multiple. The bar domain differs accordingly. -->
							{@const bar = excessBarGeometry(
								rValue,
								o.terminal ? EXCESS_RETURN_BAR_DOMAIN : 1.0
							)}
							<tr class="border-b border-grid hover:bg-bg-2 group">
								<td class="py-2.5 pr-3">
									<a
										href="/brief/{o.brief_date}"
										class="font-display font-bold text-base text-fg group-hover:text-amber transition-colors whitespace-nowrap"
									>
										{o.ticker}
									</a>
								</td>
								<td class="py-2.5 pr-3">
									<ChipTip term={o.ladder_classification} body={ladderStatusBody(o.ladder_classification)}>
										{#snippet chip()}
											<span
												class="inline-block px-1.5 py-0.5 border text-[9px] uppercase tracking-widest whitespace-nowrap {toneClasses(
													tone
												)}"
											>
												{o.ladder_classification}
											</span>
										{/snippet}
									</ChipTip>
								</td>
								<td class="py-2.5 pr-3 min-w-[8rem]">
									<div class="flex items-center gap-2">
										<span
											class="font-bold text-xs whitespace-nowrap w-14 text-right shrink-0"
											class:text-green={o.terminal ? bar.positive : false}
											class:text-red={o.terminal ? !bar.positive && rValue != null : false}
											class:text-cyan={!o.terminal}
											class:text-fg-muted={rValue == null}
										>
											{#if o.terminal}
												{fmtFracPct(o.market_excess_return, 1)}
											{:else}
												{fmtR(o.open_r)}
											{/if}
										</span>
										{#if !o.terminal}
											<!-- "open" label as its own flex item so it never overlaps the bar -->
											<span class="text-[9px] text-fg-muted whitespace-nowrap shrink-0">open</span>
										{/if}
										<!-- Centered excess bar: 0 in the middle. -->
										<div class="relative h-1.5 flex-1 bg-bg-3 overflow-hidden">
											<!-- center tick -->
											<div
												class="absolute inset-y-0 left-1/2 w-px bg-grid-strong"
												aria-hidden="true"
											></div>
											{#if rValue != null}
												<div
													class="absolute inset-y-0"
													class:bg-green={o.terminal ? bar.positive : false}
													class:bg-red={o.terminal ? !bar.positive : false}
													class:bg-cyan={!o.terminal}
													style="left: {bar.left}%; width: {bar.width}%"
												></div>
											{/if}
										</div>
									</div>
								</td>
								<td class="hidden sm:table-cell py-2.5 pr-3 text-right text-fg-dim whitespace-nowrap">
									{o.holding_days_elapsed != null ? `${o.holding_days_elapsed}d` : '—'}
								</td>
								<td class="hidden md:table-cell py-2.5 pr-3 text-right text-fg-dim whitespace-nowrap">
									{o.terminal ? fmtFracPct(o.realized_return_pct_of_book, 2) : '—'}
								</td>
								<td class="hidden md:table-cell py-2.5 pr-3 text-amber lowercase truncate max-w-[140px]">
									{#if !o.terminal}
										<Clock class="inline size-3 text-fg-muted" aria-label="ongoing" />
									{/if}
									{o.theme ?? '—'}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</section>
	{/if}
</div>
