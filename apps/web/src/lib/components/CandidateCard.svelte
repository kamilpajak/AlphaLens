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
		fcffYieldDisplay
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
	// annotation shown below it (see fcffYieldDisplay). Replaces the old duplicate
	// raw-% row in FUNDAMENTALS.
	const fcffRaw = $derived(fcffYieldDisplay(c.fcff_yield_sector_percentile, c.fcff_yield_pct));
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
	// Keys avoid the exact substrings used by visible signal bar labels
	// ("off 52w high", "ma200 slope") to prevent DOM dedup collisions —
	// Playwright toHaveCount counts hidden tooltip text alongside visible.
	const oneilRows = $derived([
		{ key: 'high drawdown', value: fmtPct(oneil?.oneil_pct_off_52w_high) },
		{ key: '200d trend slope', value: `${fmtPct(oneil?.oneil_ma200_slope_pct_per_day, 2)}/d` },
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

	<!-- Meta bar: sector / industry on the left; the headline metrics on the right,
	     grouped so the eye lands on the layer-4 score first — a filled L4 badge
	     (the ordering signal) leads, then confidence / mcap / catalyst, then the
	     display-only expert chips (buffett + panel) set apart behind a divider.
	     Pattern: small dim uppercase key + bold value, replacing the old uniform
	     uppercase ticker-tape. -->
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
			<!-- Layer-4 score — the headline ordering signal, given a filled badge. -->
			<span
				class="inline-flex items-baseline gap-1.5 whitespace-nowrap rounded-sm border border-amber/35 bg-amber/10 px-2 py-0.5"
			>
				<span class="text-[8px] uppercase tracking-widest text-amber">layer-4</span>
				<span class="font-display text-[15px] font-bold leading-none text-amber"
					>{c.layer4_weighted_score ?? '—'}</span
				>
			</span>
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
				<div class="text-[10px] uppercase tracking-widest text-cyan mb-3">catalyst &amp; event</div>
				<div class="mb-4">
					<SignalBar
						label={`catalyst${c.catalyst_event_type ? ' · ' + c.catalyst_event_type : ''}`}
						value={c.catalyst_strength != null ? c.catalyst_strength * 100 : null}
						format={(v) => (v / 100).toFixed(2)}
					>
						{#snippet tooltipRich()}
							<span class="block">Layer-4 catalyst-floor score (0–1), combining:</span>
							<BulletList
								items={['news novelty', 'thematic alignment with the source event', 'freshness']}
							/>
							<TooltipNote
								>higher = stronger event-driven setup; <span class="font-bold">below</span> the
								<span class="whitespace-nowrap font-bold">0.55 floor</span> → candidate
								<span class="font-bold">filtered out</span></TooltipNote
							>
						{/snippet}
					</SignalBar>
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
						<div class="text-[10px] uppercase tracking-widest text-cyan">valuation &amp; quality</div>
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
					<!-- Expert anchor: Buffett value/quality lens. -->
					<ChipTip term="buffett quality">
						{#snippet chip()}
							<div
								class="mb-4 flex items-baseline justify-between gap-2 cursor-help"
								class:opacity-60={buffLowCov}
							>
								<span class="text-[10px] uppercase tracking-widest text-fg-muted">buffett <span class="normal-case text-fg-dim">· value / quality</span></span>
								<span
									class="font-display text-base font-bold leading-none"
									class:text-green={buffTone === 'green'}
									class:text-amber={buffTone === 'amber'}
									class:text-fg-muted={buffTone === 'muted'}
									>{buffScore ?? '—'}<span class="text-[10px] font-normal text-fg-muted">/100</span></span
								>
							</div>
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
					<div class="flex flex-col gap-y-4">
						<SignalBar
							label="fcff yield (sector %ile)"
							value={c.fcff_yield_sector_percentile}
							format={(v) => fmtPctile(v) + '%ile'}
							tooltip="Free-cash-flow-to-firm yield = FCFF / EV, ranked within sector. Higher = cheaper on a cash-generation basis. Paradigm #13 scorer (αt 1.18 IS, multi-signal corroboration only)."
						/>
						{#if fcffRaw.rawText}
							<div class="-mt-3 text-[10px] uppercase tracking-widest text-fg-muted">
								raw <span class="text-fg-dim font-bold normal-case whitespace-nowrap">{fcffRaw.rawText}</span>
							</div>
						{/if}
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
					<div class="text-[10px] uppercase tracking-widest text-fg-muted mt-4 mb-2">multiples</div>
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
					<div class="text-[10px] uppercase tracking-widest text-cyan mb-3">momentum &amp; technicals</div>
					<!-- Expert anchor: O'Neil momentum lens. -->
					<ChipTip term="o'neil momentum">
						{#snippet chip()}
							<div class="mb-4 flex items-baseline justify-between gap-2 cursor-help">
								<span class="text-[10px] uppercase tracking-widest text-fg-muted">o'neil <span class="normal-case text-fg-dim">· momentum</span></span>
								<span
									class="font-display text-base font-bold leading-none"
									class:text-green={oneilScoreTone === 'green'}
									class:text-amber={oneilScoreTone === 'amber'}
									class:text-fg-muted={oneilScoreTone === 'muted'}
									>{oneilScore ?? '—'}<span class="text-[10px] font-normal text-fg-muted">/100</span></span
								>
							</div>
						{/snippet}
						{#snippet bodyRich()}
							<MetricGrid rows={oneilRows} align="right" />
							{#if oneilScore === null}
								<p class="mt-1 text-fg-muted">momentum terms incomplete to score</p>
							{/if}
						{/snippet}
					</ChipTip>
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
					<div class="text-[10px] uppercase tracking-widest text-fg-muted mt-4 mb-2">trend</div>
					<dl class="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px]">
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA50')}>ma50 dist</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_ma50_distance_pct)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA200')}>ma200 dist</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_ma200_distance_pct)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('MA200 slope')}>ma200 slope</JargonTip></dt><dd class="text-fg text-right whitespace-nowrap">{c.technical_ma200_slope_pct_per_day !== null ? fmtPct(c.technical_ma200_slope_pct_per_day, 3) + '/d' : '—'}</dd>
						<dt class="text-fg-muted uppercase tracking-widest"><JargonTip {...tipProps('ATR')}>atr</JargonTip></dt><dd class="text-fg text-right">{fmtPct(c.technical_atr_pct)}</dd>
						<dt class="text-fg-muted uppercase tracking-widest">earnings yoy</dt><dd class="text-fg text-right whitespace-nowrap">{fmtPct(oneil?.oneil_earnings_growth_yoy_pct)}</dd>
					</dl>
				</div>
			</div>

			<!-- INSIDER / FLOW — ownership-flow domain (one metric, its own strip). -->
			<div class="px-4 sm:px-5 py-4 border-t border-grid">
				<div class="text-[10px] uppercase tracking-widest text-cyan mb-3">insider / flow</div>
				{#if insider.mode === 'bar'}
					<SignalBar
						label="insider 90d (sector %ile)"
						value={insider.percentile}
						format={(v) => fmtPctile(v) + '%ile'}
						tooltip="Net opportunistic insider buying ({fmtUsdCompact(insider.netUsd)}) in the last 90 days, ranked within sector — shown only when there is net buying. Cohen-Malloy opportunistic classification; paradigm #11 scorer."
					/>
				{:else}
					<SignalBar
						label="insider 90d"
						value={null}
						placeholder={insider.label}
						tooltip="No net opportunistic insider buying in the last 90 days. The sector percentile is suppressed: a 0/negative dollar signal ranks high only relative to net-selling peers, which is not a buy signal. Cohen-Malloy opportunistic classification."
					/>
				{/if}
			</div>

			<!-- Expert-panel deep-read (PR-8b): the generalized drawer — disagreement
			     headline + dot-lane (only when >=2 lenses scored) + one section per
			     expert (Buffett qual pillars + rationale; O'Neil numeric readouts +
			     audit flags). Hidden by default; renders nothing when no lens has
			     content for this name. The transition shim lives inside (reads the
			     persisted panel.expert_spread, never recomputes). -->
			<ExpertPanel
				assessments={c.expert_assessments}
				layer4Score={c.layer4_weighted_score}
				atrPenalty={c.atr_penalty}
				selectionScore={c.selection_score}
				scorerConfigVersion={c.scorer_config_version}
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
