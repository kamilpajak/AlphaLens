<script lang="ts">
	// The concluding "so what / threshold" line of a tooltip, set apart from the
	// list above it in a subtly-shaded footer panel (the list is the "what", this
	// is the interpretation). Negative margins cancel the TooltipBubble's inner
	// px-3 py-2 so the panel bleeds flush to the bubble edges + bottom, then a top
	// border + bg-2 fill separate it. Used inside `bodyRich`/`tooltipRich` snippets.
	import type { Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();
</script>

<span class="block mt-2 -mx-3 -mb-2 px-3 py-2 border-t border-grid bg-bg-2 text-fg-dim">
	{@render children()}
</span>
