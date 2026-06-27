<script lang="ts">
	// The generalized expert-panel deep-read drawer (PR-8b, rebuilt for readability).
	// One accordion that stacks a per-expert "scorecard" — a QUAL expert (Buffett:
	// pillar badges + LLM rationale) and a NUMERIC expert (O'Neil: readout grid +
	// audit-flag badges, no rationale) via the EXPERT_KIND map. Above the cards, a
	// disagreement SCALE (the two lens scores plotted on one 0-100 track with the gap
	// shaded) renders ONLY when the persisted expert_spread is finite (>=2 lenses
	// scored). Display-only throughout: the band word + its colour live HERE in the
	// drawer header, never on the resting card face (that chip is tone-neutral
	// coverage). The transition shim is a
	// single predicate — Number.isFinite(panel.expert_spread): we NEVER recompute the
	// spread client-side (the pipeline owns the formula), so a pre-PR-8a row (no panel
	// key) degrades to "just the per-expert cards, no scale", never a wrong number.
	import type { BuffettAssessment, ExpertAssessments, ONeilAssessment } from '$lib/types';
	import { EXPERT_KIND } from '$lib/types';
	import {
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
		type BuffettTone
	} from '$lib/format';
	import { ChevronRight } from 'lucide-svelte';
	import ExpertPillar from './ExpertPillar.svelte';

	interface Props {
		assessments: ExpertAssessments | null | undefined;
		layer4Score?: number | null;
		atrPenalty?: number | null;
		selectionScore?: number | null;
		scorerConfigVersion?: string | null;
		/** Whether a 10-K exists (from the tenk gate) — explains an absent Buffett
		 *  qualitative read. */
		tenkAvailable?: boolean | null;
	}
	let {
		assessments,
		layer4Score,
		atrPenalty,
		selectionScore,
		scorerConfigVersion,
		tenkAvailable
	}: Props = $props();

	// Score breakdown: show the ATR-penalty breakdown row when any of the three
	// scorer cols is present. The precise numbers live HERE (drawer), not on the
	// card face (the extended chip carries no number — manufactured-authority guard).
	const hasScoreBreakdown = $derived(
		selectionScore != null || atrPenalty != null || scorerConfigVersion != null
	);

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
	// The disagreement scale needs both markers; it plots the two lens scores together.
	// gapLeft/gapWidth are meaningful ONLY when bothScored (the dummy 0 fallback is
	// never rendered — every read is inside the `{#if showScale}` guard below).
	const showScale = $derived(spread !== null && bothScored);
	const gapLeft = $derived(bothScored ? Math.min(buffScore!, oneilScore!) : 0);
	const gapWidth = $derived(bothScored ? Math.abs(buffScore! - oneilScore!) : 0);

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

	// O'Neil audit flags (badges only on strict `=== true`,
	// which is why the parquet bool-as-float must round-trip through coerce_optional_bool).
	// Numeric readouts have moved to the Momentum & Technicals block on the card face.
	const hasOneil = $derived(
		oneilScore !== null ||
			Number.isFinite(oneil?.oneil_pct_off_52w_high) ||
			Number.isFinite(oneil?.oneil_ma200_slope_pct_per_day) ||
			Number.isFinite(oneil?.oneil_earnings_growth_yoy_pct) ||
			Number.isFinite(oneil?.oneil_rs_approx_pct)
	);

	// The drawer is offered when ANY expert has renderable content, a spread exists,
	// or the scorer breakdown (selection_score / atr_penalty / config_version) is present.
	const hasContent = $derived(
		hasBuffQual || hasOneil || spread !== null || hasScoreBreakdown || buffScore !== null
	);

	// Registry order (buffett, oneil). A 3rd expert is one entry in EXPERT_KIND.
	const sections = $derived(
		['buffett', 'oneil'].filter((id) =>
			id === 'buffett' ? hasBuffQual || buffScore !== null : id === 'oneil' ? hasOneil : false
		)
	);

	let open = $state(false);

	// Tone → marker fill / score text colour. The score number and its scale marker
	// share one colour so a weak lens (muted) and a strong lens (green) read at a glance.
	function toneDot(t: BuffettTone): string {
		return t === 'green' ? 'bg-green' : t === 'amber' ? 'bg-amber' : 'bg-fg-muted';
	}
	function toneText(t: BuffettTone): string {
		return t === 'green' ? 'text-green' : t === 'amber' ? 'text-amber' : 'text-fg-muted';
	}
	// Anchor a marker's label so the edge scores (0 / 100) do not clip past the track
	// ends: a continuous -p% shift left-anchors at 0, centres at 50, right-anchors at
	// 100 — no hard-threshold jump as the score moves between rows.
	function labelShift(p: number): string {
		return `translateX(-${Math.max(0, Math.min(100, p))}%)`;
	}
</script>

{#if hasContent}
	<div class="px-4 sm:px-5 py-3 border-t border-grid">
		<!-- Trigger row + (when open & both scored) the one-glance disagreement verdict. -->
		<div class="flex items-center justify-between gap-2">
			<button
				type="button"
				class="flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-cyan hover:text-amber transition-colors"
				aria-expanded={open}
				onclick={() => (open = !open)}
			>
				<ChevronRight class="size-3 transition-transform {open ? 'rotate-90' : ''}" />
				expert.panel
			</button>
			{#if open && showScale}
				{@const bandTone = consensusTone(spread)}
				<span class="text-[10px] uppercase tracking-widest text-fg-muted whitespace-nowrap">
					lenses
					<span
						class="font-bold"
						class:text-green={bandTone === 'green'}
						class:text-amber={bandTone === 'amber'}
						class:text-red={bandTone === 'red'}>{consensusBand(spread)}</span
					>
					<span class="text-grid-strong">·</span> gap {Math.round(spread!)}
				</span>
			{/if}
		</div>

		{#if open}
			<div data-testid="expert-panel-body" class="mt-3 space-y-4">
				<!-- Disagreement scale: the two lens scores on one 0-100 track, the gap
				     between them shaded. Replaces the old thin dot-lane + headline sentence.
				     Renders only when the persisted spread is finite (>=2 lenses scored). -->
				{#if showScale}
					{@const bandTone = consensusTone(spread)}
					{@const buffT = buffettTone(buf?.buffett_quality_score)}
					{@const oneilT = oneilTone(oneil?.oneil_score)}
					<div>
						<div class="flex justify-between text-[9px] uppercase tracking-widest text-fg-muted">
							<span>lens score</span>
							<span>0–100</span>
						</div>
						<!-- Buffett label row (above the track) -->
						<div class="relative mt-2 h-3.5">
							<span
								data-testid="lens-label-buffett"
								class="absolute bottom-0 text-[9px] whitespace-nowrap {toneText(buffT)}"
								style="left: {buffScore}%; transform: {labelShift(buffScore!)}"
							>
								Buffett {buffScore}
							</span>
						</div>
						<!-- track (overflow-hidden clips the dots cleanly at 0/100) -->
						<div class="relative h-1.5 overflow-hidden rounded-full bg-grid" aria-hidden="true">
							<span
								class="absolute top-0 h-1.5 rounded-full"
								class:bg-green={bandTone === 'green'}
								class:bg-amber={bandTone === 'amber'}
								class:bg-red={bandTone === 'red'}
								style="left: {gapLeft}%; width: {gapWidth}%; opacity: 0.22"
							></span>
							<span
								class="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-bg {toneDot(
									buffT
								)}"
								style="left: {buffScore}%"
							></span>
							<span
								class="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-bg {toneDot(
									oneilT
								)}"
								style="left: {oneilScore}%"
							></span>
						</div>
						<!-- O'Neil label row (below the track) -->
						<div class="relative mb-1 h-3.5">
							<span
								data-testid="lens-label-oneil"
								class="absolute top-0 text-[9px] whitespace-nowrap {toneText(oneilT)}"
								style="left: {oneilScore}%; transform: {labelShift(oneilScore!)}"
							>
								O'Neil {oneilScore}
							</span>
						</div>
					</div>
				{/if}

				<!-- Per-expert scorecards, registry order. -->
				{#each sections as id (id)}
					{@const isBuf = EXPERT_KIND[id] === 'qual'}
					{@const score = isBuf ? buffScore : oneilScore}
					{@const tone = isBuf
						? buffettTone(buf?.buffett_quality_score)
						: oneilTone(oneil?.oneil_score)}
					<div class="border-t border-grid pt-4 first:border-t-0 first:pt-0">
						<!-- Card header: identity swatch + name + lens kind | big tone-coloured score. -->
						<div class="flex items-baseline justify-between gap-3">
							<div class="flex items-center gap-2.5">
								<span
									class="h-7 w-[3px] rounded-sm {isBuf ? 'bg-cyan' : 'bg-magenta'}"
									aria-hidden="true"
								></span>
								<span>
									<span class="block font-display text-[15px] font-semibold leading-none">
										{isBuf ? 'Buffett' : "O'Neil"}
									</span>
									<span class="mt-1 block text-[9px] uppercase tracking-widest text-fg-muted">
										{isBuf ? 'value / quality' : 'momentum'}
									</span>
								</span>
							</div>
							<span class="font-display text-2xl font-semibold leading-none whitespace-nowrap {toneText(tone)}">
								{score ?? '—'}<span class="text-xs font-normal text-fg-muted">/100</span>
							</span>
						</div>

						{#if isBuf}
							{#if hasBuffQual}
								<div class="mt-3 flex flex-wrap gap-2">
									{#each buffPillars as pillar (pillar.label)}
										<ExpertPillar label={pillar.label} value={pillar.value} tone={pillar.tone} body={pillar.body} />
									{/each}
								</div>
								{#if buf?.buffett_qualitative_rationale}
									<blockquote class="mt-3 border-l-2 border-cyan pl-4">
										<p class="text-fg-dim text-xs leading-relaxed">{buf.buffett_qualitative_rationale}</p>
									</blockquote>
								{/if}
								<div class="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] leading-snug text-fg-muted">
									{#if buf?.buffett_used_scuttlebutt}
										<span class="text-amber whitespace-nowrap">scuttlebutt: web-grounded, unverified</span>
									{/if}
									{#if buf?.buffett_qual_computed_at}
										<span class="whitespace-nowrap">classified {fmtDate(buf.buffett_qual_computed_at)}</span>
									{/if}
								</div>
							{:else}
								<p class="mt-3 text-[10px] leading-snug text-fg-muted">
									numeric score only —
									<span class="text-fg-dim"
										>{tenkAvailable
											? 'qualitative read not computed'
											: 'no 10-K for a qualitative read'}</span
									>
								</p>
							{/if}
						{:else}
							{#if oneil?.oneil_new_high_split_suspected === true || oneil?.oneil_earnings_growth_near_zero_base === true}
								<div class="mt-3 flex flex-wrap gap-2">
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
							<p class="mt-3 text-[10px] leading-snug text-fg-muted">
								<span class="uppercase tracking-wider">source</span>
								<span class="text-fg-dim">price panel + EDGAR fundamentals</span>
								<span class="text-grid-strong"> · </span>numeric-only, no LLM
							</p>
							<p class="mt-1 text-[10px] leading-snug text-fg-muted">
								numeric readouts shown in <span class="text-fg-dim">Momentum &amp; Technicals</span>.
							</p>
						{/if}
					</div>
				{/each}

				<!-- Score breakdown: layer-4 → ATR penalty → selection_score.
				     Precise numbers live here (drawer), NOT on the card face.
				     Shown only when at least one scorer col is present. -->
				{#if hasScoreBreakdown}
					<div class="border-t border-grid pt-4">
						<div class="text-[9px] uppercase tracking-widest text-fg-muted mb-2">
							scorer breakdown
						</div>
						<dl class="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
							<dt class="text-[10px] uppercase tracking-widest text-fg-muted">layer-4</dt>
							<dd class="text-right font-bold text-fg-dim whitespace-nowrap">
								{layer4Score != null ? layer4Score.toFixed(2) : '—'}
							</dd>
							{#if atrPenalty != null && atrPenalty > 0}
								<dt class="text-[10px] uppercase tracking-widest text-fg-muted">atr penalty</dt>
								<dd class="text-right font-bold text-fg-muted whitespace-nowrap">
									<span class="whitespace-nowrap">−{atrPenalty.toFixed(2)}</span>
								</dd>
							{/if}
							<dt class="text-[10px] uppercase tracking-widest text-fg-muted">selection score</dt>
							<dd class="text-right font-bold text-fg whitespace-nowrap">
								<span class="whitespace-nowrap"
									>{selectionScore != null ? selectionScore.toFixed(2) : '—'}</span
								>
							</dd>
						</dl>
						{#if scorerConfigVersion}
							<p class="mt-2 text-[10px] text-fg-muted">
								<span class="whitespace-nowrap">{scorerConfigVersion}</span>
							</p>
						{/if}
						<p class="mt-1 text-[10px] italic text-fg-muted">
							suggestive — not yet validated
						</p>
					</div>
				{/if}
			</div>
		{/if}
	</div>
{/if}
