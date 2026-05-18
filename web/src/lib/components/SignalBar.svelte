<script lang="ts">
	interface Props {
		label: string;
		value: number | null | undefined;
		min?: number;
		max?: number;
		format?: (v: number) => string;
		inverted?: boolean;
		/** Optional descriptive tooltip shown on hover. */
		tooltip?: string;
	}
	let {
		label,
		value,
		min = 0,
		max = 100,
		format = (v) => v.toFixed(1),
		inverted = false,
		tooltip = ''
	}: Props = $props();

	const pct = $derived(
		value === null || value === undefined || !Number.isFinite(value)
			? 0
			: Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100))
	);
	const display = $derived(
		value === null || value === undefined || !Number.isFinite(value) ? '—' : format(value)
	);

	type Tone = 'green' | 'amber' | 'red' | 'muted';
	const tone: Tone = $derived.by(() => {
		if (value === null || value === undefined || !Number.isFinite(value)) return 'muted';
		if (inverted) {
			if (pct > 70) return 'red';
			if (pct > 40) return 'amber';
			return 'green';
		}
		if (pct > 70) return 'green';
		if (pct > 40) return 'amber';
		return 'red';
	});
</script>

<div class="group relative text-[10px] uppercase tracking-widest hover:z-50" class:cursor-help={tooltip}>
	<div class="flex items-center justify-between mb-1.5 gap-2">
		<span class="text-fg-muted truncate">{label}</span>
		<span
			class="font-bold whitespace-nowrap"
			class:text-green={tone === 'green'}
			class:text-amber={tone === 'amber'}
			class:text-red={tone === 'red'}
			class:text-fg-muted={tone === 'muted'}
		>{display}</span>
	</div>
	<div class="h-1.5 bg-bg-3 relative overflow-hidden">
		<div
			class="absolute inset-y-0 left-0"
			class:bg-green={tone === 'green'}
			class:bg-amber={tone === 'amber'}
			class:bg-red={tone === 'red'}
			class:bg-fg-muted={tone === 'muted'}
			style="width: {pct}%"
		></div>
	</div>

	{#if tooltip}
		<span
			class="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-80 z-50 opacity-0 transition-opacity duration-150 group-hover:opacity-100"
			role="tooltip"
		>
			<span class="block border border-amber bg-bg-1 px-3 py-2 text-[11px] leading-snug text-fg-dim normal-case tracking-normal shadow-2xl">
				<span class="block text-amber font-bold uppercase tracking-widest text-[10px] mb-1">
					{label}
				</span>
				<span class="block">{tooltip}</span>
			</span>
			<span class="absolute left-1/2 -translate-x-1/2 top-full w-2 h-2 border-r border-b border-amber bg-bg-1 -mt-1 rotate-45"></span>
		</span>
	{/if}
</div>
