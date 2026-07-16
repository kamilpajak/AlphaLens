<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, userEvent, waitFor } from 'storybook/test';
	import WhatIfPanel from './WhatIfPanel.svelte';
	import type { EdgeSummary } from '$lib/types';

	type WhatIfPanelProps = ComponentProps<typeof WhatIfPanel>;

	// Base fixture — values taken verbatim from
	// tests/fixtures/api-mock/edge-summary.json.
	// whatifEarnsDisplay requires >= 2 lenses with n > 0, so we add the other
	// registry lenses (fill_anchored_0p5atr + the pre-registered be_0p5r_trail0p6)
	// alongside the canonical be_0p5r entry. All be_0p5r numeric values (n=112,
	// mean_r=0.069, median_r=0.044, realized_r_baseline=-0.22,
	// realized_r_baseline_n=110, n_helped=74, n_harmed=9) come directly from
	// the fixture file.
	const baseSummary: EdgeSummary = {
		n_brief: 168,
		n_plannable: 132,
		n_terminal: 121,
		n_matured: 118,
		n_gate_threshold: 30,
		benchmark: 'SPY',
		metric_note: 'excess-of-benchmark, gross of cost, mechanical ladder',
		edge: {
			status: 'ok',
			n_matured: 118,
			threshold: 30,
			market_excess_mean: 0.27,
			market_excess_median: 0.18,
			market_excess_quantiles: { p10: -1.12, p50: 0.18, p90: 1.94 },
			hit_rate: null,
			gross_realized_r_mean: 0.41,
			gross_realized_r_median: 0.3,
			gross_realized_r_n: 118,
			holding_days_n: 118,
			holding_days_p50: 11,
			holding_days_p95: 38,
			gross_of_cost: true,
			regime_stratified: false
		},
		portfolio: {
			status: 'ok',
			n_matured: 118,
			threshold: 30,
			mean_realized_risk_pct: 0.94,
			mean_tiers_filled_count: 1.6,
			gross_of_cost: true
		},
		whatif: {
			status: 'ok',
			n_matured: 118,
			threshold: 30,
			in_sample: true,
			note: 'counterfactual: realized R recomputed under an alternative exit-stop on the SAME picks + price paths; in-sample (tuned on this sample) and NOT validated — never the realized result.',
			lenses: {
				be_0p5r: {
					n: 112,
					mean_r: 0.069,
					median_r: 0.044,
					realized_r_baseline: -0.22,
					realized_r_baseline_n: 110,
					n_helped: 74,
					n_harmed: 9,
					preregistered_ref: null
				},
				fill_anchored_0p5atr: {
					n: 89,
					mean_r: 0.031,
					median_r: 0.018,
					realized_r_baseline: -0.22,
					realized_r_baseline_n: 87,
					n_helped: 51,
					n_harmed: 22,
					preregistered_ref: null
				},
				be_0p5r_trail0p6: {
					n: 41,
					mean_r: 0.104,
					median_r: 0.06,
					realized_r_baseline: -0.22,
					realized_r_baseline_n: 40,
					n_helped: 28,
					n_harmed: 6,
					preregistered_ref: 'exit_geometry_2026_06_30 s7 be0.5/trail0.6'
				}
			}
		},
		deployment: {
			n_terminal: 121,
			n_filled: 89,
			n_no_fill: 32,
			fill_rate: 0.74,
			no_fill_rate: 0.26,
			mean_tiers_filled_count: 1.4
		},
		open_positions: {
			n_open: 99,
			near_tp: 22,
			near_sl: 14,
			note: 'descriptive only; open positions are never reduced to a mean R'
		}
	};

	// Insufficient fixture — n_matured=5 is below the threshold of 30 so the panel
	// renders with the N-gate "insufficient" message instead of the lens stats.
	// Two lenses (both n > 0) are still present so whatifEarnsDisplay passes.
	const insufficientSummary: EdgeSummary = {
		...baseSummary,
		whatif: {
			status: 'insufficient',
			n_matured: 5,
			threshold: 30,
			in_sample: true,
			note: baseSummary.whatif.note,
			lenses: {
				be_0p5r: {
					n: 5,
					mean_r: null,
					median_r: null,
					realized_r_baseline: null,
					realized_r_baseline_n: 4,
					n_helped: null,
					n_harmed: null,
					preregistered_ref: null
				},
				fill_anchored_0p5atr: {
					n: 3,
					mean_r: null,
					median_r: null,
					realized_r_baseline: null,
					realized_r_baseline_n: 2,
					n_helped: null,
					n_harmed: null,
					preregistered_ref: null
				}
			}
		}
	};

	const { Story } = defineMeta({
		title: 'Composites/WhatIfPanel',
		component: WhatIfPanel,
		tags: ['autodocs'],
		parameters: { layout: 'padded' }
	});
</script>

<!-- Story 1: Unlocked state — opens the <details>, selects a lens button, then
     asserts the what-if banner and the mean R / n stats are visible. -->
<Story
	name="Unlocked"
	play={async ({ canvas }) => {
		// Open the collapsed <details> element.
		const summaryEl = canvas.getByText(/what-if · experimental/i);
		await userEvent.click(summaryEl);

		// The persistent epistemic banner must be present.
		await waitFor(() =>
			expect(canvas.getByTestId('whatif-banner')).toBeVisible()
		);

		// Click the break-even lens button to make it the active selection.
		const lensButton = canvas.getByRole('button', { name: /break-even/i });
		await userEvent.click(lensButton);

		// Mean R derived from fixture: be_0p5r.mean_r = 0.069 → fmtR → "+0.07R"
		await waitFor(() =>
			expect(canvas.getByText('+0.07R')).toBeVisible()
		);

		// n value from fixture: be_0p5r.n = 112
		await waitFor(() =>
			expect(canvas.getByText(/n 112/)).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; max-width: 40rem;">
			<WhatIfPanel summary={baseSummary} />
		</div>
	{/snippet}
</Story>

<!-- Story 2: Insufficient state — n_matured=5 < threshold=30, so the panel
     renders the N-gate "insufficient" message instead of lens stats. -->
<Story
	name="Insufficient"
	play={async ({ canvas }) => {
		// Open the collapsed <details> element.
		const summaryEl = canvas.getByText(/what-if · experimental/i);
		await userEvent.click(summaryEl);

		// The insufficient gate message must be visible with the actual n_matured.
		await waitFor(() =>
			expect(canvas.getByTestId('whatif-gated')).toBeVisible()
		);

		// Confirm the n_matured value (5) and threshold (30) are surfaced.
		await waitFor(() =>
			expect(canvas.getByText(/n matured = 5/i)).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; max-width: 40rem;">
			<WhatIfPanel summary={insufficientSummary} />
		</div>
	{/snippet}
</Story>
