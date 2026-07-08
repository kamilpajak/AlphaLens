<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import ExpertPanel from './ExpertPanel.svelte';
	import type { ExpertAssessments } from '$lib/types';

	type ExpertPanelProps = ComponentProps<typeof ExpertPanel>;

	// ── Fixture data ──────────────────────────────────────────────────────────
	//
	// BOTH_SCORED: ticker FOUR from tests/fixtures/api-mock/days/2026-05-18.json.
	// buffett_quality_score=62, oneil_score=55, expert_spread=7 → "consensus" band.
	// No qualitative Buffett pillars in the fixture (numeric-only enrich run).
	const BOTH_SCORED: ExpertAssessments = {
		buffett: {
			buffett_quality_score: 62
		},
		oneil: {
			oneil_score: 55,
			oneil_pct_off_52w_high: -59.85394434206203,
			oneil_rs_approx_pct: 24,
			oneil_ma200_slope_pct_per_day: -0.4227497553645656,
			oneil_ma200_distance_pct: -34.14783255940499,
			oneil_earnings_growth_yoy_pct: -3.3
		},
		panel: {
			expert_spread: 7,
			panel_config_version: 'panel-v1-absdiff-2x'
		}
	};

	// BUFFETT_QUAL_ONLY: Buffett qualitative run present (moat/trend/candor/understandable
	// + rationale + scuttlebutt + classification date), no O'Neil assessment.
	// Enum values are the real string enums accepted by moatTone / moatTrendTone /
	// candorTone in src/lib/format.ts; spread is absent (only one lens scored).
	const BUFFETT_QUAL_ONLY: ExpertAssessments = {
		buffett: {
			buffett_quality_score: 74,
			buffett_moat_type: 'switching-cost',
			buffett_moat_trend: 'stable',
			buffett_management_candor: 'candid',
			buffett_understandable: true,
			buffett_qualitative_rationale:
				"The company's deeply embedded ERP platform creates high switching costs for mid-market clients. Management disclosure is direct about competitive pressure and margin trajectory.",
			buffett_used_scuttlebutt: true,
			buffett_qual_computed_at: '2026-05-18T04:31:00Z'
		}
	};

	const { Story } = defineMeta({
		title: 'Composites/ExpertPanel',
		component: ExpertPanel,
		tags: ['autodocs'],
		parameters: { layout: 'padded' }
	});
</script>

<!-- Both lenses scored + finite panel spread: the disagreement scale renders inside the
     opened drawer. Open the drawer in play(), then assert the scale labels are visible. -->
<Story
	name="Both Scored With Spread"
	play={async ({ canvas }) => {
		const trigger = canvas.getByRole('button', { name: /expert\.panel/i });
		trigger.click();
		await waitFor(() =>
			expect(canvas.getByTestId('lens-label-buffett')).toBeVisible()
		);
		await waitFor(() =>
			expect(canvas.getByTestId('lens-label-oneil')).toBeVisible()
		);
		await waitFor(() => expect(canvas.getByText(/consensus/i)).toBeVisible());
	}}
>
	{#snippet template()}
		<div style="width: 24rem; padding: 4rem 6rem;">
			<ExpertPanel assessments={BOTH_SCORED} tenkAvailable={true} />
		</div>
	{/snippet}
</Story>

<!-- Buffett qual run present (pillars + rationale + scuttlebutt), no O'Neil assessment.
     Only the Buffett scorecard renders inside the drawer; no disagreement scale. -->
<Story
	name="Buffett Qual Only"
	play={async ({ canvas }) => {
		const trigger = canvas.getByRole('button', { name: /expert\.panel/i });
		trigger.click();
		await waitFor(() =>
			expect(canvas.getByText(/switching-cost/i)).toBeVisible()
		);
		await waitFor(() =>
			expect(canvas.getByText(/scuttlebutt/i)).toBeVisible()
		);
	}}
>
	{#snippet template()}
		<div style="width: 24rem; padding: 4rem 6rem;">
			<ExpertPanel assessments={BUFFETT_QUAL_ONLY} tenkAvailable={true} />
		</div>
	{/snippet}
</Story>

<!-- Null assessments: the component renders nothing (hasContent is false). -->
<Story
	name="Null Assessments"
	play={async ({ canvas }) => {
		await waitFor(() =>
			expect(canvas.queryByRole('button', { name: /expert\.panel/i })).toBeNull()
		);
	}}
>
	{#snippet template()}
		<div style="width: 24rem; padding: 4rem 6rem;">
			<ExpertPanel assessments={null} />
		</div>
	{/snippet}
</Story>
