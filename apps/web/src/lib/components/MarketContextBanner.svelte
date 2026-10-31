<script lang="ts">
	// Brief-level MARKET CONTEXT strip. Rendered ONCE at the top of a day view —
	// everything here is INDEX-LEVEL and identical for every candidate on the
	// date, so +page reads it from the first candidate. It is DISPLAY-ONLY /
	// UNVALIDATED context that NEVER feeds candidate selection or the brief sort
	// (buffett/oneil posture). The tone is a colour, not a buy/avoid signal.
	//
	// WHY THE FACE CARRIES NUMBERS AND THE WORD IS A CAPTION
	//
	// The four-state word watches SPY alone, so it read `bull_quiet` through 15
	// sessions where SPY fell 1.08% and the Nasdaq drew down 6.2%. The label was
	// not wrong; it is blind to breadth by construction. It also hid its own
	// drivers: they used to live behind a collapsed `axes` toggle with no
	// persisted state, so `atr_pct_q` could go 0.040 -> 0.317 and never once be
	// on screen. So the face now carries the three broad-index 5-session returns
	// plus the three drivers, and the word is a caption under them.
	//
	// Each DRIVER is annotated with its flip point — the value at which the
	// label would change — because a driver without one is a number the reader
	// cannot act on. The three RETURNS carry none, deliberately: they are raw
	// `close(t)/close(t-5) - 1` with no threshold and no emission rule, which is
	// also why they carry no `config_version`.
	//
	// `unknown` is a first-class state with THREE causes: the date predates the
	// signal, the classifier could not decide it, or a store / FRED read failed.
	// The strip always renders, so the label stays honest and the glossary
	// reference is always reachable.
	//
	// AN ABSENT CELL DISAPPEARS — it is not an em dash
	//
	// The two column families go absent independently: the returns reach back to
	// 2024-09-11, the `market_state` block ships only from 2026-07-05. So the
	// dominant historical shape is returns WITHOUT a label — 47 of 164 brief
	// dates, the window 2026-05-19 … 2026-07-04, measured on production Postgres
	// 2026-10-30. On that shape the old strip printed five em dashes around the
	// three real numbers, and worse, printed each driver's flip point beside
	// nothing ("— (volatile at 70)"). Now a cell that has no value is not
	// rendered at all: its `dt`, its value and its flip point go together. The
	// rule lives in `marketContextCells.ts`, which decides presence from the
	// INPUT (`Number.isFinite`, or `=== true` / `=== false` for the squeeze) and
	// never from the formatted string.
	//
	// EVERYTHING ABSENT — a date before BOTH families, e.g. 2026-04-14 — still
	// renders the strip, and renders exactly three things: the "market context"
	// micro-label with its glossary tip, the muted dot plus `market · unknown`,
	// and the unvalidated caption. No index row, no driver row, no toggle, no em
	// dash: about two lines tall. That is the honest reading of the rule —
	// absence shows as absence, and what remains is a real answer ("no regime
	// word for this date") plus the way to find out what the strip means. It is
	// the state a future reader is most likely to mistake for a bug, so it is
	// written down here.
	//
	// THE CAPTION IS NOT IN THE DRAWER. It used to be, and that collided with
	// hiding the toggle: the drawer holds only two cells, so on a 29% date the
	// toggle must not be offered, which would have hidden the one sentence that
	// stops a reader treating the strip as a signal. It is now the last line of
	// the section and renders on every date — strictly better anyway, since a
	// reader who never opened "more" never saw it.
	import { marketStateTone, marketStateLabel } from '$lib/format';
	import { indexCells, driverCells, residualCells } from './marketContextCells';
	import { GLOSSARY_BY_TERM } from '$lib/data/glossary';
	import JargonTip from './JargonTip.svelte';
	import { ChevronDown } from 'lucide-svelte';

	interface Props {
		marketState: string | null | undefined;
		atrPctQ?: number | null;
		dist200?: number | null;
		vix?: number | null;
		vixDecile?: number | null;
		squeezeOn?: boolean | null;
		/** The three raw broad-index 5-session returns, as FRACTIONS
		 *  (`-0.038` renders as `-3.80%`). Display data, no version key. */
		ret5Spy?: number | null;
		ret5Qqq?: number | null;
		ret5Iwm?: number | null;
	}
	let {
		marketState,
		atrPctQ = null,
		dist200 = null,
		vix = null,
		vixDecile = null,
		squeezeOn = null,
		ret5Spy = null,
		ret5Qqq = null,
		ret5Iwm = null
	}: Props = $props();

	const label = $derived(marketStateLabel(marketState));
	const tone = $derived(marketStateTone(marketState));

	// MarketStateTone → chip text class. `red-dim` is not in the shared
	// SemanticTone vocabulary (tone.ts), so map locally — the PillarTone chips do
	// the same. Every value is a literal class string so Tailwind keeps them.
	const TONE_CLASS: Record<string, string> = {
		green: 'text-green border-green',
		amber: 'text-amber border-amber',
		red: 'text-red border-red',
		'red-dim': 'text-red-dim border-red-dim',
		muted: 'text-fg-muted border-grid-strong'
	};

	// Same shared-glossary lookup as the CandidateCard / +page tipProps helper.
	function tipProps(term: string) {
		const g = GLOSSARY_BY_TERM.get(term);
		return { term: g?.term ?? term, full: g?.full ?? '', body: g?.body ?? '', bands: g?.bands };
	}

	// Only the cells whose value resolved. An empty list means the whole row is
	// not rendered: an `aria-label`'d <dl> with no children is announced as an
	// empty labelled list, which is a worse artifact than the em dash it replaced.
	const faceIndex = $derived(indexCells({ ret5Spy, ret5Qqq, ret5Iwm }));
	const faceDrivers = $derived(driverCells({ dist200, atrPctQ, vix }));
	const residual = $derived(residualCells({ vixDecile, squeezeOn }));

	let requested = $state(false);
	// The toggle is offered only while the drawer has something to show, so
	// `open` must not survive the residual set emptying under the same mounted
	// component — otherwise `aria-expanded` and `aria-controls` would point at a
	// drawer that is not there.
	const open = $derived(requested && residual.length > 0);

	// ...and the REQUEST itself must not survive it either. The strip is not
	// remounted when the reader steps from one brief date to the next (same
	// route, new param), so without this: open the drawer on a 2026-09-05-shaped
	// date, step back to a pre-signal date where the button unmounts, step
	// forward again — and the drawer is already open although nobody asked for
	// it in that view. Resetting on the empty set is enough; the request is only
	// ever honoured while there is something to reveal.
	$effect(() => {
		if (residual.length === 0) requested = false;
	});
</script>

<!-- Lives INSIDE the brief header container as a footer strip — a twin of the
     "top catalyst" strip (same border-grid divider + micro-label typography), so
     the index-level context reads as native header chrome, not a floating banner. -->
<section aria-label="market context" class="border-t border-grid px-4 sm:px-6 py-3">
	<!-- LEFT-ALIGNED, not space-between. With rows that disappear, the number of
	     children on this row is 2, 3 or 4 depending on the date, and
	     `justify-between` spread whatever survived to the outer edges — one
	     surviving cell ended up roughly 940px away from the label on a wide
	     viewport, which reads as a rendering fault rather than as a group. A
	     fixed `gap-x-6` keeps every count reading the same, and only the toggle
	     is pushed to the right edge, with `ml-auto` on the button itself. -->
	<div class="flex items-start gap-x-6 gap-y-2 flex-wrap">
		<div class="flex flex-col gap-1.5 min-w-0">
			<JargonTip {...tipProps('market context')} placement="below">
				<span class="text-[10px] uppercase tracking-widest text-fg-muted">market context</span>
			</JargonTip>
			<!-- The word, demoted to a caption under the numbers it summarises. -->
			<span
				class="inline-flex items-center gap-1.5 text-[10px] uppercase tracking-widest whitespace-nowrap {TONE_CLASS[
					tone
				]}"
			>
				<span class="size-1.5 rounded-full bg-current" aria-hidden="true"></span>
				market · {label}
			</span>
		</div>

		<!-- 5-session index returns: the breadth the one word cannot carry. Each
		     value is `whitespace-nowrap` because a signed percentage breaks after
		     the hyphen-minus and `-3.80%` would read as a positive 3.80% on the
		     next line. -->
		{#if faceIndex.length > 0}
			<dl
				aria-label="index 5-session returns"
				class="flex items-baseline gap-x-4 gap-y-1 flex-wrap text-[11px] min-w-0"
			>
				{#each faceIndex as cell (cell.key)}
					<div class="flex items-baseline gap-1.5 whitespace-nowrap">
						<dt class="text-fg-muted uppercase tracking-widest text-[9px]">{cell.key} 5d</dt>
						<dd class="font-mono text-fg">{cell.value}</dd>
					</div>
				{/each}
			</dl>
		{/if}

		<!-- The drivers, each beside the value at which the label would flip. The
		     flip point is part of the cell, so it cannot outlive its value. -->
		{#if faceDrivers.length > 0}
			<dl
				aria-label="market state drivers"
				class="flex items-baseline gap-x-4 gap-y-1 flex-wrap text-[11px] min-w-0"
			>
				{#each faceDrivers as cell (cell.key)}
					<div class="flex items-baseline gap-1.5 whitespace-nowrap">
						<dt class="text-fg-muted uppercase tracking-widest text-[9px]">{cell.key}</dt>
						<dd class="font-mono text-fg">{cell.value}</dd>
						<span class="text-fg-muted text-[9px]">({cell.flip})</span>
					</div>
				{/each}
			</dl>
		{/if}

		<!-- The drawer is removed from the DOM while closed rather than hidden, so
		     `aria-controls` is bound only while the drawer exists — a constant one
		     would name an id that is not in the document. -->
		{#if residual.length > 0}
			<button
				type="button"
				onclick={() => (requested = !requested)}
				aria-expanded={open}
				aria-controls={open ? 'market-context-axes' : undefined}
				class="ml-auto inline-flex items-center gap-1 text-[10px] uppercase tracking-widest text-fg-muted hover:text-fg transition-colors shrink-0 whitespace-nowrap"
			>
				{open ? 'hide' : 'more'}
				<ChevronDown class="size-3 transition-transform {open ? 'rotate-180' : ''}" />
			</button>
		{/if}
	</div>

	{#if open}
		<!-- Only what is NOT on the face. The promoted drivers are deliberately
		     not repeated here: two copies of one number twenty lines apart is a
		     silent doubling the moment one of them is edited. -->
		<div id="market-context-axes" class="mt-3 border-t border-grid pt-3">
			<dl class="grid grid-cols-2 sm:grid-cols-4 gap-x-6 gap-y-1.5 text-[11px]">
				{#each residual as cell (cell.key)}
					<div class="flex items-center justify-between gap-2">
						<dt class="text-fg-muted uppercase tracking-widest text-[9px]">{cell.key}</dt>
						<dd class="font-mono text-fg whitespace-nowrap">{cell.value}</dd>
					</div>
				{/each}
			</dl>
		</div>
	{/if}

	<!-- Always rendered, on every date: the sentence that stops the strip reading
	     as a signal must never be behind a toggle that may not exist. The clause
	     about the three returns reads correctly even on a date with no returns,
	     so the wording stays verbatim rather than splitting into per-state
	     variants nobody pins. -->
	<p class="mt-2 text-[10px] italic text-fg-muted">
		label unvalidated · context-only · the three index returns are raw 5-session percent changes,
		not part of the label
	</p>
</section>
