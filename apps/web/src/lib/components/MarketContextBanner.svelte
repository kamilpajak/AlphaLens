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
	// cannot act on. That holds only while a label exists; see the paragraph
	// below for the dates where it does not. The three RETURNS carry none,
	// deliberately: they are raw
	// `close(t)/close(t-5) - 1` with no threshold and no emission rule, which is
	// also why they carry no `config_version`.
	//
	// AND NEITHER DOES A DRIVER WITH NO LABEL BESIDE IT. The flip point
	// annotates the label, so when the label reads `unknown` there is no flip to
	// describe and the value renders alone.
	//
	// ANY SUBSET of the three drivers can accompany an `unknown` label, because
	// `classify_state` in `market_state.py` fills its telemetry dict BEFORE it
	// decides anything and then returns `unknown` from one of two guards. The
	// four reachable shapes, read off that function:
	//
	//   store under 200 sessions (no SMA200)        -> `vix` only
	//   200-251 sessions (no ATR percentile yet)    -> `vs ma200` + `vix`
	//   ATR percentile under 70 and no VIX print    -> `vs ma200` + `atr %ile`
	//   store / FRED read failed                    -> none
	//
	// The third is the one that matters most: the vol axis is an OR of the
	// realized and implied legs, so a low realized leg with no VIX print cannot
	// settle it (#1559), and the strip then shows two real drivers next to
	// `market · unknown`. It used to annotate both with their flip points — the
	// flip points of a label that does not exist.
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
	import { marketStateTone, marketStateLabel, isKnownMarketState } from '$lib/format';
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
	// One source of truth for "is the regime word real?" — the same predicate
	// `marketStateLabel` uses, so the drivers' flip points cannot disagree with
	// the word printed two lines above them.
	const labelKnown = $derived(isKnownMarketState(marketState));

	// MarketStateTone → chip TEXT class, and text only. `red-dim` is not in the
	// shared SemanticTone vocabulary (tone.ts), so map locally — the PillarTone
	// chips do the same. Every value is a literal class string so Tailwind keeps
	// them.
	//
	// Each entry used to carry a `border-*` colour as well, and none of them ever
	// painted: the element that consumes this map has no border-width utility and
	// no padding, so the five colours sat on a zero-width border. They are gone
	// rather than completed, because the caption is deliberately not a pill —
	// see the dot below.
	const TONE_CLASS: Record<string, string> = {
		green: 'text-green',
		amber: 'text-amber',
		red: 'text-red',
		'red-dim': 'text-red-dim',
		muted: 'text-fg-muted'
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
	const faceDrivers = $derived(driverCells({ dist200, atrPctQ, vix }, labelKnown));
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
			<!-- The word, demoted to a caption under the numbers it summarises.
			     A DOT, NOT A PILL, and on purpose. Every other tone-coloured state
			     on this dashboard is a bordered pill (`statusPillClass`,
			     `ExpertPillar`, `GatePill`), and this is the only `size-1.5`
			     status dot in the SPA. The strip is header chrome and the word is
			     a caption under the numbers that carry the information, so it must
			     not compete with the pills inside the candidate cards below it.
			     Reach for `statusPillClass` here only if that ordering changes. -->
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
						<dd class="text-fg">{cell.value}</dd>
					</div>
				{/each}
			</dl>
		{/if}

		<!-- The drivers, each beside the value at which the label would flip. The
		     flip point is part of the cell, so it cannot outlive its value — and no
		     driver carries one while the label is `unknown`, where there is no flip
		     to annotate. -->
		{#if faceDrivers.length > 0}
			<dl
				aria-label="market state drivers"
				class="flex items-baseline gap-x-4 gap-y-1 flex-wrap text-[11px] min-w-0"
			>
				{#each faceDrivers as cell (cell.key)}
					<div class="flex items-baseline gap-1.5 whitespace-nowrap">
						<dt class="text-fg-muted uppercase tracking-widest text-[9px]">{cell.key}</dt>
						<!-- The flip point lives INSIDE the <dd>, not beside it: a <dl>'s
						     <div> group may contain only <dt> and <dd>, and the annotation
						     is part of the description anyway, so it is announced with the
						     value instead of after it.
						     NOTHING HERE CARRIES A FONT CLASS, and that is the rule for the
						     whole strip. `app.html` puts `font-mono` on `<body>` and
						     `app.css` sets `var(--font-mono)` on `html, body`, so JetBrains
						     Mono is simply the page. A `font-mono` on a `<dd>` states a
						     contrast that does not exist — `CandidateCard.svelte` renders
						     every number on this page without one.
						     Written down because the opposite was shipped twice in two
						     days: this span briefly took `font-sans`, the only one in all
						     of `src/`, and since there is no `--font-sans` token it fell
						     back to Tailwind's system stack — the one string on the page in
						     a face nothing else uses. -->
						<dd class="text-fg flex items-baseline gap-1.5">
							{cell.value}
							{#if cell.flip}
								<span class="text-fg-muted text-[9px]">({cell.flip})</span>
							{/if}
						</dd>
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
			<!-- BUILT LIKE THE TWO FACE ROWS ABOVE, not as a grid of spread cells.
			     It used to be `grid grid-cols-2 sm:grid-cols-4` whose every cell was
			     `justify-between`. THAT is what separated each term from its value:
			     `justify-between` pushes the pair to the two ends of whatever width
			     the track hands it, at any cell count — the grid did it on day one
			     with all four cells filled (#758), not only after the drivers were
			     promoted to the face and `residualCells` dropped to at most two.
			     What the leftover columns changed was the SIZE of the gap, and they
			     changed it differently by width: from 640px up, four quarter-width
			     columns with half of them empty measured 218px between term and
			     value; below the `sm` breakpoint the two cells filled a two-column
			     grid and it was 75px. Either way the reader saw
			     "VIX %ILE          76    SQUEEZE          off" — a gap wide enough to
			     read as a column that failed to render rather than as a pair.
			     Now each pair is its own flex group with a fixed `gap-1.5`, and the
			     groups sit on one `gap-x-4` baseline row: the same shape as the index
			     and driver rows, so the drawer and the face read alike. No track
			     width is handed to a pair any more, so no cell count and no viewport
			     can spread one again — that part is measured, by the geometry test.
			     `whitespace-nowrap` sits on the group rather than the `<dd>` so
			     `vix %ile` cannot break across its own space. That one is NOT
			     measured and cannot be at any realistic width: two cells are about
			     160px inside a 302px row at 360px, so nothing needs to break. It is
			     here because the atomic-token rule in `apps/web/CLAUDE.md` is a
			     review-time rule that nothing enforces, and the face rows carry it.
			     The `<dl>` differs from theirs in one class: no `min-w-0`, which is
			     inert while this list is the only child of a block. -->
			<dl
				aria-label="market context residuals"
				class="flex items-baseline gap-x-4 gap-y-1 flex-wrap text-[11px]"
			>
				{#each residual as cell (cell.key)}
					<div class="flex items-baseline gap-1.5 whitespace-nowrap">
						<dt class="text-fg-muted uppercase tracking-widest text-[9px]">{cell.key}</dt>
						<dd class="text-fg">{cell.value}</dd>
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
