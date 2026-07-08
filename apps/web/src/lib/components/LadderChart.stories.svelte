<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import LadderChart from './LadderChart.svelte';
	import type { ChartPayload, ChartBar, ChartMarker } from '$lib/types';

	// ── Payload builders (shapes taken from tests/unit/ladderChart.test.ts) ──

	function bar(time: string, close: number): ChartBar {
		return { time, open: close * 0.99, high: close * 1.01, low: close * 0.98, close, volume: 500_000 };
	}

	function marker(
		kind: ChartMarker['kind'],
		time: string,
		label: string,
		level_id: string | null = label.toLowerCase()
	): ChartMarker {
		return { time, kind, level_id, price: 0, label, ambiguous: false };
	}

	// CLOSED: terminal=true, realized_r set, entry+tp markers fired.
	// Multi-tranche scale-out shape mirrors the test file.
	const CLOSED_PAYLOAD: ChartPayload = {
		status: 'OK',
		ticker: 'NVDA',
		brief_date: '2026-06-13',
		ladder_classification: 'BULLISH_REVERSAL',
		terminal: true,
		holding_days_elapsed: 10,
		realized_r: 1.45,
		open_r: null,
		ambiguous_bars: 0,
		intrabar_rule: 'SL-first',
		rth_only: true,
		bars: [
			bar('2026-06-10', 118),
			bar('2026-06-11', 117),
			bar('2026-06-12', 116),
			bar('2026-06-13', 115),
			bar('2026-06-16', 113),
			bar('2026-06-17', 112),
			bar('2026-06-18', 114),
			bar('2026-06-19', 116),
			bar('2026-06-20', 118),
			bar('2026-06-23', 121)
		],
		price_lines: {
			entry: 113.0,
			tp: [117.0, 121.0],
			stop: 110.5
		},
		markers: [
			marker('ENTRY', '2026-06-16', 'E1', 'e1'),
			marker('TP', '2026-06-19', 'TP1', 'tp1'),
			marker('TP', '2026-06-23', 'TP2', 'tp2')
		]
	};

	// OPEN: terminal=false, ENTRY fired, one pending TP not hit, stop pending.
	const OPEN_PAYLOAD: ChartPayload = {
		status: 'OK',
		ticker: 'AAPL',
		brief_date: '2026-06-20',
		ladder_classification: 'BULLISH_REVERSAL',
		terminal: false,
		holding_days_elapsed: 4,
		realized_r: null,
		open_r: 0.38,
		ambiguous_bars: 0,
		intrabar_rule: 'SL-first',
		rth_only: true,
		bars: [
			bar('2026-06-18', 192),
			bar('2026-06-19', 191),
			bar('2026-06-20', 190),
			bar('2026-06-23', 189),
			bar('2026-06-24', 191),
			bar('2026-06-25', 193),
			bar('2026-06-26', 192)
		],
		price_lines: {
			entry: 189.5,
			tp: [194.0],
			stop: 186.5
		},
		markers: [
			marker('ENTRY', '2026-06-23', 'E1', 'e1')
		]
	};

	// PLANNED: terminal=false, no markers (plan preview — not triggered yet).
	// Still renders bars + dashed price lines — NOT an empty-box fallback.
	const PLANNED_PAYLOAD: ChartPayload = {
		status: 'OK',
		ticker: 'MSFT',
		brief_date: '2026-06-27',
		ladder_classification: 'BULLISH_REVERSAL',
		terminal: false,
		holding_days_elapsed: null,
		realized_r: null,
		open_r: null,
		ambiguous_bars: 0,
		intrabar_rule: 'SL-first',
		rth_only: true,
		bars: [
			bar('2026-06-25', 442),
			bar('2026-06-26', 440),
			bar('2026-06-27', 439),
			bar('2026-06-30', 437),
			bar('2026-07-01', 436)
		],
		price_lines: {
			entry: 436.0,
			tp: [445.0, 452.0],
			stop: 431.5
		},
		markers: []
	};

	const { Story } = defineMeta({
		title: 'Data-viz/LadderChart',
		component: LadderChart,
		tags: ['autodocs'],
		parameters: { layout: 'padded' }
	});
</script>

<!-- CLOSED: history view — trade fully exited, realized R displayed in the chip. -->
<Story
	name="Closed Trade"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByTestId('ladder-lifecycle-chip')).toBeVisible());
		await waitFor(() =>
			expect(canvas.getByTestId('ladder-lifecycle-chip').textContent).toMatch(/closed/i)
		);
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart payload={CLOSED_PAYLOAD} />
		</div>
	{/snippet}
</Story>

<!-- OPEN: live view — entry fired, unrealized R shown in the chip. -->
<Story
	name="Open Trade"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByTestId('ladder-lifecycle-chip')).toBeVisible());
		await waitFor(() =>
			expect(canvas.getByTestId('ladder-lifecycle-chip').textContent).toMatch(/open/i)
		);
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart payload={OPEN_PAYLOAD} />
		</div>
	{/snippet}
</Story>

<!-- PLANNED: plan preview — bars + dashed price lines, no fills, not triggered yet. -->
<Story
	name="Planned Not Triggered"
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByTestId('ladder-lifecycle-chip')).toBeVisible());
		await waitFor(() =>
			expect(canvas.getByTestId('ladder-lifecycle-chip').textContent).toMatch(/planned/i)
		);
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart payload={PLANNED_PAYLOAD} />
		</div>
	{/snippet}
</Story>

<!-- SIM chip tooltip: focuses the chip trigger and asserts the honesty body appears.
     Extra vertical padding ensures the tooltip bubble is not clipped above. -->
<Story
	name="SIM Chip Tooltip"
	play={async ({ canvas }) => {
		canvas.getByTestId('chip-tip').focus();
		await waitFor(() =>
			expect(canvas.getByText(/All fills and exits are bar-replay modeled/i)).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 24rem; padding: 6rem 4rem 2rem;">
			<LadderChart payload={CLOSED_PAYLOAD} />
		</div>
	{/snippet}
</Story>

<!-- No Structure: status NO_STRUCTURE — renders dotted-border empty box, not the chart. -->
<Story
	name="No Structure"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.getByText(/no structured ladder/i)).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="width: 34rem; height: 18rem; padding: 2rem 3rem;">
			<LadderChart
				payload={{
					status: 'NO_STRUCTURE',
					ticker: 'XYZ',
					brief_date: '2026-06-27',
					ladder_classification: 'NO_STRUCTURE',
					terminal: false,
					holding_days_elapsed: null,
					realized_r: null,
					open_r: null,
					ambiguous_bars: 0,
					intrabar_rule: null,
					rth_only: true,
					bars: [],
					price_lines: { entry: null, tp: [], stop: null },
					markers: []
				}}
			/>
		</div>
	{/snippet}
</Story>
