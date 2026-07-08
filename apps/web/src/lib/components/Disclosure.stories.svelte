<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import Disclosure from './Disclosure.svelte';

	const { Story } = defineMeta({
		title: 'Primitives/Disclosure',
		component: Disclosure,
		tags: ['autodocs'],
		parameters: { layout: 'centered' }
	});
</script>

<!-- Closed: the native <details> element has no `open` attribute, so the
     body content is hidden by the browser. Assert the summary label is
     visible and the body text is not. -->
<Story
	name="Closed"
	play={async ({ canvas }) => {
		await expect(canvas.getByText('Show details')).toBeVisible();
		await waitFor(() => expect(canvas.getByText('Hidden body content.')).not.toBeVisible());
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<Disclosure>
				{#snippet summary(open)}
					<span class="ml-2 text-sm font-medium">{open ? 'Hide details' : 'Show details'}</span>
				{/snippet}
				{#snippet children()}
					<p class="mt-2 text-sm text-fg-muted">Hidden body content.</p>
				{/snippet}
			</Disclosure>
		</div>
	{/snippet}
</Story>

<!-- Open: pass `open={true}` so the <details> element renders with the open
     attribute. The body content must be visible in the DOM. -->
<Story
	name="Open"
	play={async ({ canvas }) => {
		await expect(canvas.getByText('Hide details')).toBeVisible();
		await expect(canvas.getByText('Visible body content.')).toBeVisible();
	}}
>
	{#snippet template()}
		<div style="padding: 4rem 6rem;">
			<Disclosure open={true}>
				{#snippet summary(open)}
					<span class="ml-2 text-sm font-medium">{open ? 'Hide details' : 'Show details'}</span>
				{/snippet}
				{#snippet children()}
					<p class="mt-2 text-sm text-fg-muted">Visible body content.</p>
				{/snippet}
			</Disclosure>
		</div>
	{/snippet}
</Story>
