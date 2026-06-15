<script lang="ts">
	// The generalized expert-panel deep-read drawer (PR-8b). Replaces the single
	// Buffett drawer: one accordion that stacks one section per expert, handling a
	// QUAL expert (Buffett — pillar badges + LLM rationale) and a NUMERIC expert
	// (O'Neil — readout list + audit-flag badges, no rationale) via the EXPERT_KIND
	// map. The disagreement headline + dot-lane render ONLY when the persisted
	// expert_spread is finite (>=2 lenses scored). Display-only: the band word + its
	// colour live HERE with a visible "unvalidated · not a buy/avoid signal" label,
	// never on the resting card face (that chip is tone-neutral coverage). The
	// transition shim is a single predicate — Number.isFinite(panel.expert_spread):
	// we NEVER recompute the spread client-side (the pipeline owns the formula), so a
	// pre-PR-8a row (no panel key) degrades to "just the per-expert sections, no
	// headline / dot-lane", never a wrong or flipping number.
	import type { BuffettAssessment, ExpertAssessments, ONeilAssessment } from '$lib/types';
	import { EXPERT_KIND } from '$lib/types';
	import {
		fmtPct,
		fmtPctile,
		fmtNum,
		fmtDate,
		buffettTone,
		oneilTone,
		consensusBand,
		consensusTone,
		moatTone,
		moatTrendTone,
		candorTone,
		understoodTone,
		understoodLabel,
		panelMagnitudeFormula,
		type BuffettTone
	} from '$lib/format';
	import { ChevronRight } from 'lucide-svelte';
	import ExpertPillar from './ExpertPillar.svelte';

	interface Props {
		assessments: ExpertAssessments | null | undefined;
	}
	let { assessments }: Props = $props();

	const buf = $derived((assessments?.buffett ?? null) as BuffettAssessment | null);
	const oneil = $derived((assessments?.oneil ?? null) as ONeilAssessment | null);
	const panel = $derived(assessments?.panel ?? null);

	const buffScore = $derived(
		Number.isFinite(buf?.buffett_quality_score) ? Math.round(buf!.buffett_quality_score as number) : null
	);
	const oneilScore = $derived(
		Number.isFinite(oneil?.oneil_score) ? Math.round(oneil!.oneil_score as number) : null
	);

	// Transition shim: the persisted spread is the SOLE source — never recomputed.
	const spread = $derived(
		Number.isFinite(panel?.expert_spread) ? (panel!.expert_spread as number) : null
	);
	const bothScored = $derived(buffScore !== null && oneilScore !== null);

	// Buffett qualitative pillars (moat / trend / candor / understood).
	const hasBuffQual = $derived(
		!!buf?.buffett_moat_type ||
			!!buf?.buffett_qualitative_rationale ||
			buf?.buffett_understandable != null ||
			!!buf?.buffett_moat_trend ||
			!!buf?.buffett_management_candor
	);
	const buffPillars = $derived([
		{
			label: 'moat',
			value: buf?.buffett_moat_type || '—',
			tone: moatTone(buf?.buffett_moat_type),
			body: 'The dominant durable competitive advantage the LLM could evidence from the 10-K (brand / cost / switching-cost / network / regulatory / intangible / none).'
		},
		{
			label: 'trend',
			value: buf?.buffett_moat_trend || '—',
			tone: moatTrendTone(buf?.buffett_moat_trend),
			body: 'Whether that advantage looks to be widening, stable, narrowing, or unclear — judged from the risk-factor evolution + margin/ROIC trend.'
		},
		{
			label: 'candor',
			value: buf?.buffett_management_candor || '—',
			tone: candorTone(buf?.buffett_management_candor),
			body: "Reading of the MD&A's tone: candid about problems, mixed, promotional, or too little to tell."
		},
		{
			label: 'understood',
			value: understoodLabel(buf?.buffett_understandable),
			tone: understoodTone(buf?.buffett_understandable),
			body: 'Could a generalist clearly explain what the company sells and how it earns money from Item 1 — or is it "too hard"?'
		}
	]);

	// O'Neil numeric readouts + the two audit flags (badges only on strict `=== true`,
	// which is why the parquet bool-as-float must round-trip through coerce_optional_bool).
	const hasOneil = $derived(
		oneilScore !== null ||
			Number.isFinite(oneil?.oneil_pct_off_52w_high) ||
			Number.isFinite(oneil?.oneil_ma200_slope_pct_per_day) ||
			Number.isFinite(oneil?.oneil_earnings_growth_yoy_pct) ||
			Number.isFinite(oneil?.oneil_rs_approx_pct)
	);
	const oneilReadouts = $derived([
		{ label: 'off 52w high', value: fmtPct(oneil?.oneil_pct_off_52w_high) },
		// Relative-strength is a 0-100 percentile RANK (O'Neil RS), not a signed % change:
		// render it as "N%ile" (matching the sector-percentile readouts), never fmtPct (+N%).
		{
			label: 'rel strength',
			value: Number.isFinite(oneil?.oneil_rs_approx_pct)
				? `${fmtPctile(oneil?.oneil_rs_approx_pct)}%ile`
				: '—'
		},
		{ label: 'MA200 slope/d', value: fmtPct(oneil?.oneil_ma200_slope_pct_per_day, 2) },
		{ label: 'MA200 dist', value: fmtPct(oneil?.oneil_ma200_distance_pct) },
		{ label: 'earnings YoY', value: fmtPct(oneil?.oneil_earnings_growth_yoy_pct) }
	]);

	// The drawer is offered when ANY expert has renderable content or a spread exists.
	const hasContent = $derived(hasBuffQual || hasOneil || spread !== null);

	// Registry order (buffett, oneil). A 3rd expert is one entry in EXPERT_KIND.
	const sections = $derived(
		['buffett', 'oneil'].filter((id) =>
			id === 'buffett' ? hasBuffQual : id === 'oneil' ? hasOneil : false
		)
	);

	let open = $state(false);

	function toneDot(t: BuffettTone): string {
		return t === 'green' ? 'bg-green' : t === 'amber' ? 'bg-amber' : 'bg-fg-muted';
	}
</script>

{#if hasContent}
	<div class="px-4 sm:px-5 py-3 border-t border-grid">
		<button
			type="button"
			class="flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-cyan hover:text-amber transition-colors"
			aria-expanded={open}
			onclick={() => (open = !open)}
		>
			<ChevronRight class="size-3 transition-transform {open ? 'rotate-90' : ''}" />
			expert.panel
		</button>
		{#if open}
			<div class="mt-3 space-y-4">
				<!-- Disagreement headline + dot-lane: only when the persisted spread is
				     finite (>=2 lenses scored). Band word + colour are display-only and
				     explicitly labelled unvalidated. -->
				{#if spread !== null && bothScored}
					{@const band = consensusBand(spread)}
					{@const bandTone = consensusTone(spread)}
					<div class="space-y-2">
						<p class="text-xs leading-relaxed text-fg-dim">
							<span class="whitespace-nowrap">Buffett {buffScore} (value/quality)</span>
							<span class="text-fg-muted"> vs </span>
							<span class="whitespace-nowrap">O'Neil {oneilScore} (momentum)</span>
							<span class="text-fg-muted"> — lenses </span>
							<span
								class="font-bold uppercase tracking-widest text-[10px]"
								class:text-green={bandTone === 'green'}
								class:text-amber={bandTone === 'amber'}
								class:text-red={bandTone === 'red'}>{band}</span
							>
							<span class="text-fg-muted whitespace-nowrap"> (spread {Math.round(spread)})</span>
						</p>
						<!-- Dot-lane: one dot per present score on a 0-100 track, coloured by
						     that expert's OWN tone. Visual distance = the spread. -->
						<div class="relative h-1 w-full bg-grid" aria-hidden="true">
							{#if buffScore !== null}
								<span
									class="absolute top-1/2 size-2 -translate-y-1/2 -translate-x-1/2 rounded-full {toneDot(
										buffettTone(buf?.buffett_quality_score)
									)}"
									style="left: {buffScore}%"
								></span>
							{/if}
							{#if oneilScore !== null}
								<span
									class="absolute top-1/2 size-2 -translate-y-1/2 -translate-x-1/2 rounded-full {toneDot(
										oneilTone(oneil?.oneil_score)
									)}"
									style="left: {oneilScore}%"
								></span>
							{/if}
						</div>
						<!-- Status bar: the disclaimer is the loudest line (the caveat people
						     act on), the opaque config slug is decoded into a plain-language
						     magnitude formula and demoted to a hover-titled audit tag.
						     Display-only, unvalidated — never a buy/avoid word. -->
						<div class="space-y-1.5 rounded-sm border-l-2 border-amber bg-bg-1 px-3 py-2">
							<p class="text-xs leading-snug text-fg">
								<span
									class="text-[10px] font-bold uppercase tracking-widest text-amber whitespace-nowrap"
									>display-only</span
								>
								<span class="text-grid-strong"> · </span>not a buy or avoid signal
							</p>
							<p class="text-[10px] leading-snug text-fg-muted">
								<span class="uppercase tracking-wider">magnitude</span>
								<span class="text-fg-dim whitespace-nowrap"
									>{panelMagnitudeFormula(panel?.panel_config_version)}</span
								>
								<span class="text-grid-strong"> · </span>unvalidated
								<span class="text-grid-strong"> · </span>
								<span class="whitespace-nowrap" title="panel config version — audit trace"
									>{panel?.panel_config_version ?? 'panel'}</span
								>
							</p>
						</div>
					</div>
				{/if}

				<!-- Per-expert sections, registry order. -->
				{#each sections as id (id)}
					<div class="space-y-2 border-t border-grid pt-3 first:border-t-0 first:pt-0">
						{#if EXPERT_KIND[id] === 'qual'}
							<p class="text-[10px] uppercase tracking-widest text-fg-muted">
								buffett <span class="font-bold normal-case">{buffScore ?? '—'}/100</span>
							</p>
							<div class="flex flex-wrap gap-2">
								{#each buffPillars as pillar (pillar.label)}
									<ExpertPillar label={pillar.label} value={pillar.value} tone={pillar.tone} body={pillar.body} />
								{/each}
							</div>
							{#if buf?.buffett_qualitative_rationale}
								<blockquote class="border-l-2 border-violet pl-4">
									<p class="text-fg-dim text-xs leading-relaxed">{buf.buffett_qualitative_rationale}</p>
								</blockquote>
							{/if}
							<div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[9px] text-fg-muted">
								{#if buf?.buffett_used_scuttlebutt}
									<span class="text-amber whitespace-nowrap">scuttlebutt: web-grounded, unverified</span>
								{/if}
								{#if buf?.buffett_qual_computed_at}
									<span class="whitespace-nowrap">classified {fmtDate(buf.buffett_qual_computed_at)}</span>
								{/if}
							</div>
						{:else}
							<p class="text-[10px] uppercase tracking-widest text-fg-muted">
								oneil <span class="font-bold normal-case">{oneilScore ?? '—'}/100</span>
							</p>
							<dl class="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
								{#each oneilReadouts as r (r.label)}
									<div>
										<dt class="text-[9px] uppercase tracking-widest text-fg-muted">{r.label}</dt>
										<dd class="font-bold text-fg-dim whitespace-nowrap">{r.value}</dd>
									</div>
								{/each}
							</dl>
							{#if oneil?.oneil_new_high_split_suspected === true || oneil?.oneil_earnings_growth_near_zero_base === true}
								<div class="flex flex-wrap gap-2">
									{#if oneil?.oneil_new_high_split_suspected === true}
										<ExpertPillar
											label="data"
											value="split-suspected"
											tone="bad"
											body="A suspected stock split in the raw-close window contaminates the 52-week-high reference, so the new-high term (and the O'Neil score) is withheld for this name."
										/>
									{/if}
									{#if oneil?.oneil_earnings_growth_near_zero_base === true}
										<ExpertPillar
											label="data"
											value="near-zero base"
											tone="mixed"
											body="The prior-year earnings base is near zero, so the year-over-year growth % would explode into an uninformative number — the earnings term is excluded."
										/>
									{/if}
								</div>
							{/if}
							<p class="text-[9px] text-fg-muted">price panel + EDGAR fundamentals · numeric-only, no LLM</p>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
	</div>
{/if}
