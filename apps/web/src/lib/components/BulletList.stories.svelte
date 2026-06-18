<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import BulletList from './BulletList.svelte';

	type BulletListProps = ComponentProps<typeof BulletList>;

	const { Story } = defineMeta({
		title: 'Tooltips/Parts/BulletList',
		component: BulletList,
		tags: ['autodocs'],
		parameters: { layout: 'centered' },
		render: template
	});
</script>

{#snippet template(args: BulletListProps)}
	<div style="padding: 6rem 8rem 3rem;">
		<div style="max-width: 22rem;">
			<p style="margin-bottom: 0.5rem;">Valuation multiples in the composite:</p>
			<BulletList {...args} />
		</div>
	</div>
{/snippet}

<Story
	name="Valuation multiples"
	args={{
		items: ['PE', 'PS', 'EV / Revenue', 'EV / EBITDA', 'FCF margin']
	}}
/>
