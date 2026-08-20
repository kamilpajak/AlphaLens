<script lang="ts">
	// The causal-support record, rendered as a MARGIN NOTE inside catalyst.event
	// (#1069).
	//
	// Placement is the argument. It closes the block the reader has just read
	// top-to-bottom — thesis, the source event, then what that event actually
	// carries — because the record is a comment on the EVENT and belongs beside
	// it, not three sections down. It is deliberately NOT a chip in the meta bar:
	// that bar is where SCORES live, and causal support is not a score.
	//
	// Tone discipline (unvalidated-display doctrine + issue #1069):
	//   - quiet by default. `not_established` on a grounded row is the instrument
	//     working correctly and reporting a weak link — honest uncertainty, which
	//     #1068 exists to keep separate from a broken measurement. Flagging it
	//     would teach the reader to ignore the flag.
	//   - emphasis (amber) only when the measurement itself failed, or when the
	//     guard had to withhold prose. Those are integrity failures.
	//   - no verdict vocabulary, no score, no confidence number. The vote
	//     telemetry stays parquet-only on purpose: a self-reported float here
	//     would invite exactly the calibrated-sounding reading the level avoids.
	import type { Candidate } from '$lib/types';
	import { channelRecord, CAUSAL_SUPPORT_NOT_A_FORECAST } from '$lib/format';
	import Disclosure from './Disclosure.svelte';
	import ChipTip from './ChipTip.svelte';
	import MetricGrid from './MetricGrid.svelte';

	interface Props {
		candidate: Candidate;
	}
	let { candidate: c }: Props = $props();

	const rec = $derived(channelRecord(c));
	const flagged = $derived(rec?.emphasis === 'flag');

	// The chain the assessor named. All three are empty by construction on a
	// `none` channel — a real answer ("no mechanism"), not a missing value — and
	// an empty chain simply removes the drawer rather than filling it with
	// em-dashes the reader has to interpret.
	const chain = $derived([
		{ label: 'mechanism', value: c.channel_text ?? '' },
		{ label: 'quoted from the event', value: c.channel_evidence ?? '' },
		{ label: 'would falsify it', value: c.channel_falsifier ?? '' }
	].filter((r) => r.value.trim() !== ''));

	// The span that placed this company inside the event. The grounding REASON is
	// deliberately absent — it is already on the face as the amber note, and a
	// drawer whose only content repeats the line above it is an empty promise.
	//
	// The two questions differ (what supports the chain vs what places the company
	// in the event) but the assessor often answers both with the same sentence, so
	// a span already contained in the evidence is dropped rather than printed
	// twice. Containment, not equality: the evidence is frequently the longer
	// quote with the grounding span inside it.
	const rawGroundingQuote = $derived((c.channel_grounding_quote ?? '').trim());
	const groundingQuote = $derived(
		rawGroundingQuote && !(c.channel_evidence ?? '').includes(rawGroundingQuote)
			? rawGroundingQuote
			: ''
	);

	// Raw pipeline tokens, for the operator reading the parquet beside the card.
	const tokenRows = $derived([
		{ key: 'causal support', value: rec?.support ?? '—' },
		{ key: 'grounding', value: rec?.grounding || '—' },
		...(c.channel_type ? [{ key: 'channel type', value: c.channel_type }] : [])
	]);

	// No drawer when there is nothing behind it. A row whose assessor named no
	// chain and quoted no span (every misroute / misfit row) says all it has to
	// say on the face — offering a disclosure there would promise detail that
	// does not exist.
	const hasDrawer = $derived(chain.length > 0 || groundingQuote !== '');

	// One interpolated literal, NOT `class:border-amber` + `class:border-opacity-40`:
	// Tailwind v4 dropped the `*-opacity-*` utilities, so the second class would
	// silently do nothing and the flagged rule would paint at full amber. Both
	// strings appear verbatim here, so the scanner still generates them.
	const ruleClass = $derived(flagged ? 'border-amber/40' : 'border-grid');
</script>

{#if rec}
	<!-- Sits AFTER the source-event line: thesis, then the event it came from,
	     then what that event actually carries. The record comments on the source,
	     so it belongs next to it. The hairline rule takes an amber tint when the
	     measurement broke — the card's existing way of raising a voice, and the
	     only non-textual signal here (no glyph, no gauge). -->
	<div data-testid="channel-record" class="mt-3 border-t pt-2.5 {ruleClass}">
		<div class="flex flex-wrap items-baseline gap-x-2 gap-y-1">
			<ChipTip term="causal support">
				{#snippet chip()}
					<span
						class="cursor-help text-[9px] uppercase tracking-widest"
						class:text-amber={flagged}
						class:text-fg-muted={!flagged}
					>
						evidence
					</span>
				{/snippet}
				{#snippet bodyRich()}
					<span class="block"
						>What the source event itself carries — checked by a second model pass, separately
						from the prose above.</span
					>
					<MetricGrid rows={tokenRows} align="right" class="mt-1" />
					<p class="mt-2 text-[10px] italic text-fg-muted">{CAUSAL_SUPPORT_NOT_A_FORECAST}</p>
				{/snippet}
			</ChipTip>
			<span class="text-[11px]" class:text-amber={flagged} class:text-fg-dim={!flagged}
				>{rec.headline}</span
			>
		</div>

		{#if rec.groundingNote}
			<!-- A grounding failure is a defect in the pipeline, not a finding about
			     the company. Saying so plainly is what lets the reader discount the
			     thesis instead of quietly distrusting the whole card. -->
			<p class="mt-1 text-[11px] text-amber/90">{rec.groundingNote}</p>
		{/if}
		{#if rec.guardNote}
			<p class="mt-1 text-[11px]" class:text-amber={flagged} class:text-fg-muted={!flagged}>
				{rec.guardNote}
			</p>
		{/if}

		{#if hasDrawer}
			<Disclosure
				detailsClass="mt-2"
				summaryClass="inline-flex items-center gap-1.5 text-[9px] uppercase tracking-widest text-fg-muted hover:text-amber transition-colors"
				chevronClass="text-fg-muted"
			>
				{#snippet summary(open)}
					<span>{open ? 'hide record' : 'record'}</span>
				{/snippet}
				<div class="mt-2 flex flex-col gap-2 border-l border-grid-strong pl-3">
					{#each chain as row (row.label)}
						<div>
							<div class="text-[9px] uppercase tracking-widest text-fg-muted">{row.label}</div>
							<p class="text-[11px] leading-relaxed text-fg-dim">{row.value}</p>
						</div>
					{/each}
					{#if groundingQuote}
						<div>
							<div class="text-[9px] uppercase tracking-widest text-fg-muted">
								grounding · quoted span
							</div>
							<p class="text-[11px] leading-relaxed text-fg-dim">{groundingQuote}</p>
						</div>
					{/if}
					<p class="text-[10px] italic text-fg-muted">{CAUSAL_SUPPORT_NOT_A_FORECAST}</p>
				</div>
			</Disclosure>
		{/if}
	</div>
{/if}
