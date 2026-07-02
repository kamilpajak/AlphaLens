<script lang="ts">
	// Sticky multi-select filter bar shared by both /experiments ledgers (the
	// paradigm ledger and the tool.experiments ledger). It doubles as the visible
	// status legend: each chip is a toggle carrying a ChipTip definition, several
	// can be active at once, and a row shows if its status is in the selection
	// (empty selection = ALL). Owns the toggle / clear / blur-on-click behaviour;
	// the parent owns `selected` (bindable) and does the row filtering.
	//
	// The ALL chip (key === allKey) clears the selection; every other chip toggles
	// its key in/out. `selected` holds only the non-ALL keys — empty means ALL.

	import ChipTip from './ChipTip.svelte';

	export type FilterChip = {
		/** Status key ('ALL' for the clear-all chip, else the status string). */
		key: string;
		label: string;
		count: number;
		/** Tailwind "text-X border-X" tone for the chip. */
		tone: string;
		/** Plain-text definition shown in the chip's ChipTip. */
		def: string;
	};

	interface Props {
		/** Micro-label to the left of the chips (e.g. "filter"). */
		label?: string;
		/** The chips; include the ALL chip (key === allKey) first. */
		chips: FilterChip[];
		/** The key that clears the selection. */
		allKey?: string;
		/** The selected non-ALL keys; empty = ALL. Bindable. Both sides must
		 *  REASSIGN this (e.g. `selected = new Set(...)`), never mutate the Set in
		 *  place — Svelte 5 reactivity tracks the assignment, not `.add`/`.delete`. */
		selected: Set<string>;
	}

	let { label = 'filter', chips, allKey = 'ALL', selected = $bindable() }: Props = $props();

	function isActive(key: string): boolean {
		return key === allKey ? selected.size === 0 : selected.has(key);
	}

	// Toggle a status in/out of the selection (the ALL key clears it), then blur
	// BOTH the button (a click focuses it) and the ChipTip wrapper (its
	// onpointerdown focuses it) so the clicked chip's tooltip doesn't stay pinned
	// via focus-within while the next chip is hovered (two tooltips at once).
	function toggle(key: string, e: Event) {
		if (key === allKey) {
			selected = new Set();
		} else {
			const next = new Set(selected);
			if (next.has(key)) next.delete(key);
			else next.add(key);
			selected = next;
		}
		const btn = e.currentTarget as HTMLElement;
		btn.blur();
		const wrap = btn.closest('[data-testid="chip-tip"]');
		if (wrap instanceof HTMLElement) wrap.blur();
	}

	function clear() {
		selected = new Set();
	}
</script>

<div class="sticky top-0 z-20 flex flex-wrap items-center gap-2 border-b border-grid bg-bg-1/95 px-4 sm:px-5 py-2.5 backdrop-blur">
	<span class="text-[10px] uppercase tracking-widest text-fg-muted mr-0.5">{label}</span>
	{#each chips as fc}
		{@const active = isActive(fc.key)}
		<ChipTip term={fc.key} body={fc.def}>
			{#snippet chip()}
				<button
					type="button"
					onclick={(e) => toggle(fc.key, e)}
					aria-pressed={active}
					class="border px-2 py-0.5 text-[10px] uppercase tracking-widest transition-colors {fc.tone} {active
						? 'bg-bg-3 ring-1 ring-inset ring-current'
						: 'opacity-70 hover:opacity-100'}"
				>{fc.label} <span class="font-mono">{fc.count}</span></button>
			{/snippet}
		</ChipTip>
	{/each}
	{#if selected.size > 0}
		<button
			type="button"
			onclick={clear}
			class="text-[10px] uppercase tracking-widest text-fg-muted hover:text-amber ml-auto">clear ✕</button>
	{/if}
</div>
