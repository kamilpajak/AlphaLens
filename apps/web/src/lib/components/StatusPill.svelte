<script lang="ts">
	// Shared bordered status/verdict pill — the `px-1.5 py-0.5 border … uppercase
	// tracking-widest {tone}` span that was hand-rolled at ~9 sites. The `tone`
	// (a precomputed "text-X border-X" class string) comes from the domain
	// (statusTone / toolStatusTone / toneClasses / a literal); this component owns
	// only the shell + variant flags. Extra DOM attributes (data-testid, aria-*,
	// data-*) pass through via `...rest`. Class math lives in `statusPill.ts`.

	import { statusPillClass, type StatusPillOpts } from './statusPill';

	interface Props extends StatusPillOpts {
		label: string;
		/** Extra classes (maps to StatusPillOpts.extra). */
		class?: string;
		[key: string]: unknown;
	}

	let {
		tone,
		label,
		size = '10',
		nowrap = false,
		dashed = false,
		interactive = false,
		class: extra = '',
		...rest
	}: Props = $props();

	const cls = $derived(statusPillClass({ tone, size, nowrap, dashed, interactive, extra }));
</script>

<span {...rest} class={cls}>{label}</span>
