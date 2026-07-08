<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, userEvent, waitFor, within } from 'storybook/test';
	import LedgerRow from './LedgerRow.svelte';
	import StatusPill from './StatusPill.svelte';
	import DetailField from './DetailField.svelte';
	import { toneClass } from '$lib/tone';

	type LedgerRowProps = ComponentProps<typeof LedgerRow>;

	const { Story } = defineMeta({
		title: 'Primitives/LedgerRow',
		component: LedgerRow,
		tags: ['autodocs'],
		parameters: { layout: 'padded' }
	});
</script>

<!-- TP_FULL outcome: AMPL, brief_date 2026-05-18, realized_r 1.85 (fixture row 0).
     Collapsed — verifies display ticker, theme name, and date are rendered. -->
<Story
	name="Collapsed TP Full"
	play={async ({ canvas }) => {
		await expect(canvas.getByText('AMPL')).toBeVisible();
		await expect(canvas.getByText('high-gas')).toBeVisible();
		await expect(canvas.getByText('2026-05-18')).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; max-width: 48rem;">
			<LedgerRow
				id="ampl-tp-full"
				display="AMPL"
				name="high-gas"
				date="2026-05-18"
				detailNoun="case detail"
			>
				{#snippet status()}
					<StatusPill tone={toneClass('green')} label="TP FULL" />
				{/snippet}
				{#snippet preface()}
					<p class="text-xs text-fg-muted mb-2">
						Ladder completed at full target — realized R: 1.85
					</p>
				{/snippet}
				{#snippet fields()}
					<DetailField label="Outcome">
						{#snippet children()}
							TP_FULL
						{/snippet}
					</DetailField>
					<DetailField label="Holding">
						{#snippet children()}
							11 days
						{/snippet}
					</DetailField>
				{/snippet}
			</LedgerRow>
		</div>
	{/snippet}
</Story>

<!-- SL_HIT outcome: RGTI, brief_date 2026-05-18, realized_r -1.0 (fixture row 1).
     Collapsed — verifies the stop-loss pill and ticker name. -->
<Story
	name="Collapsed SL Hit"
	play={async ({ canvas }) => {
		await expect(canvas.getByText('RGTI')).toBeVisible();
		await expect(canvas.getByText('quantum_computing')).toBeVisible();
		await expect(canvas.getByText('SL HIT')).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; max-width: 48rem;">
			<LedgerRow
				id="rgti-sl-hit"
				display="RGTI"
				name="quantum_computing"
				date="2026-05-18"
				detailNoun="case detail"
			>
				{#snippet status()}
					<StatusPill tone={toneClass('red')} label="SL HIT" />
				{/snippet}
				{#snippet preface()}
					<p class="text-xs text-fg-muted mb-2">
						Stop-loss triggered — realized R: -1.0
					</p>
				{/snippet}
				{#snippet fields()}
					<DetailField label="Outcome">
						{#snippet children()}
							SL_HIT
						{/snippet}
					</DetailField>
					<DetailField label="Holding">
						{#snippet children()}
							7 days
						{/snippet}
					</DetailField>
				{/snippet}
			</LedgerRow>
		</div>
	{/snippet}
</Story>

<!-- AMPL TP_FULL with detail panel opened — verifies that clicking "show case
     detail" exposes the DetailField rows inside the Disclosure. -->
<Story
	name="Opened Detail Panel"
	play={async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		const toggle = canvas.getByText(/show case detail/i);
		await userEvent.click(toggle);
		await expect(canvas.getByText('TP_FULL')).toBeVisible();
		await expect(canvas.getByText('11 days')).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; max-width: 48rem;">
			<LedgerRow
				id="ampl-tp-full-open"
				display="AMPL"
				name="high-gas"
				date="2026-05-18"
				detailNoun="case detail"
			>
				{#snippet status()}
					<StatusPill tone={toneClass('green')} label="TP FULL" />
				{/snippet}
				{#snippet preface()}
					<p class="text-xs text-fg-muted mb-2">
						Ladder completed at full target — realized R: 1.85
					</p>
				{/snippet}
				{#snippet fields()}
					<DetailField label="Outcome">
						{#snippet children()}
							TP_FULL
						{/snippet}
					</DetailField>
					<DetailField label="Holding">
						{#snippet children()}
							11 days
						{/snippet}
					</DetailField>
				{/snippet}
			</LedgerRow>
		</div>
	{/snippet}
</Story>

<!-- Optional tags snippet: IONQ TIME_STOP with axis/layer tags rendered in the
     header. Verifies that the tags snippet content appears alongside the name. -->
<Story
	name="With Tags"
	play={async ({ canvas }) => {
		await expect(canvas.getByText('IONQ')).toBeVisible();
		await expect(canvas.getByText('quantum_computing')).toBeVisible();
		await expect(canvas.getByText('TIME STOP')).toBeVisible();
		await expect(canvas.getByText('momentum')).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem; max-width: 48rem;">
			<LedgerRow
				id="ionq-time-stop"
				display="IONQ"
				name="quantum_computing"
				date="2026-04-14"
				detailNoun="detail"
			>
				{#snippet status()}
					<StatusPill tone={toneClass('muted')} label="TIME STOP" />
				{/snippet}
				{#snippet tags()}
					<span class="text-[10px] uppercase tracking-widest text-fg-muted border border-grid px-1.5 py-0.5">
						momentum
					</span>
				{/snippet}
				{#snippet preface()}
					<p class="text-xs text-fg-muted mb-2">
						Time-stop exit after 42 days — realized R: 0.12
					</p>
				{/snippet}
				{#snippet fields()}
					<DetailField label="Outcome">
						{#snippet children()}
							TIME_STOP
						{/snippet}
					</DetailField>
					<DetailField label="Holding">
						{#snippet children()}
							42 days
						{/snippet}
					</DetailField>
				{/snippet}
			</LedgerRow>
		</div>
	{/snippet}
</Story>
