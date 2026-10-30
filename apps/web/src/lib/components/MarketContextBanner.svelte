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
	import {
		marketStateTone,
		marketStateLabel,
		fmtNum,
		fmtFracSignedPct,
		MARKET_STATE_ATR_HIGH_PCTILE,
		MARKET_STATE_VIX_HIGH,
		MARKET_STATE_TREND_FLIP_PCT
	} from '$lib/format';
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

	let open = $state(false);

	// Same shared-glossary lookup as the CandidateCard / +page tipProps helper.
	function tipProps(term: string) {
		const g = GLOSSARY_BY_TERM.get(term);
		return { term: g?.term ?? term, full: g?.full ?? '', body: g?.body ?? '', bands: g?.bands };
	}

	// PR-8b guard pattern: every numeric read is Number.isFinite-gated before it
	// is shown. `atr_pct_q` / `vix_decile` are 0-1 quantile ranks (× 100 for the
	// %ile); `dist200` and the three returns are fractions, so they go through
	// the ONE shared fraction→percent helper rather than a local ×100.
	const pctile01 = (v: number | null | undefined): string =>
		Number.isFinite(v) ? `${Math.round((v as number) * 100)}` : '—';
	const squeezeLabel = $derived(squeezeOn === true ? 'on' : squeezeOn === false ? 'off' : '—');

	// One row per face cell: a short key, the value, and the flip point (null for
	// the three returns, which have no threshold to flip).
	const indexCells = $derived([
		{ key: 'spy', value: fmtFracSignedPct(ret5Spy) },
		{ key: 'qqq', value: fmtFracSignedPct(ret5Qqq) },
		{ key: 'iwm', value: fmtFracSignedPct(ret5Iwm) }
	]);
	const driverCells = $derived([
		{
			key: 'vs ma200',
			value: fmtFracSignedPct(dist200, 1),
			flip: `bear below ${MARKET_STATE_TREND_FLIP_PCT}%`
		},
		{
			key: 'atr %ile',
			value: pctile01(atrPctQ),
			flip: `volatile at ${MARKET_STATE_ATR_HIGH_PCTILE}`
		},
		{ key: 'vix', value: fmtNum(vix, 1), flip: `volatile at ${MARKET_STATE_VIX_HIGH}` }
	]);
</script>

<!-- Lives INSIDE the brief header container as a footer strip — a twin of the
     "top catalyst" strip (same border-grid divider + micro-label typography), so
     the index-level context reads as native header chrome, not a floating banner. -->
<section aria-label="market context" class="border-t border-grid px-4 sm:px-6 py-3">
	<div class="flex items-start justify-between gap-x-4 gap-y-2 flex-wrap">
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
		<dl
			aria-label="index 5-session returns"
			class="flex items-baseline gap-x-4 gap-y-1 flex-wrap text-[11px] min-w-0"
		>
			{#each indexCells as cell (cell.key)}
				<div class="flex items-baseline gap-1.5 whitespace-nowrap">
					<dt class="text-fg-muted uppercase tracking-widest text-[9px]">{cell.key} 5d</dt>
					<dd class="font-mono text-fg">{cell.value}</dd>
				</div>
			{/each}
		</dl>

		<!-- The drivers, each beside the value at which the label would flip. -->
		<dl
			aria-label="market state drivers"
			class="flex items-baseline gap-x-4 gap-y-1 flex-wrap text-[11px] min-w-0"
		>
			{#each driverCells as cell (cell.key)}
				<div class="flex items-baseline gap-1.5 whitespace-nowrap">
					<dt class="text-fg-muted uppercase tracking-widest text-[9px]">{cell.key}</dt>
					<dd class="font-mono text-fg">{cell.value}</dd>
					<span class="text-fg-muted text-[9px]">({cell.flip})</span>
				</div>
			{/each}
		</dl>

		<button
			type="button"
			onclick={() => (open = !open)}
			aria-expanded={open}
			aria-controls="market-context-axes"
			class="inline-flex items-center gap-1 text-[10px] uppercase tracking-widest text-fg-muted hover:text-fg transition-colors shrink-0 whitespace-nowrap"
		>
			{open ? 'hide' : 'more'}
			<ChevronDown class="size-3 transition-transform {open ? 'rotate-180' : ''}" />
		</button>
	</div>

	{#if open}
		<!-- Only what is NOT on the face. The promoted drivers are deliberately
		     not repeated here: two copies of one number twenty lines apart is a
		     silent doubling the moment one of them is edited. -->
		<div id="market-context-axes" class="mt-3 border-t border-grid pt-3">
			<dl class="grid grid-cols-2 sm:grid-cols-4 gap-x-6 gap-y-1.5 text-[11px]">
				<div class="flex items-center justify-between gap-2">
					<dt class="text-fg-muted uppercase tracking-widest text-[9px]">vix %ile</dt>
					<dd class="font-mono text-fg whitespace-nowrap">{pctile01(vixDecile)}</dd>
				</div>
				<div class="flex items-center justify-between gap-2">
					<dt class="text-fg-muted uppercase tracking-widest text-[9px]">squeeze</dt>
					<dd class="font-mono text-fg whitespace-nowrap">{squeezeLabel}</dd>
				</div>
			</dl>
			<p class="mt-2 text-[10px] italic text-fg-muted">
				label unvalidated · context-only · the three index returns are raw 5-session percent
				changes, not part of the label
			</p>
		</div>
	{/if}
</section>
