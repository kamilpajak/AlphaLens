<script lang="ts">
	import type { Candidate } from '$lib/types';
	import {
		fmtUsdCompact,
		fmtPct,
		fmtNum,
		fmtPctile,
		fmtDate,
		confidenceTone,
		confidenceLabel,
		buffettTone,
		oneilTone,
		insiderDisplay,
		magicFormulaDisplay,
		fcffYieldRawDisplay,
		tenkAvailable,
		selectionBadge,
		catalystLabel
	} from '$lib/format';
	import { ExternalLink, Sparkle } from 'lucide-svelte';
	import SignalBar from './SignalBar.svelte';
	import GatePill from './GatePill.svelte';
	import JargonTip from './JargonTip.svelte';
	import ChipTip from './ChipTip.svelte';
	import Formula from './Formula.svelte';
	import TooltipNote from './TooltipNote.svelte';
	import MetricGrid from './MetricGrid.svelte';
	import BulletList from './BulletList.svelte';
	import ExpertPanel from './ExpertPanel.svelte';
	import TradeSetup from './TradeSetup.svelte';
	import TemplateFacts from './TemplateFacts.svelte';
	import { GLOSSARY_BY_TERM } from '$lib/data/glossary';

	// Same tipProps pattern as /experiments — looks up term in shared glossary.
	function tipProps(term: string) {
		const g = GLOSSARY_BY_TERM.get(term);
		return {
			term: g?.term ?? term,
			full: g?.full ?? '',
			body: g?.body ?? '',
			formula: g?.formula,
			bands: g?.bands
		};
	}

	interface Props {
		candidate: Candidate;
		index: number;
	}
	let {
		candidate: c,
		index
	}: Props = $props();

	const confTone = $derived(confidenceTone(c.llm_confidence));
	// Honest insider 90d display: only show the sector-percentile bar when there
	// is actual net opportunistic buying; otherwise a muted "no buys" / "net
	// selling" / "—" state (a 0/negative dollar signal ranks ~100th percentile
	// only relative to net-selling peers — not a buy signal). See format.ts.
	const insider = $derived(insiderDisplay(c.insider_score_usd, c.insider_score_sector_percentile));
	// Unranked (health-gate fail) renders a muted "—" like every sibling
	// fundamentals row, not the verbose phrase. See format.ts.
	const magic = $derived(magicFormulaDisplay(c.magic_formula_rank, c.magic_formula_cohort_n));
	// Merged fcff-yield Valuation row: the %ile drives the bar; the raw % is an
	// annotation shown below it. Replaces the old duplicate raw-% row in FUNDAMENTALS.
	const fcffRaw = $derived(fcffYieldRawDisplay(c.fcff_yield_pct));
	// Humanised catalyst event type for the CATALYST & EVENT bar label (M&A / IPO /
	// underscores→spaces); null when absent so the " · <type>" suffix is dropped.
	const catLabel = $derived(catalystLabel(c.catalyst_event_type));
	// Tier colour for the catalyst-strength chip: strong (≥0.70, +2) green, moderate
	// (≥0.45, +1) amber, weak (no lift) muted — mirrors the lift the tooltip explains.
	const catalystTone = $derived(
		c.catalyst_strength == null
			? 'text-fg-muted'
			: c.catalyst_strength >= 0.7
				? 'text-green'
				: c.catalyst_strength >= 0.45
					? 'text-amber'
					: 'text-fg-muted'
	);
	// Rows for the headline-score badge tooltip: the derivation of selection_score
	// (= layer4 − atr_penalty). The ATR-penalty row is shown only when it bit. This
	// replaces the old SCORER BREAKDOWN section that used to sit in the expert drawer.
	const fmt2 = (v: number | null | undefined): string =>
		Number.isFinite(v) ? (v as number).toFixed(2) : '—';
	const scorerRows = $derived([
		{ key: 'layer-4', value: fmt2(c.layer4_weighted_score) },
		// Only when the penalty rounds to a visible ≥0.01 at 2dp, so a sub-0.005 tilt
		// never renders a misleading "−0.00" and layer4 − penalty = selection stays
		// internally consistent at the displayed precision.
		...(Number.isFinite(c.atr_penalty) && (c.atr_penalty as number) >= 0.005
			? [{ key: 'atr penalty', value: `−${(c.atr_penalty as number).toFixed(2)}` }]
			: []),
		{
			key: 'selection score',
			value: fmt2(Number.isFinite(c.selection_score) ? c.selection_score : c.layer4_weighted_score)
		}
	]);
	const rank = $derived(c.rank_in_day ?? index + 1);
	const cohort = $derived(c.cohort_size_in_day ?? '?');

	// The Buffett expert's assessment, read from the per-expert expert_assessments
	// blob (PR-5a: the card is now blob-driven so a later PR can drop the flat
	// buffett_* columns without touching the UI). The blob is SPARSE — a key may be
	// absent (not just null) when that part of the layer did not run — so every read
	// is optional-chained; the chip/drawer null-paths are unchanged ("—" when absent).
	const buf = $derived(c.expert_assessments?.buffett ?? null);

	// Buffett quality chip: a single 0-100 token in the meta bar, tone by score,
	// dimmed when fundamentals coverage is thin (< 0.5). Always rendered (shows
	// "—" when the score is null) so every card carries the metric consistently
	// with the other meta-bar figures; the hover explains an absent score.
	const buffScore = $derived(
		Number.isFinite(buf?.buffett_quality_score)
			? Math.round(buf?.buffett_quality_score as number)
			: null
	);
	const buffTone = $derived(buffettTone(buf?.buffett_quality_score));
	const buffLowCov = $derived(buf?.buffett_data_coverage != null && buf?.buffett_data_coverage < 0.5);
	const buffCovN = $derived(
		buf?.buffett_data_coverage != null ? Math.round(buf?.buffett_data_coverage * 6) : null
	);
	// Rows for the buffett quality tooltip's key→value grid (label left, value
	// right) — see MetricGrid.
	const buffRows = $derived([
		{ key: 'owner-earnings yield', value: fmtPct(buf?.buffett_owner_earnings_yield_pct) },
		{ key: 'ROIC 3y', value: fmtPct(buf?.buffett_roic_3y_avg) },
		{ key: 'margin of safety', value: fmtPct(buf?.buffett_margin_of_safety_pct) },
		{ key: 'coverage', value: `${buffCovN ?? '—'}/6` }
	]);

	// O'Neil momentum chip — the symmetric sibling of the Buffett chip (both expert
	// lenses are named on the meta-bar face so it reads coherently, not "Buffett +
	// an unnamed count"). Same shape: a 0-100 token, tone by score, always rendered
	// ("—" when absent). The disagreement BAND + its colour still live ONLY in the
	// opened <ExpertPanel> drawer (the manufactured-authority guard) — the face shows
	// the two raw scores, never the verdict.
	const oneil = $derived(c.expert_assessments?.oneil ?? null);
	const oneilScore = $derived(
		Number.isFinite(oneil?.oneil_score) ? Math.round(oneil?.oneil_score as number) : null
	);
	const oneilScoreTone = $derived(oneilTone(oneil?.oneil_score));
	// Rows for the o'neil momentum tooltip's key→value grid.
	const oneilRows = $derived([
		{ key: 'off 52w high', value: fmtPct(oneil?.oneil_pct_off_52w_high) },
		{ key: 'MA200 slope', value: `${fmtPct(oneil?.oneil_ma200_slope_pct_per_day, 2)}/d` },
		{ key: 'earnings YoY', value: fmtPct(oneil?.oneil_earnings_growth_yoy_pct) }
	]);
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

	<!-- Meta bar: sector / industry + mcap on the left (identity cluster); the
	     ordering signals on the right — a filled L4 badge leads, then the extended
	     flag (only when atr_penalty > 0) and confidence. Catalyst and the expert
	     lenses (buffett / o'neil) moved into their domain blocks below as part of
	     the domain regroup, so they no longer sit here. -->
	<div data-testid="card-meta" class="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 sm:px-5 py-2 border-b border-grid">
		<!-- Identity cluster: what the company is (sector / industry) and how big it is
		     (market cap). Mcap lives here, not with the right-side scores — it is a
		     descriptive company fact (kin to sector), and it is a SELECTION-gate input
		     (the mcap filter), so it earns an at-a-glance spot but reads as identity,
		     not a signal. The sector path truncates; mcap stays (shrink-0, nowrap). -->
		<div class="flex min-w-0 items-baseline gap-x-3 text-[10px] uppercase tracking-widest">
			<span class="min-w-0 truncate text-fg-muted">
				{#if c.sector_name && c.industry_name}
					{c.sector_name}<span class="text-grid-strong mx-1">/</span>{c.industry_name}
				{:else}
					{c.sector_name ?? c.industry_name ?? '—'}
				{/if}
			</span>
			<span class="inline-flex shrink-0 items-baseline gap-1.5 whitespace-nowrap">
				<span class="text-grid-strong">·</span>
				<span class="text-[9px] text-fg-muted">mcap</span>
				<span class="text-xs font-bold normal-case text-fg">{fmtUsdCompact(c.market_cap)}</span>
			</span>
		</div>
		<div class="ml-auto flex flex-wrap items-center gap-x-4 gap-y-2">
			<!-- Headline score — the OPERATIVE ranking signal, given a filled badge.
			     The brief is ranked by selection_score (= layer4 − atr_penalty), so the
			     badge next to "RANK" is that score, not the raw layer4 input. The hover
			     carries the derivation (layer4 → ATR penalty → selection + config + the
			     not-yet-validated caveat); the `extended` chip (below) flags a tilt. -->
			<ChipTip term="ranking score">
				{#snippet chip()}
					<span
						class="inline-flex cursor-help items-baseline gap-1.5 whitespace-nowrap rounded-sm border border-amber/35 bg-amber/10 px-2 py-0.5"
					>
						<span class="text-[8px] uppercase tracking-widest text-amber">score</span>
						<span class="font-display text-[15px] font-bold leading-none text-amber"
							>{selectionBadge(c.selection_score, c.layer4_weighted_score)}</span
						>
					</span>
				{/snippet}
				{#snippet bodyRich()}
					<MetricGrid rows={scorerRows} align="right" />
					{#if c.scorer_config_version}
						<p class="mt-2 text-[10px] text-fg-muted">
							<span class="whitespace-nowrap">{c.scorer_config_version}</span>
						</p>
					{/if}
					<p class="mt-1 text-[10px] italic text-fg-muted">suggestive — not yet validated</p>
				{/snippet}
			</ChipTip>
			<!-- Extended band: shown only when atr_penalty > 0 (high realized-vol /
			     extended at entry — deprioritized). Tone-neutral / muted — a soft flag,
			     not a hard gate. Precise penalty number + scorer_config_version live in
			     the expert.panel drawer (manufactured-authority discipline). -->
			{#if (c.atr_penalty ?? 0) > 0}
				<ChipTip
					term="extended"
					body="High realized volatility / extended at entry — deprioritized (suggestive, not yet validated)"
				>
					{#snippet chip()}
						<span
							class="inline-flex items-baseline gap-1.5 whitespace-nowrap rounded-sm border border-fg-muted/30 px-2 py-0.5 cursor-help"
						>
							<span class="text-[8px] uppercase tracking-widest text-fg-muted">extended</span>
						</span>
					{/snippet}
				</ChipTip>
			{/if}
			<span class="inline-flex items-baseline gap-1.5 whitespace-nowrap">
				<span class="text-[9px] uppercase tracking-widest text-fg-muted">conf</span>
				<span
					class="text-xs font-bold"
					class:text-green={confTone === 'green'}
					class:text-amber={confTone === 'amber'}
					class:text-cyan={confTone === 'cyan'}
					class:text-fg-muted={confTone === 'muted'}>{confidenceLabel(c.llm_confidence)}</span
				>
			</span>
		</div>
	</div>

	<!-- Main split: left = thesis + signals/fundamentals, right = trade setup + narrative. -->
	<div class="grid grid-cols-1 lg:grid-cols-12">
		<!-- LEFT column -->
		<div class="lg:col-span-7 lg:border-r border-grid">
			<!-- CATALYST & EVENT — the reason this name surfaced: catalyst strength,
			     the thesis it drives, the source event, and the deterministic typed
			     facts. (Retires the standalone live.equity.thesis heading.) -->
			<div class="px-4 sm:px-5 py-4 border-b border-grid">
				<!-- Header row: section title left, the catalyst strength top-right, in
				     place of the old full-width bar. Unboxed lens-score style — a small
				     muted event-type label + the tier-coloured strength; the corrected
				     lift explanation is in its hover. -->
				<div class="flex items-baseline justify-between gap-2 mb-3">
					<div class="text-[10px] uppercase tracking-widest text-cyan">catalyst.event</div>
					{#if c.catalyst_strength != null}
						<ChipTip term="catalyst strength">
							{#snippet chip()}
								<span class="inline-flex items-baseline gap-1.5 whitespace-nowrap cursor-help">
									{#if catLabel}<span class="text-[10px] uppercase tracking-widest text-fg-muted"
											>{catLabel}</span
										>{/if}<span class="font-display text-base font-bold leading-none {catalystTone}"
										>{fmtNum(c.catalyst_strength, 2)}</span
									>
								</span>
							{/snippet}
							{#snippet bodyRich()}
								<span class="block">Catalyst strength (0–1) of the source event, combining:</span>
								<BulletList
									items={[
										'event-type tier (M&A 1.0 … other 0.3)',
										'extraction confidence',
										'second-order implications'
									]}
								/>
								<TooltipNote
									>a cohort-score <span class="font-bold">lift</span>, not a filter:
									<span class="whitespace-nowrap font-bold">≥0.45 → +1</span>,
									<span class="whitespace-nowrap font-bold">≥0.70 → +2</span>; a weak catalyst adds no
									lift but does <span class="font-bold">not</span> drop the name</TooltipNote
								>
							{/snippet}
						</ChipTip>
					{/if}
				</div>
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
				{#if c.brief_template_id}
					<div class="mt-4 border-t border-grid pt-4">
						<TemplateFacts templateId={c.brief_template_id} facts={c.brief_template_facts} />
					</div>
				{/if}
			</div>

			<!-- Valuation & Quality | Momentum & Technicals — two analytical domains
			     side by side, each anchored by its expert lens score. -->
			<div class="grid grid-cols-1 md:grid-cols-2">
				<!-- VALUATION & QUALITY (Buffett anchors) -->
				<div data-testid="block-valuation" class="px-4 sm:px-5 py-4 md:border-r border-grid">
					<div class="flex flex-wrap items-baseline gap-x-2 gap-y-1 mb-3">
						<div class="text-[10px] uppercase tracking-widest text-cyan">valuation.quality</div>
						{#if c.peer_cohort_level === 'thin'}
							<ChipTip
								term="THIN cohort"
								body="SIC peer cohort too small to compute a meaningful percentile (4-digit + 3-digit fallback both below 8 members). Sector-percentile bars are suppressed (shown as —)."
							>
								{#snippet chip()}
									<span class="inline-flex items-center px-1.5 py-0.5 bg-red/10 text-red text-[9px] uppercase tracking-widest border border-red/40 cursor-help">thin cohort</span>
								{/snippet}
							</ChipTip>
						{:else if c.peer_cohort_level === 'sic3'}
							<ChipTip
								term="SIC-3 cohort"
								body="4-digit SIC cohort was too small; widened to the 3-digit prefix. Percentile computed over a broader peer set — still trustworthy but looser."
							>
								{#snippet chip()}
									<span class="inline-flex items-center px-1.5 py-0.5 bg-cyan/10 text-cyan text-[9px] uppercase tracking-widest border border-cyan/30 cursor-help">sic-3 cohort</span>
								{/snippet}
							</ChipTip>
						{:else if c.peer_cohort_level === 'ff48'}
							<ChipTip
								term="FF-48 cohort"
								body="4-digit + 3-digit SIC cohorts were both too small; widened to the Fama-French 48-industry bucket. Percentile reflects a broader but economically coherent peer set."
							>
								{#snippet chip()}
									<span class="inline-flex items-center px-1.5 py-0.5 bg-fg-muted/10 text-fg-muted text-[9px] uppercase tracking-widest border border-fg-muted/40 cursor-help">ff-48 cohort</span>
								{/snippet}
							</ChipTip>
						{/if}
					</div>
					<!-- Expert anchor: Buffett value/quality lens. The full-width row lives in
					     the card markup; only the score token is wrapped in ChipTip so the hover
					     trigger stays phrasing content (no div-in-span) and justify-between can
					     push the score to the column's right edge. -->
					<div
						class="mb-4 flex items-baseline justify-between gap-2"
						class:opacity-60={buffLowCov}
					>
						<span class="text-[10px] uppercase tracking-widest text-fg-muted">buffett <span class="normal-case text-fg-dim">· value / quality</span></span>
						<ChipTip term="buffett quality">
							{#snippet chip()}
								<span
									class="font-display text-base font-bold leading-none cursor-help"
									class:text-green={buffTone === 'green'}
									class:text-amber={buffTone === 'amber'}
									class:text-fg-muted={buffTone === 'muted'}
									>{buffScore ?? '—'}<span class="text-[10px] font-normal text-fg-muted">/100</span></span
								>
							{/snippet}
							{#snippet bodyRich()}
								<MetricGrid rows={buffRows} align="right" />
								<p class="mt-2 text-center text-[15px] text-fg-dim"><Formula name="margin_of_safety" /></p>
								{#if buffScore === null}
									<p class="mt-1 text-fg-muted">not enough fundamentals to score</p>
								{:else if buffLowCov}
									<p class="mt-1 text-fg-muted">thin data, score down-weighted</p>
								{/if}
							{/snippet}
						</ChipTip>
					</div>
					<div class="flex flex-col gap-y-4">
						<SignalBar
							label="fcff yield (sector %ile)"
							value={c.fcff_yield_sector_percentile}
							format={(v) => fmtPctile(v) + '%ile'}
							subValue={fcffRaw}
							tooltip="Free-cash-flow-to-firm yield = FCFF / EV. The dimmed value is the raw yield; the bold %ile is its rank within sector. Higher = cheaper on a cash-generation basis. Paradigm #13 scorer (αt 1.18 IS, multi-signal corroboration only)."
						/>
						<SignalBar
							label="valuation composite"
							value={c.valuation_composite_sector_percentile}
							format={(v) => fmtPctile(v) + '%ile'}
						>
							{#snippet tooltipRich()}
								<span class="block">Composite sector-%ile rank across 5 multiples:</span>
								<BulletList items={['PE', 'PS', 'EV/Revenue', 'EV/EBITDA', 'FCF margin']} />
								<TooltipNote>higher = cheaper than sector peers on several multiples at once</TooltipNote>
							{/snippet}
						</SignalBar>
					</div>
					<div class="mt-4 mb-3 border-t border-grid" aria-hidden="true"></div>
					<dl class="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px]">
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('PE')}>pe</JargonTip></dt><dd class="text-fg text-right">{fmtNum(c.valuation_pe, 1)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('PS')}>ps</JargonTip></dt><dd class="text-fg text-right">{fmtNum(c.valuation_ps, 1)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('EV/REV')}>ev/rev</JargonTip></dt><dd class="text-fg text-right">{fmtNum(c.valuation_ev_rev, 1)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('EV/EBITDA')}>ev/ebitda</JargonTip></dt><dd class="text-fg text-right">{fmtNum(c.valuation_ev_ebitda, 1)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('FCF margin')}>fcf margin</JargonTip></dt><dd class="text-fg text-right">{c.valuation_fcf_margin !== null ? fmtPct(c.valuation_fcf_margin * 100) : '—'}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('ROE')}>roe</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.roe_pct)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('magic formula')}>magic formula</JargonTip></dt><dd class="text-fg text-right">
							{#if magic.mode === 'rank'}
								<span class="text-amber font-bold">#{magic.rank}</span>{#if magic.cohortN !== null}/{magic.cohortN}{/if}
							{:else}
								{magic.label}
							{/if}
						</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('financials age')}>financials age</JargonTip></dt><dd class="text-fg text-right">{c.valuation_financials_age_days != null ? Math.round(c.valuation_financials_age_days) + 'd' : '—'}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('next earnings')}>next earnings</JargonTip></dt><dd class="text-fg text-right whitespace-nowrap">{fmtDate(c.next_earnings_date)}</dd>
					</dl>
				</div>

				<!-- MOMENTUM & TECHNICALS (O'Neil anchors) -->
				<div data-testid="block-momentum" class="px-4 sm:px-5 py-4 border-t md:border-t-0 border-grid">
					<div class="text-[10px] uppercase tracking-widest text-cyan mb-3">momentum.technicals</div>
					<!-- Expert anchor: O'Neil momentum lens. Same pattern — full-width row in
					     the card; only the score token is the ChipTip trigger. -->
					<div class="mb-4 flex items-baseline justify-between gap-2">
						<span class="text-[10px] uppercase tracking-widest text-fg-muted">o'neil <span class="normal-case text-fg-dim">· momentum</span></span>
						<ChipTip term="o'neil momentum">
							{#snippet chip()}
								<span
									class="font-display text-base font-bold leading-none cursor-help"
									class:text-green={oneilScoreTone === 'green'}
									class:text-amber={oneilScoreTone === 'amber'}
									class:text-fg-muted={oneilScoreTone === 'muted'}
									>{oneilScore ?? '—'}<span class="text-[10px] font-normal text-fg-muted">/100</span></span
								>
							{/snippet}
							{#snippet bodyRich()}
								<MetricGrid rows={oneilRows} align="right" />
								{#if oneilScore === null}
									<p class="mt-1 text-fg-muted">momentum terms incomplete to score</p>
								{/if}
							{/snippet}
						</ChipTip>
					</div>
					<div class="flex flex-col gap-y-4">
						<SignalBar
							label="rsi 14d"
							value={c.technical_rsi}
							format={(v) => v.toFixed(0)}
						>
							{#snippet tooltipRich()}
								<span class="block">Relative Strength Index, 14-day:</span>
								<MetricGrid
									rows={[
										{ key: '<30', value: 'oversold (potential reversal)' },
										{ key: '~50', value: 'neutral' },
										{ key: '>70', value: 'overbought (potential pullback)' }
									]}
									class="mt-1"
								/>
							{/snippet}
						</SignalBar>
						<SignalBar
							label="off 52w high"
							value={c.technical_pct_off_52w_high != null ? Math.abs(c.technical_pct_off_52w_high) : null}
							min={0}
							max={95}
							format={(v) => '-' + v.toFixed(1) + '%'}
							inverted
						/>
						<SignalBar
							label="off 52w low"
							value={c.technical_pct_off_52w_low}
							min={0}
							max={200}
							format={(v) => '+' + v.toFixed(1) + '%'}
							tooltip="% above the 52-week low. Larger = stronger recovery from recent bottom."
						/>
						<SignalBar
							label="rel strength (sector %ile)"
							value={oneil?.oneil_rs_approx_pct ?? null}
							format={(v) => fmtPctile(v) + '%ile'}
							tooltip="O'Neil relative-strength rank — the stock's trailing return ranked against peers. Higher = stronger leadership. From the O'Neil momentum lens."
						/>
						<SignalBar
							label="vol z-score"
							value={c.technical_volume_zscore !== null ? Math.abs(c.technical_volume_zscore) : null}
							min={0}
							max={5}
							format={(v) => (c.technical_volume_zscore! >= 0 ? '+' : '-') + v.toFixed(1) + 'σ'}
						>
							{#snippet tooltipRich()}
								<span class="block">20-day volume z-score:</span>
								<MetricGrid
									rows={[
										{ key: '>+2σ', value: 'unusual buying interest (catalyst confirmation)' },
										{ key: '<−2σ', value: 'drying volume (waning thesis)' }
									]}
									class="mt-1"
								/>
							{/snippet}
						</SignalBar>
					</div>
					<div class="mt-4 mb-3 border-t border-grid" aria-hidden="true"></div>
					<dl class="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px]">
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA50')}>ma50 dist</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_ma50_distance_pct)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA200')}>ma200 dist</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_ma200_distance_pct)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA200 slope')}>ma200 slope</JargonTip></dt><dd class="text-fg text-right whitespace-nowrap">{c.technical_ma200_slope_pct_per_day !== null ? fmtPct(c.technical_ma200_slope_pct_per_day, 3) + '/d' : '—'}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('ATR')}>atr</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_atr_pct)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest">earnings yoy</dt><dd class="text-fg text-right whitespace-nowrap">{fmtPct(oneil?.oneil_earnings_growth_yoy_pct)}</dd>
					</dl>
				</div>
			</div>

			<!-- INSIDER (paradigm #11) — a compact one-line row that renders ONLY when
			     there is net opportunistic buying (~1 in 400 cards in production). The
			     no-buys case is carried by the header ✗ INSIDER gate, so no always-empty
			     row. NOTE: this score is a 180-day buy-only signal (the header gate is a
			     separate 90-day binary check — different window, can disagree). -->
			{#if insider.mode === 'bar'}
				<div
					class="flex items-baseline justify-between gap-3 border-t border-grid px-4 py-2.5 text-[11px] sm:px-5"
				>
					<span class="uppercase tracking-widest text-fg-muted">insider buys · 180d</span>
					<ChipTip
						term="opportunistic insider buys (180d)"
						body="Buy-only opportunistic insider purchases ({fmtUsdCompact(insider.netUsd)}) over the last 180 days, ranked within sector. Cohen-Malloy opportunistic classification; paradigm #11 scorer. (Distinct from the 90-day INSIDER header gate.)"
					>
						{#snippet chip()}
							<span class="cursor-help font-bold text-amber whitespace-nowrap"
								>{insider.percentile != null
									? fmtPctile(insider.percentile) + '%ile · '
									: ''}{fmtUsdCompact(insider.netUsd)}</span
							>
						{/snippet}
					</ChipTip>
				</div>
			{/if}

			<!-- Expert-panel deep-read (PR-8b): the generalized drawer — disagreement
			     headline + dot-lane (only when >=2 lenses scored) + one section per
			     expert (Buffett qual pillars + rationale; O'Neil numeric readouts +
			     audit flags). Hidden by default; renders nothing when no lens has
			     content for this name. The transition shim lives inside (reads the
			     persisted panel.expert_spread, never recomputes). -->
			<ExpertPanel
				assessments={c.expert_assessments}
				tenkAvailable={tenkAvailable(c.gates_passed, c.gates_failed)}
			/>
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
		<div class="col-span-12 lg:col-span-4 px-4 sm:px-5 py-4 border-grid border-t lg:border-t-0">
			<div class="text-[10px] uppercase tracking-widest text-amber mb-2">catalyst.failure.exit</div>
			<p class="text-fg-dim text-xs leading-relaxed">{c.brief_catalyst_failure_exit ?? '—'}</p>
		</div>
	</div>
</article>
