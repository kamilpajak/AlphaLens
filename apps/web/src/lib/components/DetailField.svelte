<script lang="ts">
	// One `<dt>/<dd>` row of a ledger detail list — the
	// `<div class="flex gap-2"><dt class="text-cyan font-bold w-20 sm:w-24 shrink-0">
	// Label</dt><dd>…</dd></div>` shell repeated ~11× across the /experiments
	// paradigm + tool detail <dl>s. The value is a snippet (parseMarkup output,
	// plain text, an EvidenceLink, …); `ddClass` carries the per-field <dd> tweak
	// (e.g. `text-fg` for Outcome, `font-mono text-[11px] text-fg-muted` for PRs).

	import type { Snippet } from 'svelte';

	interface Props {
		label: string;
		/** Extra classes for the <dd>. */
		ddClass?: string;
		children: Snippet;
	}

	let { label, ddClass = '', children }: Props = $props();
</script>

<div class="flex gap-2">
	<dt class="text-cyan font-bold w-20 sm:w-24 shrink-0">{label}</dt>
	<dd class={ddClass}>{@render children()}</dd>
</div>
