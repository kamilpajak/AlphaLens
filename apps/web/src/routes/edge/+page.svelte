<script lang="ts">
	import type { PageData } from './$types';
	import type { ChartPayload, EdgeOutcome } from '$lib/types';
	import { AlertTriangle, ChevronRight, Clock, Lock } from 'lucide-svelte';
	import JargonTip from '$lib/components/JargonTip.svelte';
	import ChipTip from '$lib/components/ChipTip.svelte';
	import LadderChart from '$lib/components/LadderChart.svelte';
	import LadderStatusLegend from '$lib/components/LadderStatusLegend.svelte';
	import StatusPill from '$lib/components/StatusPill.svelte';
	import WhatIfPanel from '$lib/components/WhatIfPanel.svelte';
	import { isPendingStatus, ladderStatusBody, ladderStatusLabel } from '$lib/data/ladderStatus';
	import { getEdgeChart } from '$lib/api';
	import { fmtNum } from '$lib/format';
	import {
		classificationTone,
		EXCESS_RETURN_BAR_DOMAIN,
		excessBarGeometry,
		fmtFracPct,
		fmtR,
		SIZING_MODEL_RISK_LABEL,
		statsUnlocked,
		toneClasses
	} from '$lib/edge';
	import {
		defaultDir,
		isSortKeyVisible,
		sortOutcomes,
		type SortDir,
		type SortKey
	} from '$lib/edgeSort';

	let { data }: { data: PageData } = $props();

	// Inline-accordion state for the outcomes table. Each row can expand below
	// itself into a full-width ladder-replay chart. Multiple rows may be open
	// at once (the design calls for in-place expansion, not a modal/drawer).
	//
	// `rowKey` is the compound key already used to key the {#each} loop, so the
	// expand set and the chart cache stay aligned across re-renders / filter
	// toggles. The chart payload is fetched lazily on first expand and cached
	// per row — collapsing then re-expanding does not refetch.
	const rowKey = (o: EdgeOutcome) => `${o.brief_date}::${o.ticker}`;

	type ChartState = { loading: boolean; payload: ChartPayload | null; error: boolean };

	let expanded = $state<Set<string>>(new Set());
	let chartCache = $state<Record<string, ChartState>>({});

	async function toggleRow(o: EdgeOutcome) {
		const key = rowKey(o);
		const next = new Set(expanded);
		if (next.has(key)) {
			next.delete(key);
			expanded = next;
			return;
		}
		next.add(key);
		expanded = next;

		// Lazy-load on first expand only; reuse the cache afterwards.
		if (chartCache[key]) return;
		chartCache = { ...chartCache, [key]: { loading: true, payload: null, error: false } };
		const payload = await getEdgeChart(o.brief_date, o.ticker, fetch);
		chartCache = {
			...chartCache,
			[key]: { loading: false, payload, error: payload === null }
		};
	}

	const summary = $derived(data.summary);
	const hasSummary = $derived(summary !== null);

	// Per-candidate table filter: matured (terminal) vs ongoing (open).
	type Filter = 'terminal' | 'ongoing';
	let filter = $state<Filter>('terminal');

	// Client-side sort of the outcomes table (data is already fully loaded). Default
	// `closed` desc surfaces the most-recently-completed decision at the top for the
	// terminal view; ongoing rows (no matured_at) fall back to brief_date desc.
	let sortKey = $state<SortKey>('closed');
	let sortDir = $state<SortDir>('desc');
	const valueLabel = $derived(filter === 'terminal' ? 'excess return' : 'open R');

	function toggleSort(key: SortKey) {
		if (sortKey === key) {
			sortDir = sortDir === 'asc' ? 'desc' : 'asc';
		} else {
			sortKey = key;
			sortDir = defaultDir(key);
		}
	}

	// Switching the view can hide the active sort column (terminal-only `closed`
	// / `book` have no ongoing column). Fall back to `brief` so the sort indicator
	// never lands on an invisible header.
	function setFilter(next: Filter) {
		filter = next;
		if (!isSortKeyVisible(sortKey, next)) {
			sortKey = 'brief';
			sortDir = defaultDir('brief');
		}
	}

	// Full table width: the leading expand chevron + 8 sortable headers (ticker,
	// class, value, hold, brief, closed, book, theme). The ongoing view hides the
	// two terminal-only columns (closed, % book), so the accordion detail row spans
	// TERMINAL_COLS − 2 there. Keep this in step with the <th> list below.
	const TERMINAL_COLS = 9;
	const colSpan = $derived(filter === 'terminal' ? TERMINAL_COLS : TERMINAL_COLS - 2);

	const rows = $derived(
		sortOutcomes(
			(data.outcomes ?? []).filter((o) => (filter === 'terminal' ? o.terminal : !o.terminal)),
			sortKey,
			sortDir
		)
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
					<!-- Unlocked: excess expectancy + median + hit rate + 10/50/90 quantiles. -->
					<div class="grid grid-cols-3 gap-x-4 gap-y-3 text-[10px] uppercase tracking-widest mb-3">
						<div>
							<div class="text-fg-muted">
								<JargonTip
									term="excess expectancy"
									body="Average excess return across matured trades — the candidate's raw window return minus the benchmark's return over the same window. The headline edge estimate. Gross of cost."
									formula="excess_expectancy"
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
						<div>
							<div class="text-fg-muted">
								<JargonTip
									term="hit rate"
									body="Share of matured trades that beat the benchmark — the percent with a positive market-excess return. The breadth of the edge (how OFTEN), read alongside its average size (how MUCH)."
									>hit rate</JargonTip
								>
							</div>
							<div class="text-fg text-xl font-bold normal-case whitespace-nowrap">
								{fmtFracPct(summary.edge.hit_rate, 0, false)}
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
					<div class="text-[10px] uppercase tracking-widest text-cyan">
						// suggested position sizing
					</div>
					{#if !statsUnlocked(summary.portfolio.status)}
						<Lock class="size-3 text-fg-muted" />
					{/if}
				</div>

				<div class="text-[10px] text-fg-dim italic leading-snug mt-2 mb-3">
					Per-name risk geometry only. Each member sizes independently — rescale by your own
					capital.
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
					<!--
						Suggested risk per name is the one honestly-rescalable per-name geometry
						(each member multiplies by their own capital), so it stays visible — but
						it lives HERE (gated with the sizing model) rather than in the deployment
						panel, which is N-independent / live from day one.
					-->
					<div
						class="flex items-center justify-between text-[10px] uppercase tracking-widest text-fg-muted mb-3"
					>
						<span>
							<JargonTip
								term={SIZING_MODEL_RISK_LABEL}
								body="Average risk-at-stop per trade as a percent of a 1% per-name budget (independent member sizing): position size × distance from blended entry to the disaster stop. Equal-weighted across the matured population, not a portfolio aggregate."
								formula="mean_risk">{SIZING_MODEL_RISK_LABEL}</JargonTip
							>
						</span>
						<span class="text-fg-dim font-bold normal-case whitespace-nowrap">
							{fmtFracPct(summary.portfolio.mean_realized_risk_pct, 2, false)}
						</span>
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

		<!-- WHAT-IF sandbox — display-only, in-sample counterfactual exit-stop lenses.
		     Collapsed by default; the realized panels above stay the production view.
		     Never a primary KPI card. -->
		<WhatIfPanel {summary} />

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
						onclick={() => setFilter('terminal')}
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
						onclick={() => setFilter('ongoing')}
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
				{#snippet sortHead(key: SortKey, label: string, cls: string)}
					<th
						class="py-2 pr-3 {cls}"
						aria-sort={sortKey === key ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
					>
						<button
							type="button"
							onclick={() => toggleSort(key)}
							class="group/s inline-flex items-center gap-1 uppercase tracking-widest transition-colors {sortKey ===
							key
								? 'text-amber'
								: 'text-fg-muted hover:text-fg-dim'}"
						>
							<span>{label}</span>
							<span
								class="text-[8px] leading-none {sortKey === key
									? 'opacity-100'
									: 'opacity-0 group-hover/s:opacity-50'}"
								aria-hidden="true">{(sortKey === key ? sortDir : defaultDir(key)) === 'asc' ? '▲' : '▼'}</span>
						</button>
					</th>
				{/snippet}

				<table class="w-full text-sm">
					<thead>
						<tr
							class="text-[10px] uppercase tracking-widest text-fg-muted text-left border-b border-grid"
						>
							<th class="py-2 pr-1 w-4" aria-label="expand"></th>
							{@render sortHead('ticker', 'ticker', '')}
							{@render sortHead('class', 'class', '')}
							{@render sortHead('value', valueLabel, 'min-w-[8rem]')}
							{@render sortHead('hold', 'hold', 'hidden sm:table-cell text-right')}
							{@render sortHead('brief', 'brief', 'hidden sm:table-cell text-right')}
							{#if filter === 'terminal'}
								{@render sortHead('closed', 'closed', 'hidden sm:table-cell text-right')}
								{@render sortHead('book', '% book', 'hidden md:table-cell text-right')}
							{/if}
							{@render sortHead('theme', 'theme', 'hidden md:table-cell')}
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
							{@const key = rowKey(o)}
							{@const isOpen = expanded.has(key)}
							{@const chart = chartCache[key]}
							<tr
								class="border-b border-grid hover:bg-bg-2 group cursor-pointer"
								onclick={() => toggleRow(o)}
								aria-expanded={isOpen}
							>
								<td class="py-2.5 pr-1 align-middle">
									<ChevronRight
										class="size-3.5 text-fg-muted group-hover:text-amber transition-transform {isOpen
											? 'rotate-90'
											: ''}"
										aria-hidden="true"
									/>
								</td>
								<td class="py-2.5 pr-3">
									<!-- Stop click propagation so the ticker link navigates to the
									     brief instead of toggling the row. -->
									<a
										href="/brief/{o.brief_date}"
										onclick={(e) => e.stopPropagation()}
										class="font-display font-bold text-base text-fg group-hover:text-amber transition-colors whitespace-nowrap"
									>
										{o.ticker}
									</a>
								</td>
								<td class="py-2.5 pr-3">
									<ChipTip
										term={ladderStatusLabel(o.ladder_classification)}
										body={ladderStatusBody(o.ladder_classification)}
									>
										{#snippet chip()}
											<StatusPill
												tone={toneClasses(tone)}
												label={ladderStatusLabel(o.ladder_classification)}
												size="9"
												nowrap
												dashed={isPendingStatus(o.ladder_classification)}
												class="inline-block"
											/>
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
								<td class="hidden sm:table-cell py-2.5 pr-3 text-right text-fg-dim whitespace-nowrap">
									{o.brief_date.slice(5)}
								</td>
								{#if filter === 'terminal'}
									<td class="hidden sm:table-cell py-2.5 pr-3 text-right text-fg-dim whitespace-nowrap">
										{o.matured_at ? o.matured_at.slice(5) : '—'}
									</td>
									<td class="hidden md:table-cell py-2.5 pr-3 text-right text-fg-dim whitespace-nowrap">
										{fmtFracPct(o.realized_return_pct_of_book, 2)}
									</td>
								{/if}
								<td class="hidden md:table-cell py-2.5 pr-3 max-w-[140px]">
									<div>
										<span class="text-amber lowercase truncate">
											{#if !o.terminal}
												<Clock class="inline size-3 text-fg-muted" aria-label="ongoing" />
											{/if}
											{o.theme ?? '—'}
										</span>
									</div>
								</td>
							</tr>
							{#if isOpen}
								<!-- Inline-accordion detail row: full-width ladder-replay chart,
								     lazy-mounted on first expand. -->
								<tr class="border-b border-grid bg-bg-2/40">
									<td colspan={colSpan} class="px-2 sm:px-4 py-4">
										<div class="border border-grid bg-bg-1 px-4 sm:px-5 py-4">
											{#if !chart || chart.loading}
												<div class="text-[10px] uppercase tracking-widest text-fg-muted py-6 text-center">
													loading chart…
												</div>
											{:else if chart.error || chart.payload === null}
												<div
													class="border border-dashed border-grid-strong px-3 py-4 text-[11px]"
												>
													<div class="text-fg-muted uppercase tracking-widest mb-1">
														chart unavailable
													</div>
													<p class="text-fg-dim leading-relaxed">
														Could not load the ladder replay for
														<span class="whitespace-nowrap">{o.ticker}</span>
														on
														<span class="whitespace-nowrap">{o.brief_date}</span>. The edge API
														may be offline or the window not yet recomputed.
													</p>
												</div>
											{:else}
												<LadderChart payload={chart.payload} />
											{/if}
										</div>
									</td>
								</tr>
							{/if}
						{/each}
					</tbody>
				</table>
			{/if}
		</section>
	{/if}
</div>
