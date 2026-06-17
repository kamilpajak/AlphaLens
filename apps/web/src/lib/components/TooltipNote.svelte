<script lang="ts">
	// The concluding "so what / threshold" line of a tooltip, set apart from the
	// list above it in a subtly-shaded footer panel (the list is the "what", this
	// is the interpretation). It bleeds flush to the bubble edges + bottom by
	// negating the TooltipBubble inner padding — sourced from the shared
	// --tt-pad-x / --tt-pad-y custom properties (app.css) so the two never drift.
	// A top border + bg-2 fill separate it. Used inside bodyRich/tooltipRich.
	import type { Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();
</script>

<span
	class="block mt-2 -mx-[var(--tt-pad-x)] -mb-[var(--tt-pad-y)] px-[var(--tt-pad-x)] py-[var(--tt-pad-y)] border-t border-grid bg-bg-2 text-fg-dim"
>
	{@render children()}
</span>
