<script lang="ts">
	// Brief-level MARKET CONTEXT banner (PR-3). Rendered ONCE at the top of a
	// day view — the label is INDEX-LEVEL (SPY regime), identical for every
	// candidate on the date, so +page reads it from the first candidate. It is
	// DISPLAY-ONLY / UNVALIDATED context: a descriptive regime chip that NEVER
	// feeds candidate selection or the brief sort (buffett/oneil posture). The
	// tone is a colour, not a buy/avoid signal; the JargonTip carries the
	// "context, not a signal · unvalidated" glossary body. `unknown` is a
	// first-class state (dates that predate the signal) — the banner always
	// renders so the label is honest and the glossary reference is always present.
	import { marketStateTone, marketStateLabel, fmtNum, fmtSigned } from '$lib/format';
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
	}
	let {
		marketState,
		atrPctQ = null,
		dist200 = null,
		vix = null,
		vixDecile = null,
		squeezeOn = null
	}: Props = $props();

	const label = $derived(marketStateLabel(marketState));
	const tone = $derived(marketStateTone(marketState));

	// MarketStateTone → chip text+border classes. `red-dim` is not in the shared
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
	// %ile); `dist200` is a fraction above/below MA200 (× 100 for the signed %).
	const pctile01 = (v: number | null | undefined): string =>
		Number.isFinite(v) ? `${Math.round((v as number) * 100)}` : '—';
	const dist200Label = $derived(
		Number.isFinite(dist200) ? `${fmtSigned((dist200 as number) * 100, 1)}%` : '—'
	);
	const squeezeLabel = $derived(squeezeOn === true ? 'on' : squeezeOn === false ? 'off' : '—');
</script>

<!-- Lives INSIDE the brief header container as a footer strip — a twin of the
     "top catalyst" strip (same border-grid divider + micro-label typography), so
     the index-level regime reads as native header chrome, not a floating banner. -->
<section aria-label="market context" class="border-t border-grid px-4 sm:px-6 py-3">
	<div class="flex items-center justify-between gap-x-4 gap-y-2 flex-wrap">
		<div class="flex items-center gap-x-3 gap-y-2 flex-wrap min-w-0">
			<JargonTip {...tipProps('market context')} placement="below">
				<span class="text-[10px] uppercase tracking-widest text-fg-muted">market context</span>
			</JargonTip>

			<span
				class="inline-flex items-center gap-1.5 px-2 py-1 border text-[11px] uppercase tracking-widest whitespace-nowrap {TONE_CLASS[
					tone
				]}"
			>
				<span class="size-1.5 rounded-full bg-current" aria-hidden="true"></span>
				market · {label}
			</span>
		</div>

		<button
			type="button"
			onclick={() => (open = !open)}
			aria-expanded={open}
			aria-controls="market-context-axes"
			class="inline-flex items-center gap-1 text-[10px] uppercase tracking-widest text-fg-muted hover:text-fg transition-colors shrink-0"
		>
			{open ? 'hide' : 'axes'}
			<ChevronDown class="size-3 transition-transform {open ? 'rotate-180' : ''}" />
		</button>
	</div>

	{#if open}
		<div id="market-context-axes" class="mt-3 border-t border-grid pt-3">
			<dl class="grid grid-cols-2 sm:grid-cols-4 gap-x-6 gap-y-1.5 text-[11px]">
				<div class="flex items-center justify-between gap-2">
					<dt class="text-fg-muted uppercase tracking-widest text-[9px]">trend</dt>
					<dd class="font-mono text-fg whitespace-nowrap">{dist200Label} vs ma200</dd>
				</div>
				<div class="flex items-center justify-between gap-2">
					<dt class="text-fg-muted uppercase tracking-widest text-[9px]">vol</dt>
					<dd class="font-mono text-fg whitespace-nowrap">{pctile01(atrPctQ)} %ile atr</dd>
				</div>
				<div class="flex items-center justify-between gap-2">
					<dt class="text-fg-muted uppercase tracking-widest text-[9px]">vix</dt>
					<dd class="font-mono text-fg whitespace-nowrap">
						{fmtNum(vix, 1)} · {pctile01(vixDecile)} %ile
					</dd>
				</div>
				<div class="flex items-center justify-between gap-2">
					<dt class="text-fg-muted uppercase tracking-widest text-[9px]">squeeze</dt>
					<dd class="font-mono text-fg whitespace-nowrap">{squeezeLabel}</dd>
				</div>
			</dl>
			<p class="mt-2 text-[10px] italic text-fg-muted">unvalidated · context-only</p>
		</div>
	{/if}
</section>
