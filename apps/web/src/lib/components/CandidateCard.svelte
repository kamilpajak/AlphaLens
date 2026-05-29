<script lang="ts">
	import type { Candidate, Decision, FeedbackTaxonomy } from '$lib/types';
	import { fmtUsdCompact, fmtPct, fmtNum, fmtPctile, fmtDate, confidenceTone, confidenceLabel } from '$lib/format';
	import { ExternalLink, Sparkle } from 'lucide-svelte';
	import SignalBar from './SignalBar.svelte';
	import GatePill from './GatePill.svelte';
	import JargonTip from './JargonTip.svelte';
	import ChipTip from './ChipTip.svelte';
	import TradeSetup from './TradeSetup.svelte';
	import FeedbackControls from './FeedbackControls.svelte';
	import { GLOSSARY_BY_TERM } from '$lib/data/glossary';

	// Same tipProps pattern as /experiments — looks up term in shared glossary.
	function tipProps(term: string) {
		const g = GLOSSARY_BY_TERM.get(term);
		return { term: g?.term ?? term, full: g?.full ?? '', body: g?.body ?? '' };
	}

	interface Props {
		candidate: Candidate;
		index: number;
		// Feedback ledger plumbing. Optional so /experiments + any non-brief
		// host can drop the card without wiring the loader. When `taxonomy`
		// is null the FeedbackControls row is hidden (degrade gracefully if
		// the /v1/feedback/taxonomy endpoint is unreachable).
		// `briefDate` is the route param, NOT candidate.date — the daily-brief
		// API does not stamp candidate.date in every fixture, so we pass it
		// from the page loader where it is authoritative.
		briefDate?: string | null;
		taxonomy?: FeedbackTaxonomy | null;
		// `decisionsLoaded` distinguishes "no decisions yet" (true + empty)
		// from "couldn't load decisions" (false) — when false we hide the
		// controls so a POST cannot silently overwrite a prior decision
		// that exists server-side but failed to load this session.
		decisionsLoaded?: boolean;
		existingDecision?: Decision | null;
	}
	let {
		candidate: c,
		index,
		briefDate = null,
		taxonomy = null,
		decisionsLoaded = false,
		existingDecision = null
	}: Props = $props();

	const confTone = $derived(confidenceTone(c.gemini_confidence));
	const rank = $derived(c.rank_in_day ?? index + 1);
	const cohort = $derived(c.cohort_size_in_day ?? '?');
</script>

<article
	id={c.ticker}
	class="border border-grid bg-bg-1 fade-up isolate"
	style="animation-delay: {index * 0.04}s"
>
	<!-- Header: rank + ticker + company + theme on the left, verification gates
	     pushed to the right. Wraps to a stacked layout on narrow viewports. -->
	<header class="px-4 sm:px-5 py-3 border-b border-grid bg-gradient-to-r from-bg-2 to-bg-1">
		<div class="flex flex-wrap items-center gap-x-3 gap-y-2">
			<span
				class="px-2 py-1 border border-grid-strong text-[9px] uppercase tracking-widest text-fg-muted whitespace-nowrap"
			>
				rank {String(rank).padStart(2, '0')} of {cohort}
			</span>
			<div class="flex items-baseline gap-2 min-w-0">
				<h3 class="font-display font-bold text-2xl sm:text-3xl text-amber leading-none">{c.ticker}</h3>
				<span class="text-fg-dim text-xs sm:text-sm truncate">{c.company_name}</span>
			</div>
			<span
				class="px-2 py-0.5 bg-violet/15 border border-violet/40 text-violet text-[10px] lowercase tracking-widest"
				>#{c.theme}</span
			>
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

			<div class="flex flex-wrap items-center gap-1.5 ml-auto">
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
	</header>

	<!-- Meta bar: sector / industry on the left, the 4 headline metrics on the right. -->
	<div
		class="flex flex-wrap items-center gap-x-5 gap-y-1 px-4 sm:px-5 py-2 border-b border-grid text-[10px] uppercase tracking-widest"
	>
		<span class="text-fg-muted min-w-0 truncate">
			{#if c.sector_name && c.industry_name}
				{c.sector_name}<span class="text-grid-strong mx-1">/</span>{c.industry_name}
			{:else}
				{c.sector_name ?? c.industry_name ?? '—'}
			{/if}
		</span>
		<div class="flex flex-wrap items-center gap-x-5 gap-y-1 ml-auto">
			<span class="text-fg-muted whitespace-nowrap">
				layer-4 score <span class="text-amber font-bold normal-case">{c.layer4_weighted_score ?? '—'}</span>
			</span>
			<span class="text-fg-muted whitespace-nowrap">
				confidence
				<span
					class="font-bold normal-case"
					class:text-green={confTone === 'green'}
					class:text-amber={confTone === 'amber'}
					class:text-cyan={confTone === 'cyan'}
					class:text-fg-muted={confTone === 'muted'}>{confidenceLabel(c.gemini_confidence)}</span
				>
			</span>
			<span class="text-fg-muted whitespace-nowrap">
				mkt cap <span class="text-fg font-bold normal-case">{fmtUsdCompact(c.market_cap)}</span>
			</span>
			<span class="text-fg-muted whitespace-nowrap">
				catalyst
				<span class="text-violet font-bold lowercase">{c.catalyst_event_type ?? '—'}</span>
				<span class="text-fg-muted">/ {fmtNum(c.catalyst_strength, 2)}</span>
			</span>
		</div>
	</div>

	<!-- Main split: left = thesis + signals/fundamentals, right = trade setup + narrative. -->
	<div class="grid grid-cols-1 lg:grid-cols-12">
		<!-- LEFT column -->
		<div class="lg:col-span-7 lg:border-r border-grid">
			<!-- Live equity thesis -->
			<div class="px-4 sm:px-5 py-4 border-b border-grid">
				<div class="text-[10px] uppercase tracking-widest text-cyan mb-3">live.equity.thesis</div>
				<blockquote class="border-l-2 border-violet pl-4">
					{#if c.brief_tldr}
						<p class="text-fg text-sm leading-relaxed">{c.brief_tldr}</p>
					{:else}
						<p class="text-fg-dim text-sm leading-relaxed italic">{c.rationale}</p>
					{/if}
				</blockquote>

				<div class="mt-3 flex items-start gap-3 text-[11px]">
					<span class="text-fg-muted whitespace-nowrap">{fmtDate(c.source_event_published_at)}</span>
					<span class="w-px self-stretch bg-grid-strong" aria-hidden="true"></span>
					<a
						href={c.source_event_url}
						target="_blank"
						rel="noreferrer"
						aria-label={`${c.source_event_title ?? 'source event'} (opens in a new tab)`}
						class="inline-flex items-start gap-1 text-cyan hover:text-amber transition-colors underline underline-offset-2 min-w-0"
					>
						<span>{c.source_event_title}</span>
						<ExternalLink class="size-3 flex-shrink-0 mt-0.5" />
					</a>
				</div>
			</div>

			<!-- Signals | Fundamentals + Technicals -->
			<div class="grid grid-cols-1 md:grid-cols-2">
				<!-- System signals -->
				<div class="px-4 sm:px-5 py-4 md:border-r border-grid">
					<div class="flex flex-wrap items-baseline gap-x-2 gap-y-1 mb-3">
						<div class="text-[10px] uppercase tracking-widest text-cyan">system.signals</div>
						{#if c.peer_cohort_level === 'thin'}
							<ChipTip
								term="THIN cohort"
								body="SIC peer cohort too small to compute a meaningful percentile (4-digit + 3-digit fallback both below 8 members). Sector-percentile bars below are suppressed (shown as —)."
							>
								{#snippet chip()}
									<span
										class="inline-flex items-center px-1.5 py-0.5 bg-red/10 text-red text-[9px] uppercase tracking-widest border border-red/40 cursor-help"
										>thin cohort</span
									>
								{/snippet}
							</ChipTip>
						{:else if c.peer_cohort_level === 'sic3'}
							<ChipTip
								term="SIC-3 cohort"
								body="4-digit SIC cohort was too small; widened to the 3-digit prefix. Percentile computed over a broader peer set — still trustworthy but looser."
							>
								{#snippet chip()}
									<span
										class="inline-flex items-center px-1.5 py-0.5 bg-cyan/10 text-cyan text-[9px] uppercase tracking-widest border border-cyan/30 cursor-help"
										>sic-3 cohort</span
									>
								{/snippet}
							</ChipTip>
						{:else if c.peer_cohort_level === 'ff48'}
							<ChipTip
								term="FF-48 cohort"
								body="4-digit + 3-digit SIC cohorts were both too small; widened to the Fama-French 48-industry bucket (academic SIC aggregation, free from Ken French's data library). Percentile reflects a broader but economically coherent peer set."
							>
								{#snippet chip()}
									<span
										class="inline-flex items-center px-1.5 py-0.5 bg-fg-muted/10 text-fg-muted text-[9px] uppercase tracking-widest border border-fg-muted/40 cursor-help"
										>ff-48 cohort</span
									>
								{/snippet}
							</ChipTip>
						{/if}
					</div>
					<div class="flex flex-col gap-y-4">
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

				<!-- Fundamentals + technicals context -->
				<div class="px-4 sm:px-5 py-4 border-t md:border-t-0 border-grid text-[11px]">
					<div class="text-[10px] uppercase tracking-widest text-cyan mb-2">fundamentals</div>
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
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('financials age')}>financials age</JargonTip></dt><dd class="text-fg text-right">{c.valuation_financials_age_days != null ? Math.round(c.valuation_financials_age_days) + 'd' : '—'}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('next earnings')}>next earnings</JargonTip></dt><dd class="text-fg text-right">{fmtDate(c.next_earnings_date)}</dd>
					</dl>

					<div class="text-[10px] uppercase tracking-widest text-cyan mt-4 mb-2">technicals.context</div>
					<dl class="grid grid-cols-2 gap-x-4 gap-y-1.5">
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA50')}>ma50 dist</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_ma50_distance_pct)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA200')}>ma200 dist</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_ma200_distance_pct)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA200 slope')}>ma200 slope</JargonTip></dt><dd class="text-fg text-right">{c.technical_ma200_slope_pct_per_day !== null ? fmtPct(c.technical_ma200_slope_pct_per_day, 3) + '/d' : '—'}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('ATR')}>atr</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_atr_pct)}</dd>
					</dl>
				</div>
			</div>
		</div>

		<!-- RIGHT column -->
		<div class="lg:col-span-5 border-t lg:border-t-0 border-grid">
			<!-- Trade execution setup -->
			<div class="px-4 sm:px-5 py-4">
				<TradeSetup setup={c.brief_trade_setup} />
			</div>
		</div>
	</div>

	<!-- Analyst narrative: full-width horizontal row at the bottom (3 columns). -->
	<div class="grid grid-cols-12 gap-0 border-t border-grid">
		<div class="col-span-12 lg:col-span-4 px-4 sm:px-5 py-4 lg:border-r border-grid">
			<div class="text-[10px] uppercase tracking-widest text-cyan mb-2">supply.chain</div>
			<p class="text-fg-dim text-xs leading-relaxed">{c.brief_supply_chain_md ?? '—'}</p>
		</div>
		<div class="col-span-12 lg:col-span-4 px-4 sm:px-5 py-4 lg:border-r border-grid border-t lg:border-t-0">
			<div class="text-[10px] uppercase tracking-widest text-red mb-2">bear.case</div>
			<p class="text-fg-dim text-xs leading-relaxed">{c.brief_bear_summary_md ?? '—'}</p>
		</div>
		<div class="col-span-12 lg:col-span-4 px-4 sm:px-5 py-4 border-t lg:border-t-0">
			<div class="text-[10px] uppercase tracking-widest text-amber mb-2">catalyst.failure.exit</div>
			<p class="text-fg-dim text-xs leading-relaxed">{c.brief_catalyst_failure_exit ?? '—'}</p>
		</div>
	</div>

	{#if taxonomy && briefDate && decisionsLoaded}
		<FeedbackControls
			{briefDate}
			ticker={c.ticker}
			theme={c.theme}
			surfacedAt={c.source_event_published_at ?? briefDate}
			{taxonomy}
			existing={existingDecision}
		/>
	{/if}
</article>
