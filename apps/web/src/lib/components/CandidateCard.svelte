<script lang="ts">
	import type { Candidate } from '$lib/types';
	import { fmtUsdCompact, fmtPct, fmtNum, fmtPctile, fmtDate, confidenceTone } from '$lib/format';
	import { ArrowUpRight, ExternalLink, Sparkle } from 'lucide-svelte';
	import SignalBar from './SignalBar.svelte';
	import GatePill from './GatePill.svelte';
	import JargonTip from './JargonTip.svelte';
	import ChipTip from './ChipTip.svelte';
	import { GLOSSARY_BY_TERM } from '$lib/data/glossary';

	// Same tipProps pattern as /experiments — looks up term in shared glossary.
	function tipProps(term: string) {
		const g = GLOSSARY_BY_TERM.get(term);
		return { term: g?.term ?? term, full: g?.full ?? '', body: g?.body ?? '' };
	}

	interface Props {
		candidate: Candidate;
		index: number;
	}
	let { candidate: c, index }: Props = $props();

	const conf5 = $derived(Math.round(c.gemini_confidence * 5));
	const confTone = $derived(confidenceTone(c.gemini_confidence));
	const rank = $derived(c.rank_in_day ?? index + 1);
	const cohort = $derived(c.cohort_size_in_day ?? '?');

	let expanded = $state(false);
</script>

<article
	id={c.ticker}
	class="border border-grid bg-bg-1 fade-up isolate"
	style="animation-delay: {index * 0.04}s"
>
	<!-- Header strip: rank + title + 4 metric tiles on one wrapping flex row.
	     On lg+, metrics align right of the title block (fills the previously
	     empty right gutter). On mobile/sm, metrics wrap below as full-width
	     4-col grid. -->
	<header class="px-4 sm:px-5 py-3 border-b border-grid bg-gradient-to-r from-bg-2 to-bg-1">
		<div class="flex flex-wrap items-start gap-x-4 gap-y-3 sm:gap-x-6">
			<div class="text-right shrink-0">
				<div class="font-display font-bold text-3xl sm:text-4xl text-amber leading-none">
					{String(rank).padStart(2, '0')}
				</div>
				<div class="text-[9px] uppercase tracking-widest text-fg-muted mt-1">/{cohort}</div>
			</div>
			<div class="min-w-0 flex-1 basis-[200px]">
				<div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
					<h3 class="font-display font-bold text-xl sm:text-2xl text-fg">{c.ticker}</h3>
					<!-- Pattern tags: REVERSAL is currently the only one. When a 2nd pattern -->
					<!-- (e.g. BREAKOUT, INSIDER_CLUSTER, PRE_EARNINGS_DRIFT) arrives, extract -->
					<!-- to a `patterns: …` group with shared color-coding + a small label. -->
					{#if c.deep_drawdown_reversal}
						<ChipTip
							term="REVERSAL pattern"
							body="Deep-drawdown-reversal: ≥30% off 52-week high + fresh thematic catalyst (news URL present) + volume z-score ≥ +2σ. Archetype: oversold name on news with institutional accumulation flow. Heuristic — not validated alpha; use as decision-support signal."
						>
							{#snippet chip()}
								<span
									class="inline-flex items-center gap-1 px-1.5 py-0.5 bg-amber/20 text-amber text-[9px] uppercase tracking-widest border border-amber/40 cursor-help"
								>
									<Sparkle class="size-2.5" /> reversal
								</span>
							{/snippet}
						</ChipTip>
					{/if}
				</div>
				<div class="text-fg-dim text-xs mt-0.5 truncate">{c.company_name}</div>
				<div class="flex flex-wrap items-center gap-x-2 gap-y-0.5 mt-1 text-[10px] uppercase tracking-widest text-fg-muted">
					<span class="text-cyan lowercase">#{c.theme}</span>
					{#if c.also_in_themes && c.also_in_themes.length > 0}
						<span class="hidden sm:inline">·</span>
						<span>also in:</span>
						{#each c.also_in_themes.slice(0, 2) as t, i}
							<span class="text-cyan lowercase">{t}</span>{#if i < Math.min(c.also_in_themes.length, 2) - 1}<span>,</span>{/if}
						{/each}
						{#if c.also_in_themes.length > 2}
							<span
								class="text-fg-muted cursor-help"
								title={c.also_in_themes.slice(2).join(', ')}
							>+{c.also_in_themes.length - 2} more</span>
						{/if}
					{/if}
					{#if c.industry_name}
						<span class="hidden sm:inline">·</span>
						<span class="truncate">{c.industry_name}</span>
					{/if}
					{#if c.sector_name}
						<span class="hidden sm:inline">({c.sector_name})</span>
					{/if}
				</div>
			</div>

			<div class="grid grid-cols-4 gap-x-4 sm:gap-x-6 w-full lg:w-auto lg:ml-auto">
				<div class="text-[10px] uppercase tracking-widest min-w-0">
					<div class="text-fg-muted">mcap</div>
					<div class="text-fg text-sm sm:text-base font-bold normal-case truncate">{fmtUsdCompact(c.market_cap)}</div>
				</div>
				<div class="text-[10px] uppercase tracking-widest min-w-0">
					<div class="text-fg-muted">layer4</div>
					<div class="text-amber text-sm sm:text-base font-bold normal-case truncate">{c.layer4_weighted_score ?? '—'}</div>
				</div>
				<div class="text-[10px] uppercase tracking-widest min-w-0">
					<div class="text-fg-muted">conf</div>
					<div
						class="text-sm sm:text-base font-bold normal-case truncate"
						class:text-green={confTone === 'green'}
						class:text-amber={confTone === 'amber'}
						class:text-cyan={confTone === 'cyan'}
						class:text-fg-muted={confTone === 'muted'}
					>{conf5}/5</div>
				</div>
				<div class="text-[10px] uppercase tracking-widest min-w-0">
					<div class="text-fg-muted">catalyst</div>
					<div class="text-fg text-sm sm:text-base font-bold normal-case truncate">{fmtNum(c.catalyst_strength, 2)}</div>
					<div class="text-fg-muted text-[9px] mt-0.5 truncate">{c.catalyst_event_type ?? '—'}</div>
				</div>
			</div>
		</div>
	</header>

	<!-- TLDR + verification -->
	<div class="grid grid-cols-12 gap-4 lg:gap-5 px-4 sm:px-5 py-4">
		<div class="col-span-12 lg:col-span-8">
			{#if c.brief_tldr}
				<p class="text-fg text-sm leading-relaxed">{c.brief_tldr}</p>
			{:else}
				<p class="text-fg-dim text-sm leading-relaxed italic">{c.rationale}</p>
			{/if}

			<div class="mt-3 flex items-start gap-2 text-[11px]">
				<ExternalLink class="size-3 text-cyan flex-shrink-0 mt-0.5" />
				<div>
					<a
						href={c.source_event_url}
						target="_blank"
						rel="noreferrer"
						class="text-cyan hover:text-amber transition-colors underline underline-offset-2"
					>
						{c.source_event_title}
					</a>
					<span class="text-fg-muted ml-2">{fmtDate(c.source_event_published_at)}</span>
				</div>
			</div>
		</div>

		<div class="col-span-12 lg:col-span-4">
			<div class="text-[10px] uppercase tracking-widest text-fg-muted mb-2">verification.gates</div>
			<div class="flex flex-wrap gap-1.5">
				{#each c.gates_passed as g}
					<GatePill name={g} status="passed" />
				{/each}
				{#each c.gates_failed as g}
					<GatePill name={g} status="failed" />
				{/each}
				{#each c.gates_unknown as g}
					<GatePill name={g} status="unknown" />
				{/each}
			</div>
		</div>
	</div>

	<!-- Signal grid -->
	<div class="border-t border-grid bg-bg/30 px-4 sm:px-5 py-4">
		<div class="flex flex-wrap items-baseline gap-x-3 gap-y-1 mb-3 text-[10px] uppercase tracking-widest text-fg-muted">
			<span class="text-green">signals · vs sector peers</span>
			{#if c.peer_cohort_level === 'thin'}
				<ChipTip
					term="THIN cohort"
					body="SIC peer cohort too small to compute a meaningful percentile (4-digit + 3-digit fallback both below 8 members). Sector-percentile bars below are suppressed (shown as —)."
				>
					{#snippet chip()}
						<span class="inline-flex items-center px-1.5 py-0.5 bg-red/10 text-red text-[9px] uppercase tracking-widest border border-red/40 cursor-help"
						>thin cohort · bars suppressed</span>
					{/snippet}
				</ChipTip>
			{:else if c.peer_cohort_level === 'sic3'}
				<ChipTip
					term="SIC-3 cohort"
					body="4-digit SIC cohort was too small; widened to the 3-digit prefix. Percentile computed over a broader peer set — still trustworthy but looser."
				>
					{#snippet chip()}
						<span class="inline-flex items-center px-1.5 py-0.5 bg-cyan/10 text-cyan text-[9px] uppercase tracking-widest border border-cyan/30 cursor-help"
						>sic-3 cohort</span>
					{/snippet}
				</ChipTip>
			{:else if c.peer_cohort_level === 'ff48'}
				<ChipTip
					term="FF-48 cohort"
					body="4-digit + 3-digit SIC cohorts were both too small; widened to the Fama-French 48-industry bucket (academic SIC aggregation, free from Ken French's data library). Percentile reflects a broader but economically coherent peer set."
				>
					{#snippet chip()}
						<span class="inline-flex items-center px-1.5 py-0.5 bg-fg-muted/10 text-fg-muted text-[9px] uppercase tracking-widest border border-fg-muted/40 cursor-help"
						>ff-48 cohort</span>
					{/snippet}
				</ChipTip>
			{/if}
		</div>
		<div class="grid grid-cols-2 lg:grid-cols-4 gap-x-4 sm:gap-x-5 gap-y-4">
			<SignalBar
				label="insider 90d (sector %ile)"
				value={c.insider_score_sector_percentile}
				format={(v) => fmtPctile(v) + '%ile'}
				tooltip="Cohen-Malloy opportunistic insider buys ($USD) in the last 90 days, ranked within the ticker's sector. Higher percentile = stronger insider conviction vs sector peers. Paradigm #11 scorer (αt 2.71 IS, SLIPPAGE-FAIL standalone)."
			/>
			<SignalBar
				label="fcff yield (sector %ile)"
				value={c.fcff_yield_sector_percentile}
				format={(v) => fmtPctile(v) + '%ile'}
				tooltip="Free-cash-flow-to-firm yield = FCFF / EV, ranked within sector. Higher = cheaper on cash-generation basis. Paradigm #13 scorer (αt 1.18 IS, every-phase positive, multi-signal corroboration use only)."
			/>
			<SignalBar
				label="valuation composite"
				value={c.valuation_composite_sector_percentile}
				format={(v) => fmtPctile(v) + '%ile'}
				tooltip="Composite sector-percentile rank across PE, PS, EV/Revenue, EV/EBITDA, FCF margin. Higher = cheaper than sector peers on multiple multiples simultaneously."
			/>
			<SignalBar
				label="catalyst strength"
				value={c.catalyst_strength != null ? c.catalyst_strength * 100 : null}
				format={(v) => (v / 100).toFixed(2)}
				tooltip="Layer 4 catalyst-floor score (0-1) combining news novelty, thematic alignment with the source event, and freshness. Higher = stronger event-driven setup. Below 0.55 floor → candidate filtered out."
			/>

			<SignalBar
				label="rsi 14d"
				value={c.technical_rsi}
				format={(v) => v.toFixed(0)}
				tooltip="Relative Strength Index, 14-day. <30 oversold (potential reversal), >70 overbought (potential pullback), ~50 neutral. Combined with MA200 distance / 52w drawdown for the deep-drawdown-reversal pattern flag."
			/>
			<SignalBar
				label="off 52w high"
				value={c.technical_pct_off_52w_high != null ? Math.abs(c.technical_pct_off_52w_high) : null}
				min={0}
				max={95}
				format={(v) => '-' + v.toFixed(1) + '%'}
				inverted
				tooltip="% below the 52-week high. Deeper drawdown = potential reversal candidate OR continuation of secular decline. Pair with MA200 slope to discriminate."
			/>
			<SignalBar
				label="off 52w low"
				value={c.technical_pct_off_52w_low}
				min={0}
				max={200}
				format={(v) => '+' + v.toFixed(1) + '%'}
				tooltip="% above the 52-week low. Larger = stronger recovery from recent bottom. Combined with off-52w-high to gauge where the price sits within its annual range."
			/>
			<SignalBar
				label="vol z-score"
				value={c.technical_volume_zscore !== null ? Math.abs(c.technical_volume_zscore) : null}
				min={0}
				max={5}
				format={(v) => (c.technical_volume_zscore! >= 0 ? '+' : '-') + v.toFixed(1) + 'σ'}
				tooltip="20-day volume z-score. >+2σ = unusual buying interest (catalyst confirmation), <-2σ = drying volume (waning thesis). Sign matters; magnitude bar shows |z|."
			/>
		</div>
	</div>

	<!-- Technicals + fundamentals table -->
	<div class="grid grid-cols-12 gap-0 border-t border-grid text-[11px]">
		<div class="col-span-12 lg:col-span-6 px-4 sm:px-5 py-4 lg:border-r lg:border-grid">
			<div class="text-[10px] uppercase tracking-widest text-green mb-2">fundamentals</div>
			<dl class="grid grid-cols-2 gap-x-4 gap-y-1.5">
				<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('PE')}>pe</JargonTip></dt><dd class="text-fg text-right">{fmtNum(c.valuation_pe, 1)}</dd>
				<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('PS')}>ps</JargonTip></dt><dd class="text-fg text-right">{fmtNum(c.valuation_ps, 1)}</dd>
				<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('EV/REV')}>ev/rev</JargonTip></dt><dd class="text-fg text-right">{fmtNum(c.valuation_ev_rev, 1)}</dd>
				<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('EV/EBITDA')}>ev/ebitda</JargonTip></dt><dd class="text-fg text-right">{fmtNum(c.valuation_ev_ebitda, 1)}</dd>
				<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('FCF margin')}>fcf margin</JargonTip></dt><dd class="text-fg text-right">{c.valuation_fcf_margin !== null ? fmtPct(c.valuation_fcf_margin * 100) : '—'}</dd>
				<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('ROE')}>roe</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.roe_pct)}</dd>
				<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('FCFF')}>fcff yield</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.fcff_yield_pct, 2)}</dd>
				<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('magic formula')}>magic formula</JargonTip></dt><dd class="text-right">
					{#if c.magic_formula_rank != null}
						<span class="text-amber font-bold">#{Math.round(c.magic_formula_rank)}</span>
						<span class="text-fg-muted">/{c.magic_formula_cohort_n}</span>
					{:else}
						<span class="text-fg-muted">health-gate fail</span>
					{/if}
				</dd>
				<dt class="text-fg-muted uppercase tracking-widest">financials age</dt><dd class="text-fg text-right">{c.valuation_financials_age_days != null ? Math.round(c.valuation_financials_age_days) + 'd' : '—'}</dd>
				<dt class="text-fg-muted uppercase tracking-widest">next earnings</dt><dd class="text-fg text-right">{fmtDate(c.next_earnings_date)}</dd>
			</dl>
		</div>
		<div class="col-span-12 lg:col-span-6 px-4 sm:px-5 py-4 border-t lg:border-t-0 border-grid">
			<div class="text-[10px] uppercase tracking-widest text-green mb-2">technicals · trade setup</div>
			<dl class="grid grid-cols-2 gap-x-4 gap-y-1.5">
				<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA50')}>ma50 dist</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_ma50_distance_pct)}</dd>
				<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA200')}>ma200 dist</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_ma200_distance_pct)}</dd>
				<dt class="text-fg-muted uppercase tracking-widest">ma200 slope</dt><dd class="text-fg text-right">{c.technical_ma200_slope_pct_per_day !== null ? fmtPct(c.technical_ma200_slope_pct_per_day, 3) + '/d' : '—'}</dd>
				<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('ATR')}>atr</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_atr_pct)}</dd>
				<dt class="text-fg-muted uppercase tracking-widest">position size</dt><dd class="text-amber text-right font-bold">{c.brief_position_pct != null ? c.brief_position_pct.toFixed(1) + '%' : '—'}</dd>
				<dt class="text-fg-muted uppercase tracking-widest">time exit</dt><dd class="text-fg text-right">{c.brief_time_exit_weeks != null ? c.brief_time_exit_weeks + 'w' : '—'}</dd>
				<dt class="text-fg-muted uppercase tracking-widest">catalyst-fail exit</dt><dd class="text-fg text-right">{c.brief_time_exit_on_catalyst_failure_weeks != null ? c.brief_time_exit_on_catalyst_failure_weeks + 'w' : '—'}</dd>
				<dt class="text-fg-muted uppercase tracking-widest">disaster stop</dt><dd class="text-red text-right">{c.brief_disaster_stop_pct != null ? fmtPct(c.brief_disaster_stop_pct, 0) : '—'}</dd>
			</dl>
		</div>
	</div>

	<!-- Supply chain + bear + exit -->
	<div class="grid grid-cols-12 gap-0 border-t border-grid">
		<div class="col-span-12 lg:col-span-4 px-4 sm:px-5 py-4 lg:border-r lg:border-grid">
			<div class="text-[10px] uppercase tracking-widest text-cyan mb-2">supply.chain</div>
			<p class="text-fg-dim text-xs leading-relaxed">{c.brief_supply_chain_md ?? '—'}</p>
		</div>
		<div class="col-span-12 lg:col-span-4 px-4 sm:px-5 py-4 lg:border-r lg:border-grid border-t lg:border-t-0">
			<div class="text-[10px] uppercase tracking-widest text-red mb-2">bear.case</div>
			<p class="text-fg-dim text-xs leading-relaxed">{c.brief_bear_summary_md ?? '—'}</p>
		</div>
		<div class="col-span-12 lg:col-span-4 px-4 sm:px-5 py-4 border-t lg:border-t-0">
			<div class="text-[10px] uppercase tracking-widest text-amber mb-2">catalyst.failure.exit</div>
			<p class="text-fg-dim text-xs leading-relaxed">{c.brief_catalyst_failure_exit ?? '—'}</p>
		</div>
	</div>

	<!-- Expandable full markdown -->
	<details class="border-t border-grid group" bind:open={expanded}>
		<summary class="px-4 sm:px-5 py-3 text-[10px] uppercase tracking-widest text-fg-muted hover:text-amber cursor-pointer flex items-center gap-2 select-none">
			<ArrowUpRight class="size-3 transition-transform group-open:rotate-90" />
			{expanded ? 'collapse' : 'expand'} full brief markdown
			<span class="ml-auto text-fg-muted truncate">{c.brief_model_used ?? '—'}</span>
		</summary>
		<div class="px-4 sm:px-5 py-4 bg-bg-2 border-t border-grid">
			<pre class="prose-terminal whitespace-pre-wrap break-words text-[11px] leading-relaxed">{c.brief_full_md ?? ''}</pre>
		</div>
	</details>
</article>
