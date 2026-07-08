<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import DetailField from './DetailField.svelte';

	type DetailFieldProps = ComponentProps<typeof DetailField>;

	const { Story } = defineMeta({
		title: 'Primitives/DetailField',
		component: DetailField,
		tags: ['autodocs'],
		parameters: { layout: 'centered' }
	});
</script>

<Story
	name="Basic"
	play={async ({ canvas }) => {
		await expect(canvas.getByText('Outcome')).toBeVisible();
		await expect(canvas.getByText('PASS')).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<dl>
				<DetailField label="Outcome">
					{#snippet children()}
						PASS
					{/snippet}
				</DetailField>
			</dl>
		</div>
	{/snippet}
</Story>

<Story
	name="Long Value"
	play={async ({ canvas }) => {
		await expect(canvas.getByText('Strategy')).toBeVisible();
		await expect(
			canvas.getByText('Momentum low-vol screener with vol-target overlay')
		).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<dl>
				<DetailField label="Strategy">
					{#snippet children()}
						Momentum low-vol screener with vol-target overlay
					{/snippet}
				</DetailField>
			</dl>
		</div>
	{/snippet}
</Story>

<Story
	name="With dd Class"
	play={async ({ canvas }) => {
		await expect(canvas.getByText('PRs')).toBeVisible();
		await expect(canvas.getByText('#477 #480 #484')).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<dl>
				<DetailField label="PRs" ddClass="font-mono text-[11px]">
					{#snippet children()}
						#477 #480 #484
					{/snippet}
				</DetailField>
			</dl>
		</div>
	{/snippet}
</Story>
