<script lang="ts">
	// The concluding "so what / threshold" line of a tooltip — a quiet muted
	// footnote under the list (the list is the "what", this is the interpretation),
	// separated only by a small gap. Used inside bodyRich/tooltipRich snippets.
	import type { Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();
</script>

<span class="block mt-2 text-fg-muted">
	{@render children()}
</span>
