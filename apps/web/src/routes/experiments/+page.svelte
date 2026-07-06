<script lang="ts">
	// Research ledger route — thin view layer over the experiments page.
	//
	// Greenfield layout (2026-07): a cover-sheet hero (kill-rate `0 / 18` + the
	// αt-distribution strip) leads, the paradigm ledger is the payload — grouped
	// into research-class chapters with a sticky filter/legend bar — the live-tool
	// ledger (tool.experiments) follows, and the reference material (patterns /
	// methodology / glossary) is demoted to a "supporting material" appendix (its
	// own divider). The patterns + artifacts appendix sections
	// are card grids (independent peer items), not the stacked list/table the rest
	// of the page uses. The static data (paradigms, tool experiments, artifacts,
	// patterns, status legends, group chapters) lives in
	// `$lib/data/research-ledger`; the evidence-drawer FSM lives in
	// `$lib/components/EvidenceDrawer.svelte`. This file keeps the layout, the
	// JargonTip / ChipTip wiring, the αt bar + scatter helpers, the status filter,
	// the TOC IntersectionObserver, and the hash-deep-link handling.
	//
	// WHEN CLOSING A NEW PARADIGM: append a row to `paradigms` in
	// `$lib/data/research-ledger` (populate ALL fields incl. `story`, `group`, and
	// `is_t`/`oos_t`), add the evidence filename to
	// scripts/sync-research-docs.mjs::REFERENCED, and add any new acronym to the
	// `GLOSSARY` array in `$lib/data/glossary`.

	import JargonTip from '$lib/components/JargonTip.svelte';
	import ChipTip from '$lib/components/ChipTip.svelte';
	import LedgerFilterBar, { type FilterChip } from '$lib/components/LedgerFilterBar.svelte';
	import StatusPill from '$lib/components/StatusPill.svelte';
	import Disclosure from '$lib/components/Disclosure.svelte';
	import SectionPanel from '$lib/components/SectionPanel.svelte';
	import LedgerRow from '$lib/components/LedgerRow.svelte';
	import DetailField from '$lib/components/DetailField.svelte';
	import EvidenceLink from '$lib/components/EvidenceLink.svelte';
	import EvidenceDrawer from '$lib/components/EvidenceDrawer.svelte';
	import { toneClass } from '$lib/tone';
	import { fmtSigned } from '$lib/format';
	import { GLOSSARY, GLOSSARY_BY_TERM } from '$lib/data/glossary';
	import {
		paradigms,
		artifacts,
		patterns,
		statusLegend,
		toolExperiments,
		toolStatusLegend,
		toolStatusTone,
		alphaValueTone,
		alphaBand,
		stripLedgerMarkup,
		groupedParadigms,
		paradigmScatter,
		PARADIGM_GROUPS,
		ALPHA_T_MARGINAL,
		ALPHA_T_DOCTRINE,
		type ParadigmStatus
	} from '$lib/data/research-ledger';

	// Plain-text status definitions for the on-hover chip tooltips (ChipTip) that
	// replace the two written legend blocks. Paradigm defs carry [jargon] markup —
	// stripped to plain text because the popover is pointer-events-none and can't
	// host nested inline tips. The legend arrays stay the single source of truth.
	const paradigmStatusDef = new Map(
		statusLegend.map((s) => [s.status, stripLedgerMarkup(s.definition)])
	);
	const toolStatusDef = new Map(
		toolStatusLegend.map((s) => [s.status, stripLedgerMarkup(s.definition)])
	);

	// Tooltip helper — looks term up in the shared GLOSSARY (single source of
	// truth). Any text rendered through `parseMarkup` can wrap a term inline
	// via [term] or [term|visible-label] syntax; the tooltip body comes from
	// the glossary entry.
	function tipProps(term: string) {
		const g = GLOSSARY_BY_TERM.get(term);
		return { term: g?.term ?? term, full: g?.full ?? '', body: g?.body ?? '' };
	}

	type MarkupSeg = { kind: 'text'; text: string } | { kind: 'term'; term: string; label: string };

	// Atomic-group emulation `(?=([^|\]]+))\1` keeps an unterminated `[…` run from
	// backtracking char-by-char (O(n²)); groups unchanged (1 = term, 2 = label).
	// Mirrors `stripLedgerMarkup` in $lib/data/research-ledger — keep in sync.
	const MARKUP_RE = /\[(?=([^|\]]+))\1(?:\|([^\]]+))?\]/g;

	function parseMarkup(text: string): MarkupSeg[] {
		const out: MarkupSeg[] = [];
		let lastIndex = 0;
		let m: RegExpExecArray | null;
		MARKUP_RE.lastIndex = 0;
		while ((m = MARKUP_RE.exec(text)) !== null) {
			if (m.index > lastIndex) {
				out.push({ kind: 'text', text: text.slice(lastIndex, m.index) });
			}
			out.push({ kind: 'term', term: m[1], label: m[2] ?? m[1] });
			lastIndex = MARKUP_RE.lastIndex;
		}
		if (lastIndex < text.length) {
			out.push({ kind: 'text', text: text.slice(lastIndex) });
		}
		return out;
	}

	// Glossary subset scoped to /experiments — drops brief-only entries.
	const experimentsGlossary = GLOSSARY.filter(
		(g) => !g.pages || g.pages.includes('experiments')
	);

	// --- Ledger derivations ---------------------------------------------------
	// Research-class chapters (pure helper); the hero αt-distribution strip; and
	// the aggregate verdict (0 of 18 ever cleared the deploy bar).
	const grouped = groupedParadigms(paradigms, PARADIGM_GROUPS);
	const scatter = paradigmScatter(paradigms);
	const nTested = paradigms.length;
	const nDeployed = paradigms.filter((p) => (p.oos_t ?? p.is_t ?? 0) >= ALPHA_T_DOCTRINE).length;

	function statusTone(s: ParadigmStatus | 'OSS' | 'INTERNAL'): string {
		switch (s) {
			case 'FAIL':
			case 'SLIPPAGE-FAIL':
				return toneClass('red');
			case 'INCONCLUSIVE':
			case 'PASS_MARGINAL':
				return toneClass('magenta');
			case 'IN-FLIGHT':
			case 'INTERNAL':
				return toneClass('cyan');
			case 'OSS':
				return toneClass('amber');
			default:
				return toneClass('muted');
		}
	}

	// --- Status filter (the sticky chip bar doubles as the visible legend) ----
	// Multi-select status filters — one Set per ledger. Empty = ALL (every row
	// shown), so smoke assertions (which never click a chip) see the full ledger.
	// The shared <LedgerFilterBar> owns the toggle / clear / blur-on-click
	// behaviour and binds these sets; the page owns the row-level predicates.
	let selected = $state<Set<string>>(new Set());
	const showP = (p: (typeof paradigms)[number]) => selected.size === 0 || selected.has(p.status);

	let toolSelected = $state<Set<string>>(new Set());
	const showT = (t: (typeof toolExperiments)[number]) =>
		toolSelected.size === 0 || toolSelected.has(t.status);

	const statusCount = (s: ParadigmStatus) => paradigms.filter((p) => p.status === s).length;
	const filterChips: FilterChip[] = [
		{ key: 'ALL', label: 'all', count: nTested, tone: 'text-fg border-fg-muted', def: 'every hypothesis in the ledger' },
		...(['FAIL', 'INCONCLUSIVE', 'SLIPPAGE-FAIL'] as ParadigmStatus[])
			.filter((s) => statusCount(s) > 0)
			.map((s) => ({
				key: s,
				label: s.toLowerCase(),
				count: statusCount(s),
				tone: statusTone(s),
				def: paradigmStatusDef.get(s) ?? ''
			}))
	];
	const toolStatusCount = (s: string) => toolExperiments.filter((t) => t.status === s).length;
	const toolFilterChips: FilterChip[] = [
		{ key: 'ALL', label: 'all', count: toolExperiments.length, tone: 'text-fg border-fg-muted', def: 'every live-tool experiment' },
		...toolStatusLegend
			.filter((s) => toolStatusCount(s.status) > 0)
			.map((s) => ({
				key: s.status,
				label: s.status.toLowerCase(),
				count: toolStatusCount(s.status),
				tone: toolStatusTone(s.status),
				def: toolStatusDef.get(s.status) ?? ''
			}))
	];

	// αt bar + scatter geometry. The 0–4 scale, the 2.0 marginal hairline, and the
	// 3.5 deploy marker are shared by the per-row bars and the hero strip so they
	// can never disagree.
	const T_SCALE_MAX = 4.0;
	const MARGINAL_PCT = (ALPHA_T_MARGINAL / T_SCALE_MAX) * 100;
	const DOCTRINE_PCT = (ALPHA_T_DOCTRINE / T_SCALE_MAX) * 100;
	function tBarWidthPct(t: number | null): number {
		if (t === null || !Number.isFinite(t)) return 0;
		const clamped = Math.max(0, Math.min(t, T_SCALE_MAX));
		return (clamped / T_SCALE_MAX) * 100;
	}
	// Bar-fill colour by the shared αt band. Note the `noise` band uses the dim
	// `bg-amber-dim` (a quieter bar), NOT the muted fg colour that alphaValueTone
	// uses for the same band — the two intentionally diverge on colour while
	// sharing the band thresholds via `alphaBand`.
	function tBarTone(t: number | null): string {
		switch (alphaBand(t)) {
			case 'negative':
				return toneClass('red', ['bg']);
			case 'noise':
				return 'bg-amber-dim';
			case 'marginal':
				return toneClass('amber', ['bg']);
			case 'deploy':
				return toneClass('green', ['bg']);
			default: // null
				return toneClass('muted', ['bg']);
		}
	}

	// Evidence drawer instance — bound via `bind:this`. Owns its open/loading/
	// content/error FSM, the synced-doc fetch, the sanitized markdown pipeline,
	// and Esc-to-close; row buttons just call `evidenceDrawer.open(path)`.
	let evidenceDrawer: EvidenceDrawer;

	// Hash auto-expand + filter reconciliation. Landing on /experiments#P14 (deep
	// linked from a postmortem / commit body) opens that paradigm's <details> and
	// scrolls it into view. If the active status filter would hide the target row,
	// reset to ALL first so the deep link never lands on a hidden element. A
	// section-level hash (#tool-experiments) scrolls but does NOT force-open a
	// row; only an <article> row (or the how-to-read primer) auto-opens.
	function expandRowForHash() {
		const id = location.hash.slice(1);
		if (!id) return;
		const p = paradigms.find((x) => x.id === id);
		if (p && selected.size > 0 && !selected.has(p.status)) selected = new Set();
		const t = toolExperiments.find((x) => x.id === id);
		if (t && toolSelected.size > 0 && !toolSelected.has(t.status)) toolSelected = new Set();
		// Defer one frame so a filter reset has re-rendered the row before we
		// query + open + scroll it.
		requestAnimationFrame(() => {
			const el = document.getElementById(id);
			if (!el) return;
			const det = el.querySelector('details');
			if (det && !det.open && (el.tagName === 'ARTICLE' || id === 'how-to-read')) det.open = true;
			requestAnimationFrame(() => el.scrollIntoView({ block: 'start', behavior: 'instant' }));
		});
	}

	function onHashChange() {
		expandRowForHash();
	}

	// Run once after hydration so the initial-load hash is honoured.
	$effect(() => {
		expandRowForHash();
	});

	// Sticky TOC — section anchor list on xl+ screens, current section highlighted
	// via IntersectionObserver. Items match the in-page <section id> anchors.
	const TOC_ITEMS = [
		{ id: 'how-to-read', label: 'how.to.read' },
		{ id: 'paradigms', label: 'paradigms.ledger' },
		{ id: 'tool-experiments', label: 'tool.experiments' },
		{ id: 'patterns', label: 'failure.patterns' },
		{ id: 'methodology', label: 'methodology.artifacts' },
		{ id: 'glossary', label: 'glossary.terms' }
	];
	let activeSection = $state<string>('how-to-read');

	$effect(() => {
		if (typeof IntersectionObserver === 'undefined') return;
		const io = new IntersectionObserver(
			(entries) => {
				const visible = entries.filter((e) => e.isIntersecting);
				if (visible.length > 0) {
					activeSection = (visible[visible.length - 1].target as HTMLElement).id;
				}
			},
			{ rootMargin: '-33% 0% -50% 0%', threshold: 0 }
		);
		for (const item of TOC_ITEMS) {
			const el = document.getElementById(item.id);
			if (el) io.observe(el);
		}
		return () => io.disconnect();
	});
</script>

<!-- Hash-change handler for deep-link row expansion. The drawer's own
     Esc-to-close <svelte:window> lives inside EvidenceDrawer.svelte. -->
<svelte:window onhashchange={onHashChange} />

<div class="max-w-[1400px] mx-auto px-3 sm:px-4 py-8 sm:py-10 xl:grid xl:grid-cols-[15rem_minmax(0,1fr)] xl:gap-8">
	<!-- Section TOC sidebar (xl+), sticky, IntersectionObserver-driven active.
	     Styled loosely on the dashboard mockup (mono type kept): no boxed border,
	     the active section reads as amber text on an amber-tint background block. -->
	<aside class="hidden xl:block">
		<nav aria-label="Section table of contents" class="sticky top-4">
			<div class="px-3 text-[10px] uppercase tracking-widest text-fg-muted mb-3">// toc</div>
			<ul class="flex flex-col gap-0.5 text-xs">
				{#each TOC_ITEMS as item}
					<li>
						<a
							href="#{item.id}"
							class="block px-3 py-2 transition-colors {activeSection === item.id
								? 'text-amber bg-amber/10'
								: 'text-fg-dim hover:text-fg hover:bg-bg-2'}"
						>{item.label}</a>
					</li>
				{/each}
			</ul>
		</nav>
	</aside>

	<div class="xl:min-w-0">
	<!-- ============================ COVER SHEET / HERO ======================= -->
	<header class="mb-8 fade-up">
		<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted mb-2">// experiments</div>
		<h1 class="font-display font-bold text-3xl sm:text-4xl lg:text-5xl tracking-tight text-fg">
			Research <span class="text-amber">ledger</span>
		</h1>

		<div class="text-fg-dim mt-3 max-w-3xl text-sm leading-relaxed space-y-3">
			<p>
				AlphaLens runs two parallel research tracks. This page is the
				<span class="text-amber">paradigm-search</span> track — a systematic protocol for
				falsifying alpha hypotheses on US equities under pre-registration discipline. The
				<a href="/about" class="text-cyan hover:text-amber underline decoration-dotted underline-offset-2">other track</a>
				is the thematic event-driven research assistant (dashboard / briefs), logged below in
				<a href="#tool-experiments" class="text-cyan hover:text-amber underline decoration-dotted underline-offset-2">tool.experiments</a>.
			</p>
			<p>
				Failure is the expected outcome — markets are largely efficient. The durable artifact is the
				<em>method</em> that survived the failures (<JargonTip {...tipProps('pre-registration ledger')}>pre-registration ledger</JargonTip>,
				<JargonTip {...tipProps('multi-phase audit')}>multi-phase audit</JargonTip>,
				<JargonTip {...tipProps('Bonferroni correction')}>Bonferroni</JargonTip>-correct
				multiple testing), not any individual strategy. We kill our own ideas, and none has ever
				cleared the bar to deploy real capital.
			</p>
		</div>

		<!-- Cover sheet: the aggregate verdict at a glance — `0 deployed / 18
		     tested` + the αt-distribution strip where every measured t-stat piles
		     up short of the green 3.5 deploy line. -->
		<!-- No overflow-hidden here: it would clip the αt tooltip that pops above
		     the scatter label (and the amber corner brackets). The scanline below
		     is `absolute inset-0`, so it stays within bounds without clipping. -->
		<div class="corners relative border border-grid bg-bg-1 mt-6 px-4 sm:px-6 py-5 sm:py-6">
			<!-- Subtle CRT scanline confined to the cover; off under reduced-motion. -->
			<div
				aria-hidden="true"
				class="pointer-events-none absolute inset-0 opacity-60 motion-reduce:hidden"
				style="background-image: repeating-linear-gradient(to bottom, rgba(255,255,255,0.02) 0px, rgba(255,255,255,0.02) 1px, transparent 1px, transparent 3px);"
			></div>

			<div class="relative grid gap-6 sm:gap-8 sm:grid-cols-[auto_minmax(0,1fr)] sm:items-center">
				<!-- Kill-rate: giant amber 0 (deployed) over tested. -->
				<div class="flex items-end gap-3">
					<span class="font-display font-bold text-amber leading-[0.8] text-6xl sm:text-7xl">{nDeployed}</span>
					<div class="pb-1.5">
						<div class="text-[10px] uppercase tracking-[0.2em] text-fg-dim">deployed</div>
						<div class="text-[11px] text-fg-muted">of {nTested} tested</div>
					</div>
				</div>

				<!-- αt distribution strip. -->
				<div>
					<div class="flex items-center justify-between text-[10px] uppercase tracking-widest text-fg-muted mb-1.5">
						<span><span class="normal-case"><JargonTip {...tipProps('αt')}>αt</JargonTip></span> distribution · every measured hypothesis</span>
						<span class="text-fg-dim normal-case tracking-normal">scale 0 – 4.0</span>
					</div>
					<!-- 0–4 chart: a thin track with zone tints (noise / marginal / deploy)
					     behind the 2.0 + 3.5 threshold lines, plus one tick per measured
					     hypothesis (max amber + wider). Axis labels positioned at the two
					     thresholds. -->
					<div class="relative h-10 flex items-center" role="img" aria-label="alpha t-stat distribution: {scatter.nWithT} of {nTested} hypotheses produced a t-statistic; none reached the 3.5 deploy threshold (best {scatter.maxT?.toFixed(2) ?? 'n/a'})">
						<div class="absolute inset-x-0 h-1 bg-bg-3 overflow-hidden">
							<div class="absolute inset-y-0 left-0 w-1/2 bg-bg-2"></div>
							<div class="absolute inset-y-0 bg-amber/10" style="left: {MARGINAL_PCT}%; width: {DOCTRINE_PCT - MARGINAL_PCT}%"></div>
							<div class="absolute inset-y-0 right-0 bg-green/10" style="left: {DOCTRINE_PCT}%"></div>
						</div>
						<div class="absolute inset-y-0 w-px bg-grid-strong" style="left: {MARGINAL_PCT}%"></div>
						<div class="absolute inset-y-0 w-px bg-green/60" style="left: {DOCTRINE_PCT}%"></div>
						{#each scatter.ticks as tk}
							<div
								class="absolute top-2 bottom-2 {tk.isMax ? 'w-[3px] bg-amber' : 'w-[2px] ' + tBarTone(tk.t)}"
								style="left: {tBarWidthPct(tk.t)}%"
							></div>
						{/each}
					</div>
					<div class="relative h-3 mt-2 text-[10px] uppercase tracking-wider">
						<span class="absolute left-0 text-fg-muted">&lt; 2.0 noise</span>
						<span class="absolute text-amber -translate-x-1/2" style="left: {MARGINAL_PCT}%">2.0 marginal</span>
						<span class="absolute text-green -translate-x-full whitespace-nowrap" style="left: {DOCTRINE_PCT}%">3.5 deploy →</span>
					</div>
					<p class="text-[11px] text-fg-muted mt-2 leading-relaxed">
						{scatter.nWithT} of {nTested} produced a t-stat; the other {nTested - scatter.nWithT} were
						killed pre-audit. The closest call reached
						<span class="text-amber">≈ {scatter.maxT?.toFixed(2)}</span> — an inconclusive options
						retrospective sent to paper-trade, still short of 3.5. Nothing has ever crossed the green line.
					</p>
				</div>
			</div>
		</div>
	</header>

	<!-- ==================== how.to.read (collapsed primer) =================== -->
	<section id="how-to-read" class="border border-grid bg-bg-1 mb-8 fade-up" style="animation-delay: 0.05s">
		<Disclosure summaryClass="px-4 sm:px-5 py-3 text-[10px] uppercase tracking-widest text-fg-muted hover:bg-bg-2 flex items-center gap-2">
			{#snippet summary()}
				<h2 class="font-normal">how.to.read</h2>
				<span class="ml-auto text-fg-dim normal-case tracking-normal">the αt scale · IS / OOS / FL windows · click to expand</span>
			{/snippet}
			{#snippet children()}
			<div class="px-4 sm:px-5 py-3 border-t border-grid text-sm text-fg-dim leading-relaxed">
				Each paradigm row carries a t-statistic on
				<JargonTip {...tipProps('Carhart 4F')}>Carhart-4F</JargonTip>
				α — abbreviated
				<JargonTip {...tipProps('αt')}>αt</JargonTip>.
				Higher means stronger statistical evidence the strategy worked. Project doctrine:
				<span class="text-green"><JargonTip {...tipProps('αt')}>αt</JargonTip> ≥ 3.5 = deploy-eligible</span>,
				<span class="text-amber">2.0–3.5 = marginal</span> (paper-trade only),
				<span class="text-fg-muted">&lt; 2.0 = noise</span>. Strategies are evaluated on three windows:
				<JargonTip {...tipProps('IS')}>IS</JargonTip>
				(training),
				<JargonTip {...tipProps('OOS')}>OOS</JargonTip>
				(fresh holdout), and where applicable
				<JargonTip {...tipProps('FL')}>FL</JargonTip>
				(an even more recent independent window for confirmation). The two horizontal bars per row
				visualise
				<JargonTip {...tipProps('IS')}>IS</JargonTip>
				vs
				<JargonTip {...tipProps('OOS')}>OOS</JargonTip>
				<JargonTip {...tipProps('αt')}>αt</JargonTip>;
				<JargonTip {...tipProps('FL')}>FL</JargonTip>
				values appear in the Outcome field where measured. Vertical reference lines mark 2.0 and 3.5.
				<br /><br />
				You'll see the word "phase" used two different ways across the rows. Most paradigms run a
				<JargonTip {...tipProps('multi-phase audit')}>multi-phase audit</JargonTip>
				where the same backtest is run with 5 different rebalance start-day offsets —
				<JargonTip {...tipProps('single-phase')}>single-phase</JargonTip>
				results are sample-of-one artifacts (see pattern #07). Separately, the PEAD paradigm
				(#14) was built in
				<JargonTip {...tipProps('Phase A/B/C/D/E')}>Phase A/B/C/D/E</JargonTip>
				sequential implementation milestones — different concept entirely (project phases of building
				the audit, not statistical replicates).
			</div>
			{/snippet}
		</Disclosure>
	</section>

	<!-- ============================ LEDGER 1 · paradigms ===================== -->
	<section id="paradigms" class="border border-grid bg-bg-1 mb-8 fade-up" style="animation-delay: 0.1s">
		<div class="px-4 sm:px-5 py-3 border-b border-grid">
			<div class="text-[10px] uppercase tracking-widest text-fg-muted flex items-center justify-between">
				<h2 class="font-normal">paradigms.ledger</h2>
				<span class="text-fg-dim normal-case tracking-normal">{nTested} hypotheses · {nDeployed} cleared the bar</span>
			</div>
			<p class="text-[11px] text-fg-dim mt-1.5 leading-relaxed">
				<span class="text-cyan">paradigm-search track</span> — falsifying standalone alpha hypotheses, measured in
				<JargonTip {...tipProps('αt')}>αt</JargonTip> (Carhart-4F t-stat), grouped by research class. The live-tool track is
				<a href="#tool-experiments" class="text-cyan hover:text-amber underline decoration-dotted underline-offset-2">tool.experiments</a>
				below.
			</p>
		</div>

		<!-- Multi-select status filter = the visible legend (shared component). -->
		<LedgerFilterBar chips={filterChips} bind:selected />

		<!-- Research-class chapters. Each group is a labelled band; within it the
		     paradigm rows carry a status-coloured left rail. -->
		{#each grouped as g}
			{@const vis = g.items.filter(showP)}
			{#if vis.length > 0}
				<div class="border-t-2 border-grid-strong bg-bg-2/30 px-4 sm:px-5 pt-4 pb-2.5">
					<div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
						<span class="font-display font-bold text-[11px] uppercase tracking-[0.22em] text-amber whitespace-nowrap">{g.label}</span>
						<span class="text-[10px] uppercase tracking-widest text-fg-muted whitespace-nowrap">{g.tested} tested · {g.cleared} cleared</span>
					</div>
						<!-- No max-w cap: the panel already bounds the line (~1094px at the
						     widest), and a 768px cap left ~326px empty to the right while
						     wrapping the last word to a lone second line. text-pretty avoids
						     a single-word widow if a gloss does wrap on a narrower viewport. -->
						<p class="text-[11px] text-fg-dim mt-1 leading-relaxed text-pretty">{g.gloss}</p>
					</div>
				<div class="divide-y divide-grid">
					{#each vis as p}
							<LedgerRow
								id={p.id}
								display={p.display}
								name={p.name}
								date={p.date}
								detailNoun="case detail"
							>
								{#snippet status()}
									<ChipTip term={p.status} body={paradigmStatusDef.get(p.status) ?? ''}>
										{#snippet chip()}
											<StatusPill tone={statusTone(p.status)} label={p.status} interactive />
										{/snippet}
									</ChipTip>
								{/snippet}
								{#snippet tags()}
									<span class="text-[10px] uppercase tracking-widest">
										<span class="text-fg-muted">
											<JargonTip {...tipProps(p.layer_id)}>{p.layer_id}</JargonTip> ·&nbsp;</span>
										{#if p.axis_a === 'screener'}
											<span class="text-fg-muted"><JargonTip {...tipProps('screener')}>screener</JargonTip></span>
										{:else}
											<span class="text-fg-dim font-bold"><JargonTip {...tipProps(p.axis_a)}>{p.axis_a}</JargonTip></span>
										{/if}
										{#if p.axis_b && p.axis_b.length > 0}
											<span class="text-fg-muted"> / </span>
											{#each p.axis_b as b, i}
												{#if i > 0}<span class="text-fg-muted"> × </span>{/if}<span class="text-fg-dim font-bold"><JargonTip {...tipProps(b)}>{b}</JargonTip></span>
											{/each}
										{/if}
									</span>
								{/snippet}
								{#snippet preface()}
							<div class="sm:pl-12 mb-3 lg:flex lg:items-start lg:justify-between lg:gap-8">
								<p class="text-[13px] text-fg-dim leading-relaxed mb-3 lg:mb-0 lg:max-w-[72ch]">{p.story}</p>

								{#if p.is_t !== null || p.oos_t !== null}
									<div class="text-[11px] w-full lg:w-[28rem] lg:shrink-0">
										<div class="flex items-center gap-2 mb-1">
											<span class="w-10 text-fg-muted uppercase tracking-widest">
												<JargonTip {...tipProps('IS')}>IS</JargonTip>
											</span>
											<div class="relative h-2 flex-1 bg-bg-3 overflow-hidden ring-1 ring-inset ring-grid">
												{#if p.is_t !== null}
													<div class="absolute inset-y-0 left-0 {tBarTone(p.is_t)}" style="width: {tBarWidthPct(p.is_t)}%"></div>
												{/if}
												<div class="absolute inset-y-0 border-l border-grid-strong" style="left: {MARGINAL_PCT}%"></div>
												<div class="absolute inset-y-0 border-l-2 border-green" style="left: {DOCTRINE_PCT}%"></div>
											</div>
											<span class="w-14 text-right font-mono {alphaValueTone(p.is_t)}">{fmtSigned(p.is_t)}</span>
										</div>
										<div class="flex items-center gap-2">
											<span class="w-10 text-fg-muted uppercase tracking-widest">
												<JargonTip {...tipProps('OOS')}>OOS</JargonTip>
											</span>
											<div class="relative h-2 flex-1 bg-bg-3 overflow-hidden ring-1 ring-inset ring-grid">
												{#if p.oos_t !== null}
													<div class="absolute inset-y-0 left-0 {tBarTone(p.oos_t)}" style="width: {tBarWidthPct(p.oos_t)}%"></div>
												{/if}
												<div class="absolute inset-y-0 border-l border-grid-strong" style="left: {MARGINAL_PCT}%"></div>
												<div class="absolute inset-y-0 border-l-2 border-green" style="left: {DOCTRINE_PCT}%"></div>
											</div>
											<span class="w-14 text-right font-mono {alphaValueTone(p.oos_t)}">{fmtSigned(p.oos_t)}</span>
										</div>
									</div>
								{/if}
							</div>
								{/snippet}
								{#snippet fields()}
									<DetailField label="Hypothesis">{#each parseMarkup(p.hypothesis) as seg}{#if seg.kind === 'term'}<JargonTip {...tipProps(seg.term)}>{seg.label}</JargonTip>{:else}{seg.text}{/if}{/each}</DetailField>
									<DetailField label="Mechanism">{#each parseMarkup(p.mechanism) as seg}{#if seg.kind === 'term'}<JargonTip {...tipProps(seg.term)}>{seg.label}</JargonTip>{:else}{seg.text}{/if}{/each}</DetailField>
									<DetailField label="Outcome" ddClass="text-fg">{#each parseMarkup(p.metric) as seg}{#if seg.kind === 'term'}<JargonTip {...tipProps(seg.term)}>{seg.label}</JargonTip>{:else}{seg.text}{/if}{/each}</DetailField>
									<DetailField label="Lesson">{#each parseMarkup(p.lesson) as seg}{#if seg.kind === 'term'}<JargonTip {...tipProps(seg.term)}>{seg.label}</JargonTip>{:else}{seg.text}{/if}{/each}</DetailField>
									{#if p.evidence}
										<DetailField label="Evidence"><EvidenceLink path={p.evidence} onopen={(pth) => evidenceDrawer.open(pth)} /></DetailField>
									{/if}
								{/snippet}
							</LedgerRow>
					{/each}
				</div>
			{/if}
		{/each}
	</section>

	<!-- ============================ LEDGER 2 · tool.experiments ============== -->
	<SectionPanel id="tool-experiments" title="tool.experiments" style="animation-delay: 0.14s">
		{#snippet meta()}
			<span class="text-fg-dim normal-case tracking-normal">{toolExperiments.length} rows · tuning the live tool</span>
		{/snippet}
		{#snippet children()}

		<div class="px-4 sm:px-5 py-3 border-b border-grid">
			<p class="text-[11px] text-fg-dim leading-relaxed">
				<span class="text-cyan">live-tool track</span> — changes tested on how the tool picks names (selection) and how
				trades enter and exit. Measured on realized R, market-excess return, and live sample size (N), not
				<JargonTip {...tipProps('αt')}>αt</JargonTip>. These are open, forward experiments — no terminal verdict.
			</p>
			<p class="text-[11px] text-fg-dim leading-relaxed mt-2">
				<span class="text-amber">Honesty rule:</span> anything marked
				<span class="text-cyan">FORWARD-LOG</span> is an in-sample what-if replay that never touched the real
				trade record and has not passed a fresh forward test.
				<span class="text-fg-muted">Snapshot 2026-07-01 · 372 plannable / 89 terminal / 43 brief-days.</span>
			</p>
		</div>

		<!-- Multi-select status filter = the visible legend (shared component). -->
		<LedgerFilterBar chips={toolFilterChips} bind:selected={toolSelected} />

		<div class="divide-y divide-grid">
			{#each toolExperiments.filter(showT) as t}
					<LedgerRow
						id={t.id}
						display={t.display}
						displayWidth="w-8 sm:w-10"
						name={t.name}
						date={t.date}
						detailMargin="sm:ml-10"
						detailNoun="detail"
					>
						{#snippet status()}
							<ChipTip term={t.status} body={toolStatusDef.get(t.status) ?? ''}>
								{#snippet chip()}
									<StatusPill tone={toolStatusTone(t.status)} label={t.status} interactive />
								{/snippet}
							</ChipTip>
						{/snippet}
						{#snippet preface()}
							<!-- No inline in-sample badge: it only ever appeared on FORWARD-LOG
							     rows, which already carry the FORWARD-LOG pill (its ChipTip + the
							     section "Honesty rule" define it as an in-sample what-if replay).
							     The badge duplicated that pill, so the metric line stands alone. -->
							<p class="text-sm text-fg leading-relaxed mb-3 sm:pl-10">{t.metric}</p>
						{/snippet}
						{#snippet fields()}
							<DetailField label="Hypothesis">{t.hypothesis}</DetailField>
							<DetailField label="Mechanism">{t.mechanism}</DetailField>
							<DetailField label="Outcome" ddClass="text-fg">{t.outcome}</DetailField>
							<DetailField label="Lesson">{t.lesson}</DetailField>
							<DetailField label="PRs" ddClass="font-mono text-[11px] text-fg-muted">{t.prs.join(' · ')}</DetailField>
							{#if t.evidence}
								<DetailField label="Evidence"><EvidenceLink path={t.evidence} onopen={(pth) => evidenceDrawer.open(pth)} /></DetailField>
							{/if}
						{/snippet}
					</LedgerRow>
			{/each}
		</div>
		{/snippet}
	</SectionPanel>

	<!-- ============================ APPENDIX ================================= -->
	<div class="mt-10 mb-4 flex items-center gap-3">
		<span class="text-[10px] uppercase tracking-[0.25em] text-fg-muted whitespace-nowrap">// supporting material</span>
		<span class="h-px flex-1 bg-grid"></span>
	</div>

	<SectionPanel id="patterns" title="failure.patterns" style="animation-delay: 0.16s">
		{#snippet meta()}
			<span class="text-fg-dim normal-case tracking-normal">{patterns.length} reusable lessons · hover dotted terms for definitions</span>
		{/snippet}
		{#snippet children()}
		<!-- Lesson-card grid. These reusable lessons are independent, self-contained
		     takeaways — index cards, not a sequence — so they read as a 2-up grid of
		     quiet bordered cards rather than a stacked list. Each keeps its <h3> name
		     + inline JargonTips; the amber index number is the card's tab. -->
		<!-- role="list" restores list semantics under Tailwind Preflight's
		     list-style:none (Safari drops them otherwise). -->
		<ul role="list" class="grid grid-cols-1 md:grid-cols-2 gap-3 p-4 sm:p-5">
			{#each patterns as p}
				<li
					data-testid="pattern-card"
					class="flex gap-3 border border-grid bg-bg-2/30 px-4 py-3.5 text-sm transition-colors hover:border-grid-strong hover:bg-bg-2"
				>
					<span class="font-display font-bold text-lg leading-none text-amber tabular-nums shrink-0 w-7 pt-0.5">{p.n}</span>
					<div class="min-w-0">
						<h3 class="font-bold text-fg leading-snug">
							{#each parseMarkup(p.name) as seg}
								{#if seg.kind === 'term'}
									<JargonTip {...tipProps(seg.term)}>{seg.label}</JargonTip>
								{:else}
									{seg.text}
								{/if}
							{/each}
						</h3>
						<div class="text-fg-dim text-xs mt-1 leading-relaxed">
							{#each parseMarkup(p.body) as seg}
								{#if seg.kind === 'term'}
									<JargonTip {...tipProps(seg.term)}>{seg.label}</JargonTip>
								{:else}
									{seg.text}
								{/if}
							{/each}
						</div>
					</div>
				</li>
			{/each}
		</ul>
		{/snippet}
	</SectionPanel>

	<SectionPanel id="methodology" title="methodology.artifacts" style="animation-delay: 0.2s">
		{#snippet meta()}
			<span class="text-fg-dim normal-case tracking-normal">{artifacts.length} items · what survived</span>
		{/snippet}
		{#snippet children()}
		<!-- Feature-card grid — "what survived". The durable methodology artifacts
		     are the proud outputs of the whole search, so they get the elevated card
		     treatment: the hero's amber corner-bracket motif, a bright bg, the status
		     pill, and the repo/doc reference pinned to the card footer. -->
		<!-- <ul>/<li>, not <article>: the page reserves <article> for an expandable
		     ledger row (one <details> each — see P0.1), so a static card must not
		     inflate that count. role="list" keeps list semantics under Preflight's
		     list-style:none, matching the pattern grid above. -->
		<ul role="list" class="grid grid-cols-1 md:grid-cols-2 gap-3 p-4 sm:p-5">
			{#each artifacts as a}
				<li
					data-testid="artifact-card"
					class="corners relative flex flex-col border border-grid bg-bg-1 p-4 transition-colors hover:border-grid-strong hover:bg-bg-2"
				>
					<div class="flex items-start gap-2.5 mb-2">
						<span class="font-display font-bold text-lg leading-none text-amber shrink-0 pt-0.5">{a.id}</span>
						<span class="font-bold text-fg leading-snug min-w-0">{a.name}</span>
						<span class="ml-auto shrink-0"><StatusPill tone={statusTone(a.status)} label={a.status} /></span>
					</div>
					<p class="text-fg-dim text-xs leading-relaxed">{a.description}</p>
					<div class="mt-auto pt-3 text-[11px] font-mono text-cyan break-all">{a.link}</div>
				</li>
			{/each}
		</ul>
		{/snippet}
	</SectionPanel>

	<!-- glossary section — secondary lookup table (inline JargonTips above are the
	     primary reference). Brief-only terms filtered out. No JargonTips inside
	     (they define the terms; a tip here would be self-referential). -->
	<section id="glossary" class="border border-grid bg-bg-1 fade-up" style="animation-delay: 0.28s">
		<Disclosure summaryClass="px-4 sm:px-5 py-3 border-b border-grid text-[10px] uppercase tracking-widest text-fg-muted hover:bg-bg-2 flex items-center gap-2">
			{#snippet summary()}
				<h2 class="font-normal">glossary.terms</h2>
				<span class="ml-auto text-fg-dim normal-case tracking-normal">{experimentsGlossary.length} terms · click to expand · hover dotted-underlined inline terms above for primary reference</span>
			{/snippet}
			{#snippet children()}
			<dl class="divide-y divide-grid">
				{#each experimentsGlossary as g}
					<div class="px-4 sm:px-5 py-3 grid grid-cols-1 sm:grid-cols-[180px_1fr] gap-x-4 gap-y-1 text-sm">
						<dt class="font-display font-bold text-amber">
							{g.term}
							<span class="block text-[10px] uppercase tracking-widest text-fg-muted font-normal normal-case mt-0.5">{g.full}</span>
						</dt>
						<dd class="text-fg-dim text-xs sm:text-sm leading-relaxed">{g.body}</dd>
					</div>
				{/each}
			</dl>
			{/snippet}
		</Disclosure>
	</section>
	</div>
</div>

<EvidenceDrawer bind:this={evidenceDrawer} />
