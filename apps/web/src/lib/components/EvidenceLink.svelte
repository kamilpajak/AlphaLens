<script lang="ts">
	// The `{path} ↗` evidence-drawer link button, identical in both /experiments
	// ledgers. The parent owns the drawer instance and passes `onopen`, so this
	// stays a dumb button (no drawer dependency).

	interface Props {
		path: string;
		onopen: (path: string) => void;
	}

	let { path, onopen }: Props = $props();
</script>

<button
	type="button"
	class="font-mono text-[11px] text-cyan hover:text-amber underline decoration-dotted underline-offset-2 break-all text-left"
	onclick={() => onopen(path)}
	aria-label="open evidence: {path}"
>{path} ↗</button>
