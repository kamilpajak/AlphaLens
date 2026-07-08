<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import ExcessScatter from './ExcessScatter.svelte';
	import type { EdgeExcessTelemetry } from '$lib/types';

	type ExcessScatterProps = ComponentProps<typeof ExcessScatter>;

	// Shapes mirror the /v1/edge/excess-telemetry payload (see
	// apps/alphalens-django/edge/api/excess_telemetry.py) and the mock in
	// tests/edge-telemetry.test.ts — points + a trailing-mean trend with a CI band.
	const OK: EdgeExcessTelemetry = {
		benchmark: 'SPY',
		status: 'ok',
		gate_threshold: 30,
		n_total: 42,
		n_effective: 17,
		median_holding_days: 8,
		smoother_window: 20,
		metric_note:
			'per-trade total excess over SPY; all surfaced candidates, not a user-selected portfolio; gross / pre-cost; in-sample; telemetry / exploratory only.',
		benchmark_note:
			'SPY is a broad-market proxy and does not reflect the sector/factor exposures of these names.',
		points: [
			{ date: '2026-06-01', excess: -0.021, ticker: 'AAA', holding_days: 5, episode_repeat: false },
			{ date: '2026-06-02', excess: 0.033, ticker: 'BBB', holding_days: 9, episode_repeat: true },
			{ date: '2026-06-03', excess: -0.045, ticker: 'CCC', holding_days: 6, episode_repeat: false },
			{ date: '2026-06-04', excess: 0.012, ticker: 'DDD', holding_days: 11, episode_repeat: false },
			{ date: '2026-06-05', excess: 0.058, ticker: 'EEE', holding_days: 7, episode_repeat: false },
			{ date: '2026-06-08', excess: -0.03, ticker: 'FFF', holding_days: 8, episode_repeat: true },
			{ date: '2026-06-09', excess: 0.021, ticker: 'GGG', holding_days: 10, episode_repeat: false }
		],
		trend: [
			{ date: '2026-06-01', mean: -0.021, lo: -0.05, hi: 0.01 },
			{ date: '2026-06-03', mean: -0.011, lo: -0.038, hi: 0.017 },
			{ date: '2026-06-05', mean: 0.007, lo: -0.02, hi: 0.03 },
			{ date: '2026-06-09', mean: 0.011, lo: -0.012, hi: 0.035 }
		]
	};

	// Below the N-gate: trend (mean + band) withheld, only raw points shown.
	const ACCUMULATING: EdgeExcessTelemetry = {
		...OK,
		status: 'accumulating',
		n_total: 12,
		n_effective: 9,
		points: OK.points.slice(0, 5),
		trend: []
	};

	const { Story } = defineMeta({
		title: 'Data-viz/ExcessScatter',
		component: ExcessScatter,
		tags: ['autodocs'],
		parameters: { layout: 'padded' },
		render: template
	});
</script>

{#snippet template(args: ExcessScatterProps)}
	<div style="width: 46rem; padding: 1rem;">
		<ExcessScatter {...args} />
	</div>
{/snippet}

<!-- Above the gate: scatter + trailing-mean line + CI band, and the legend shows
     all four swatches (dot, mean, band, parity). -->
<Story
	name="Ok With Trend"
	args={{ telemetry: OK }}
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByTestId('excess-scatter-legend')).toBeVisible());
		await expect(canvas.getByText(/one closed trade/)).toBeVisible();
		await expect(canvas.getByText(/trailing mean \(last 20\)/)).toBeVisible();
		await expect(canvas.getByText(/95% confidence band/)).toBeVisible();
		await expect(canvas.getByText(/SPY parity/)).toBeVisible();
	}}
/>

<!-- Below the gate: trend withheld, so the legend drops the mean + band swatches
     and keeps only the dot + parity entries. -->
<Story
	name="Accumulating (gate)"
	args={{ telemetry: ACCUMULATING }}
	play={async ({ canvas }) => {
		await waitFor(() => expect(canvas.getByTestId('excess-scatter-legend')).toBeVisible());
		await expect(canvas.getByText(/one closed trade/)).toBeVisible();
		await expect(canvas.getByText(/SPY parity/)).toBeVisible();
		await expect(canvas.queryByText(/trailing mean/)).toBeNull();
		await expect(canvas.queryByText(/confidence band/)).toBeNull();
	}}
/>
