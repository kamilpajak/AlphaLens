<script lang="ts">
	// Shared square-bullet enumeration for tooltip bodies — the single source for
	// the plain "what goes into this score" lists (valuation composite multiples,
	// catalyst-floor components). Use this for ENUMERATIONS only; key→value or
	// threshold→meaning tables belong in MetricGrid instead.
	// `class` passes through to the <ul> so callers set their own top margin.

	let { items, class: cls = 'mt-1' }: { items: string[]; class?: string } = $props();
</script>

<ul class="list-[square] pl-4 space-y-0.5 marker:text-amber {cls}">
	{#each items as item}
		<li>{item}</li>
	{/each}
</ul>
