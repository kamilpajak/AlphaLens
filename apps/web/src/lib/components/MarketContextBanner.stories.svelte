<script module lang="ts">
	import type { ComponentProps } from 'svelte';
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { expect, waitFor } from 'storybook/test';
	import MarketContextBanner from './MarketContextBanner.svelte';
	import laneDay from '../../../tests/fixtures/api-mock/days/2026-09-05.json';
	import preSignalDay from '../../../tests/fixtures/api-mock/days/2026-05-18.json';
	import may19 from '../../../tests/fixtures/api-mock/market-context/2026-05-19.json';

	type BannerProps = ComponentProps<typeof MarketContextBanner>;

	// EVERY NUMBER IN THIS FILE IS MEASURED, NOT INVENTED.
	//
	// The index-level payload is identical on every candidate of a date, so the
	// first one carries it. `days/2026-09-05.json` is the only day fixture that
	// holds a market_state payload at all (the other three predate the signal).
	const ctx = laneDay.candidates[0];

	// The three 5-session index returns for the SAME asof (2026-09-05), computed
	// from the real grouped-daily store on the author's machine on 2026-10-30 by
	// the production function itself:
	//
	//   five_session_index_returns(date(2026, 9, 5))
	//   -> {spy: 0.0026844767215665755, qqq: -0.004770423375074606,
	//       iwm: -0.015409759514359078}
	//
	// Honest caveat kept here rather than smoothed away: that mirror's newest
	// session is 2026-09-03, so the anchor rolled back two sessions (inside the
	// staleness bound). The VPS store reaches 2026-09-05 and would therefore
	// answer a slightly different trio for the same date. These are real numbers
	// this code really produced, not the number production will hold.
	const MEASURED_RET5 = {
		ret5Spy: 0.0026844767215665755,
		ret5Qqq: -0.004770423375074606,
		ret5Iwm: -0.015409759514359078
	};

	const fromFixture: BannerProps = {
		marketState: ctx.market_state,
		atrPctQ: ctx.market_state_atr_pct_q,
		dist200: ctx.market_state_dist200,
		vix: ctx.market_state_vix,
		vixDecile: ctx.market_state_vix_decile,
		squeezeOn: ctx.market_state_squeeze_on
	};

	// 2026-05-19, read from its own fixture: the three returns at full precision
	// (produced by the production writer against the real store — provenance is
	// in the file) and NO market_state key at all. `market_state` is read off a
	// real pre-signal candidate row the same way the page does — `days/2026-05-18.json`
	// carries no market_state* column, so it reads `undefined`, which is exactly
	// what the API serves for 2026-05-19 too.
	const preSignalRow = preSignalDay.candidates[0] as Record<string, unknown>;
	const RETURNS_ONLY: BannerProps = {
		marketState: preSignalRow.market_state as string | null | undefined,
		ret5Spy: may19.market_ret5_spy,
		ret5Qqq: may19.market_ret5_qqq,
		ret5Iwm: may19.market_ret5_iwm
	};

	// Everything absent: `days/2026-05-18.json` candidates[0] genuinely carries
	// no market_state* key and no market_ret5_* key — its only `market*` column
	// is `market_cap`. Every prop below therefore reads `undefined` from a real
	// captured row, exactly as the page passes them on such a date.
	const ALL_ABSENT: BannerProps = {
		marketState: preSignalRow.market_state as string | null | undefined,
		atrPctQ: preSignalRow.market_state_atr_pct_q as number | null | undefined,
		dist200: preSignalRow.market_state_dist200 as number | null | undefined,
		vix: preSignalRow.market_state_vix as number | null | undefined,
		vixDecile: preSignalRow.market_state_vix_decile as number | null | undefined,
		squeezeOn: preSignalRow.market_state_squeeze_on as boolean | null | undefined,
		ret5Spy: preSignalRow.market_ret5_spy as number | null | undefined,
		ret5Qqq: preSignalRow.market_ret5_qqq as number | null | undefined,
		ret5Iwm: preSignalRow.market_ret5_iwm as number | null | undefined
	};

	// DRIVERS THAT OUTLIVED THEIR LABEL. `classify_state` fills its driver
	// telemetry before it decides anything, so a date can carry real drivers
	// beside no regime word. This is the shape that reaches a reader most often:
	// the vol axis is an OR of a realized and an implied leg, so a realized leg
	// below the 70th percentile with no VIX print cannot settle it (#1559), and
	// the producer then stamps `dist200` and `atr_pct_q` with the literal label
	// `'unknown'` and leaves `vix` NaN.
	//
	// Composed by DELETION plus that one producer constant: both driver values
	// are the real 2026-09-05 capture, and `'unknown'` is `_UNKNOWN` in
	// `market_state.py`, not a guess. (`vix` alone beside `unknown` is reachable
	// too — a store under 200 sessions — but it is the rarer shape.)
	// `squeezeOn` belongs here too: `market_state.py:237` stamps it in the
	// `have_bars` branch, BEFORE either `unknown` return, so the real shape also
	// has a residual drawer and therefore a HIDE/MORE toggle. Leaving it out
	// would show a strip with less on it than the date actually has.
	const DRIVERS_WITHOUT_LABEL: BannerProps = {
		marketState: 'unknown',
		dist200: ctx.market_state_dist200,
		atrPctQ: ctx.market_state_atr_pct_q,
		squeezeOn: ctx.market_state_squeeze_on
	};

	// The tone matrix varies ONLY the label. Every driver is null, because no
	// fixture exists for bull_volatile / bear_volatile / bear_quiet and
	// plausible-looking telemetry would be invented data on the page. With the
	// disappear rule this renders the two-line minimum: the label and the
	// unvalidated caption, no rows and no toggle.
	const labelOnly = (marketState: string): BannerProps => ({ marketState });

	// Open the "more" disclosure so the residual drawer is visible for review.
	// The assertion is on a DRAWER-ONLY cell: the unvalidated caption is now
	// always on screen, so waiting for it would pass before the click and prove
	// nothing.
	const openMore = async ({ canvas }: { canvas: any }) => {
		canvas.getByRole('button', { name: /more/i }).click();
		await waitFor(() => expect(canvas.getByText(/vix %ile/i)).toBeTruthy());
	};

	// The counterpart assertion: on a date with no residual the toggle must not
	// exist at all. `queryByRole` returns null instead of throwing, which is the
	// only way to assert an absence.
	const expectNoToggle = async ({ canvas }: { canvas: any }) => {
		await waitFor(() => expect(canvas.queryByRole('button', { name: /more|hide/i })).toBeNull());
		// …while the caption that keeps the strip honest is still on screen.
		expect(canvas.getByText(/label unvalidated/i)).toBeTruthy();
	};

	const { Story } = defineMeta({
		title: 'Briefs/MarketContextBanner',
		component: MarketContextBanner,
		tags: ['autodocs'],
		parameters: { layout: 'padded' },
		render: template
	});
</script>

{#snippet template(args: BannerProps)}
	<!-- Wrapped in a header-like container: the strip is a border-t footer, so it
	     previews accurately as the bottom row of the brief session box. -->
	<div style="padding: 1.5rem;">
		<div class="border border-grid bg-bg-1">
			<div class="px-4 sm:px-6 py-5">
				<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted">// session</div>
				<div class="font-display font-bold text-3xl text-amber tracking-tight mt-1">2026-09-05</div>
			</div>
			<MarketContextBanner {...args} />
		</div>
	</div>
{/snippet}

<!-- EVERYTHING PRESENT: the real captured 2026-09-05 fixture plus the measured
     trio for the same date. Note `squeeze off` in the drawer — `false` is a real
     reading and must survive the disappear rule, which a truthiness guard would
     have swept away with the absences. -->
<Story
	name="everything present (label + drivers + returns)"
	args={{ ...fromFixture, ...MEASURED_RET5 }}
/>

<!-- THE DOMINANT HISTORICAL STATE, and the one the owner hit on his first look:
     the label is `unknown` because market_state only ships from 2026-07-05,
     while the three returns ARE real because the price store reaches back to
     2024-09-11. Measured on production Postgres 2026-10-30: 164 brief dates, of
     which the 47 from 2026-05-19 to 2026-07-04 land here — 29% of everything a
     reader can scroll back to.

     What the strip must show: the three returns, the `market · unknown` caption
     and the unvalidated note. The driver row is GONE rather than three em dashes
     each carrying a flip point beside nothing, and no "more" toggle is offered
     because the drawer would be empty. -->
<Story name="returns present, label absent (2026-05-19 — 29% of dates)" args={RETURNS_ONLY} />

<!-- Label and drivers present, returns absent: every published date from
     2026-07-05 until the backfill ran. The index row disappears; the driver row
     and the drawer stay. -->
<Story name="label + drivers present, returns absent" args={fromFixture} />

<!-- EVERYTHING ABSENT — a date before BOTH families, bound to the real
     2026-05-18 row. The strip still renders, and renders exactly three things:
     the micro-label with its glossary tip, `market · unknown`, and the
     unvalidated caption. No rows, no toggle, no em dash anywhere. This is the
     state a reader is most likely to mistake for a broken component, which is
     why it has its own story. -->
<Story name="everything absent (two lines, no em dashes)" args={ALL_ABSENT} play={expectNoToggle} />

<!-- The sharper no-drawer case: the face is FULL, but `vix_decile` and
     `squeeze_on` are both absent, so the drawer would reveal nothing and the
     toggle must still not appear. No fixture can supply this (2026-09-05 stamps
     both), so the two residual props are overridden to null on the real row and
     the override is stated here rather than dressed up as captured data. -->
<Story
	name="full face, no residual, no toggle"
	args={{ ...fromFixture, ...MEASURED_RET5, vixDecile: null, squeezeOn: null }}
	play={expectNoToggle}
/>

<!-- Tone matrix — label only, drivers deliberately null (see labelOnly). Tone is
     display-only (green / amber / red / red-dim); never a buy/avoid signal.
     Under the disappear rule each of these is the two-line minimum. -->
<Story name="tone — bull-quiet (green)" args={labelOnly('bull_quiet')} />
<Story name="tone — bull-volatile (amber)" args={labelOnly('bull_volatile')} />
<Story name="tone — bear-volatile (red)" args={labelOnly('bear_volatile')} />
<Story name="tone — bear-quiet (muted red)" args={labelOnly('bear_quiet')} />

<!-- DRIVERS WITH NO LABEL BESIDE THEM: `vs ma200 +8.1%` and `atr %ile 10`, with
     nothing in brackets after either. A flip point annotates the regime word,
     and the word here is `unknown`, so "(volatile at 70)" would name the flip of
     a label that is not on the page. The values themselves stay — they are real
     readings, and the classifier emitted them before it gave up deciding. -->
<Story name="drivers without a label (no flip points)" args={DRIVERS_WITHOUT_LABEL} />

<!-- The residual drawer expanded: vix %ile + squeeze. The promoted drivers are
     NOT repeated in it, and the unvalidated caption is no longer inside it — it
     sits below the drawer and renders on every date. -->
<Story
	name="drawer expanded"
	args={{ ...fromFixture, ...MEASURED_RET5 }}
	play={openMore}
/>
