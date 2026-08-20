<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import TradeSetup from './TradeSetup.svelte';
	import type { TradeSetup as TradeSetupType } from '$lib/types';

	type TradeSetupProps = ComponentProps<typeof TradeSetup>;

	// ── Fixtures (values taken verbatim from
	//    tests/fixtures/api-mock/days/2026-05-18.json) ──────────────────────────

	// Candidate FOUR — status NO_STRUCTURE (downtrend / no valid long ladder).
	// The component renders the dashed empty-box branch with a "no structured
	// ladder" heading and the ref-close / ATR line below it.
	const NO_STRUCTURE_SETUP: TradeSetupType = {
		schema_version: '1.0.0',
		status: 'NO_STRUCTURE',
		asof_close: 88.4,
		atr: 5.74,
		disaster_stop: null,
		suggested_size_pct: null,
		order_ttl_days: 10,
		entry_tiers: [],
		tp_tranches: []
	};

	// Candidate PIPR — status OK, full 3-tier entry ladder + 3 TP tranches.
	// Fixture source: tests/fixtures/api-mock/days/2026-05-18.json candidates[2].
	const OK_SETUP: TradeSetupType = {
		schema_version: '1.0.0',
		status: 'OK',
		asof_close: 312.5,
		atr: 10.7,
		disaster_stop: 275.05,
		suggested_size_pct: 4.065583485277316,
		order_ttl_days: 10,
		entry_tiers: [
			{ limit: 307.15, alloc_pct: 27.98308726424079, atr_distance: 0.5, tag: 'shallow pullback' },
			{ limit: 299.66, alloc_pct: 34.35419283481206, atr_distance: 1.2, tag: 'momentum reset' },
			{ limit: 291.1, alloc_pct: 37.66271990094714, atr_distance: 2.0, tag: 'deep value add' }
		],
		tp_tranches: [
			{ target: 323.2, tranche_pct: 33.333333333333336, r_multiple: 0.3, tag: 'first scale-out' },
			{ target: 336.04, tranche_pct: 33.333333333333336, r_multiple: 0.6, tag: 'overhead resistance' },
			{ target: 349.95, tranche_pct: 33.333333333333336, r_multiple: 1.0, tag: 'measured-move target' }
		]
	};

	const { Story } = defineMeta({
		title: 'Composites/TradeSetup',
		component: TradeSetup,
		tags: ['autodocs'],
		parameters: { layout: 'padded' }
	});
</script>

<!-- NO_STRUCTURE: dashed empty box, "no structured ladder" heading, ref close + ATR line. -->
<Story
	name="No Structure"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText(/no structured ladder/i)).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="width: 28rem; padding: 4rem 6rem;">
			<TradeSetup setup={NO_STRUCTURE_SETUP} />
		</div>
	{/snippet}
</Story>

<!-- OK: full ladder — sizing headline (suggested size / disaster stop) + entry
     tiers + TP tranches. Fixture carries NO `label` on its tiers, so the entry
     rows must render the POSITION-derived E1/E2/E3 (regression: the old code
     rendered "TIER 1/2/3"). Asserts the sizing labels and E1/E2/E3 are visible. -->
<Story
	name="Structured Ladder"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText(/suggested size/i)).toBeVisible());
		await waitFor(() => expect(canvas.getByText(/disaster stop/i)).toBeVisible());
		await waitFor(() => expect(canvas.getByText('E1')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('E2')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('E3')).toBeVisible());
		expect(canvas.queryByText(/tier 1/i)).toBeNull();
	}}
>
	{#snippet template()}
		<div style="width: 28rem; padding: 4rem 6rem;">
			<TradeSetup setup={OK_SETUP} />
		</div>
	{/snippet}
</Story>

<!-- OK with data labels: same PIPR fixture, now with the canonical `label`
     stamped by the pipeline. The card renders the data label directly (same
     E1/E2/E3 vocabulary) rather than re-deriving it from position. -->
<Story
	name="Structured Ladder (data labels)"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText('E1')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('E2')).toBeVisible());
		await waitFor(() => expect(canvas.getByText('E3')).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="width: 28rem; padding: 4rem 6rem;">
			<TradeSetup
				setup={{
					...OK_SETUP,
					entry_tiers: OK_SETUP.entry_tiers.map((t, i) => ({ ...t, label: `E${i + 1}` }))
				}}
			/>
		</div>
	{/snippet}
</Story>

<!-- Null setup: component receives null — same empty-box branch as NO_STRUCTURE
     but without any price line below (setup?.asof_close is null). -->
<Story
	name="Null Setup"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByText(/no structured ladder/i)).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="width: 28rem; padding: 4rem 6rem;">
			<TradeSetup setup={null} />
		</div>
	{/snippet}
</Story>
