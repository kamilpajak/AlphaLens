<script lang="ts">
	// Research ledger route — thin view layer over the experiments page.
	//
	// The static data (paradigm experiments, live infrastructure, methodology
	// artifacts, failure patterns, status legend) lives in
	// `$lib/data/research-ledger`. The evidence-drawer FSM + the sanitized
	// markdown pipeline live in `$lib/components/EvidenceDrawer.svelte`. This
	// file keeps the page layout, the JargonTip tooltip wiring, the αt mini-bar
	// helpers, the TOC IntersectionObserver, and the hash-deep-link handling.
	//
	// WHEN CLOSING A NEW PARADIGM: append a row to `paradigms` in
	// `$lib/data/research-ledger`, populate ALL fields including `story` (plain
	// English) and `is_t`/`oos_t` (numeric, nullable), and add the evidence
	// filename to scripts/sync-research-docs.mjs::REFERENCED. New acronyms
	// should also get a row in the `GLOSSARY` array in `$lib/data/glossary`.

	import JargonTip from '$lib/components/JargonTip.svelte';
	import EvidenceDrawer from '$lib/components/EvidenceDrawer.svelte';
	import { GLOSSARY, GLOSSARY_BY_TERM } from '$lib/data/glossary';
	import {
		paradigms,
		live,
		artifacts,
		patterns,
		statusLegend,
		type ParadigmStatus,
		type LiveStatus
	} from '$lib/data/research-ledger';

	// Tooltip helper — looks term up in the shared GLOSSARY (single source of
	// truth). Any text rendered through `parseMarkup` can wrap a term inline
	// via [term] or [term|visible-label] syntax; the tooltip body comes from
	// the glossary entry. Adding a new tooltip = one entry in $lib/data/glossary
	// + one `[term]` marker in the prose.
	function tipProps(term: string) {
		const g = GLOSSARY_BY_TERM.get(term);
		return { term: g?.term ?? term, full: g?.full ?? '', body: g?.body ?? '' };
	}

	type MarkupSeg = { kind: 'text'; text: string } | { kind: 'term'; term: string; label: string };

	const MARKUP_RE = /\[([^|\]]+)(?:\|([^\]]+))?\]/g;

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

	// Glossary subset scoped to /experiments — drops brief-only entries
	// (PE, PS, EV/EBITDA, ROE, etc. — pages: ['briefs']) so the lookup table
	// here only lists terms actually used by paradigms / patterns above.
	const experimentsGlossary = GLOSSARY.filter(
		(g) => !g.pages || g.pages.includes('experiments')
	);

	const summaryCounts = $derived.by(() => {
		const c: Record<ParadigmStatus, number> = { FAIL: 0, 'SLIPPAGE-FAIL': 0, 'IN-FLIGHT': 0, INCONCLUSIVE: 0, PASS_MARGINAL: 0 };
		for (const p of paradigms) c[p.status]++;
		return c;
	});

	function statusTone(s: ParadigmStatus | LiveStatus | 'OSS' | 'INTERNAL'): string {
		switch (s) {
			case 'FAIL':
			case 'SLIPPAGE-FAIL':
				return 'text-red border-red';
			case 'INCONCLUSIVE':
			case 'PASS_MARGINAL':
				return 'text-magenta border-magenta';
			case 'IN-FLIGHT':
				return 'text-cyan border-cyan';
			case 'LIVE':
			case 'SHIPPED':
			case 'DONE':
				return 'text-green border-green';
			case 'OSS':
				return 'text-amber border-amber';
			case 'INTERNAL':
				return 'text-cyan border-cyan';
			default:
				return 'text-fg-dim border-grid';
		}
	}

	const T_SCALE_MAX = 4.0;
	const T_MARGINAL = 2.0;
	const T_DOCTRINE = 3.5;
	function tBarWidthPct(t: number | null): number {
		if (t === null || !Number.isFinite(t)) return 0;
		const clamped = Math.max(0, Math.min(t, T_SCALE_MAX));
		return (clamped / T_SCALE_MAX) * 100;
	}
	function tBarTone(t: number | null): string {
		if (t === null) return 'bg-fg-muted';
		if (t < 0) return 'bg-red';
		if (t < T_MARGINAL) return 'bg-amber-dim';
		if (t < T_DOCTRINE) return 'bg-amber';
		return 'bg-green';
	}

	// Evidence drawer instance — bound via `bind:this`. The drawer owns its own
	// open/loading/content/error FSM, the synced-doc fetch, the sanitized
	// markdown pipeline, and Esc-to-close; the row buttons just call
	// `evidenceDrawer.open(path)`. See $lib/components/EvidenceDrawer.svelte.
	let evidenceDrawer: EvidenceDrawer;

	// Hash auto-expand. When a reader lands on /experiments#P14 (deep-linked
	// from a postmortem markdown / commit body / external doc), open the
	// matching paradigm row's <details> and scroll it into view — otherwise
	// the row's detail fields stay collapsed (P0.1 default) and the deep link
	// loses its meaning.
	//
	// Match the fragment to <article id="..."> and toggle the nested <details>
	// open. Re-run on `hashchange` so client-side anchor navigation behaves
	// the same as initial load.
	function expandRowForHash() {
		const id = location.hash.slice(1);
		if (!id) return;
		const article = document.getElementById(id);
		if (!article) return;
		const det = article.querySelector('details');
		if (det && !det.open) det.open = true;
		// Defer scroll to next frame so the just-opened details has its final
		// height before the browser computes the target scroll offset.
		requestAnimationFrame(() => {
			article.scrollIntoView({ block: 'start', behavior: 'instant' });
		});
	}

	function onHashChange() {
		expandRowForHash();
	}

	// Run once after hydration so the initial-load hash is honoured (the
	// hashchange listener below only fires on subsequent changes, not on
	// first paint). $effect inherently runs after the DOM is wired up.
	$effect(() => {
		expandRowForHash();
	});

	// Sticky TOC — section anchor list rendered on xl+ screens, with the
	// currently-visible section highlighted via IntersectionObserver. The
	// items match the in-page <section id="..."> anchors created above.
	const TOC_ITEMS = [
		{ id: 'status', label: 'status.legend' },
		{ id: 'how-to-read', label: 'how.to.read' },
		{ id: 'paradigms', label: 'paradigms.ledger' },
		{ id: 'patterns', label: 'failure.patterns' },
		{ id: 'infra', label: 'infrastructure.live' },
		{ id: 'methodology', label: 'methodology.artifacts' },
		{ id: 'glossary', label: 'glossary.terms' }
	];
	let activeSection = $state<string>('status');

	$effect(() => {
		// Bail on SSR / when DOM isn't ready. IntersectionObserver lives only
		// in browser env; $effect doesn't run server-side anyway, but the
		// guard makes the code unit-testable without jsdom.
		if (typeof IntersectionObserver === 'undefined') return;
		// rootMargin pulls the active boundary up so a section counts as
		// "visible" once its top hits ~33% from the viewport top — feels more
		// natural than the default "any pixel" rule for tall sections.
		const io = new IntersectionObserver(
			(entries) => {
				// When the user scrolls fast, the callback can receive several
				// intersecting entries in a single batch — picking any one of
				// them caused activeSection to flicker as the array order is
				// not document-order. Filter to intersecting only and take the
				// last (which is the section deepest into the viewport).
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

<div class="max-w-[1400px] mx-auto px-3 sm:px-4 py-8 sm:py-10 xl:grid xl:grid-cols-[160px_minmax(0,1fr)] xl:gap-6">
	<!-- Section TOC rail (xl+ only). Sticky to the viewport top so the section
	     list stays visible regardless of scroll depth. Matches in-page section
	     ids; the IntersectionObserver wired in <script> updates activeSection
	     so the current section reads as the amber accent. -->
	<aside class="hidden xl:block">
		<nav aria-label="Section table of contents" class="sticky top-4 border border-grid bg-bg-1 px-3 py-3">
			<div class="text-[10px] uppercase tracking-widest text-fg-muted mb-2">// toc</div>
			<ul class="space-y-1 text-[11px]">
				{#each TOC_ITEMS as item}
					<li>
						<a
							href="#{item.id}"
							class="block py-0.5 hover:text-amber transition-colors"
							class:text-amber={activeSection === item.id}
							class:text-fg-dim={activeSection !== item.id}
						>{item.label}</a>
					</li>
				{/each}
			</ul>
		</nav>
	</aside>

	<div class="xl:min-w-0">
	<header class="mb-10 fade-up">
		<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted mb-2">// experiments</div>
		<h1 class="font-display font-bold text-2xl sm:text-3xl lg:text-4xl tracking-tight text-fg">
			Research <span class="text-amber">ledger</span>
		</h1>

		<!-- A: project context. Outsiders need to know which track this is + that
		     failure is the expected outcome. -->
		<div class="text-fg-dim mt-3 max-w-3xl text-sm leading-relaxed space-y-3">
			<p>
				AlphaLens runs two parallel research tracks. This page is the
				<span class="text-amber">paradigm-search</span> track — a systematic protocol for
				falsifying alpha hypotheses on US equities under pre-registration discipline. The
				<a href="/about" class="text-cyan hover:text-amber underline decoration-dotted underline-offset-2">other track</a>
				is the thematic event-driven research assistant (dashboard / briefs).
			</p>
			<p>
				Failure is the expected outcome — markets are largely efficient. The methodology bundle
				that survived the failures (<JargonTip {...tipProps('pre-registration ledger')}>pre-registration ledger</JargonTip>,
				<JargonTip {...tipProps('multi-phase audit')}>multi-phase audit</JargonTip>,
				<JargonTip {...tipProps('Bonferroni correction')}>Bonferroni</JargonTip>-correct
				multiple testing), <em>not</em> any individual paradigm, is the actual artifact. Below: every
				hypothesis we tested, how we tested it, why it didn't work, and the general lesson that
				came out of it.
			</p>
		</div>

		<div class="mt-5 flex flex-wrap gap-2 text-[10px] uppercase tracking-widest">
			<span class="px-2 py-1 border border-red text-red">{summaryCounts.FAIL} FAIL</span>
			<span class="px-2 py-1 border border-red text-red">{summaryCounts['SLIPPAGE-FAIL']} SLIPPAGE-FAIL</span>
			<span class="px-2 py-1 border border-magenta text-magenta">{summaryCounts.INCONCLUSIVE} INCONCLUSIVE</span>
			<span class="px-2 py-1 border border-cyan text-cyan">{summaryCounts['IN-FLIGHT']} IN-FLIGHT</span>
			<span class="px-2 py-1 border border-fg-muted text-fg-muted">{paradigms.length} total tests</span>
		</div>
	</header>

	<!-- C: status taxonomy legend. Five chips, defined. -->
	<section id="status" class="border border-grid bg-bg-1 mb-8 fade-up" style="animation-delay: 0.05s">
		<div class="px-4 sm:px-5 py-3 border-b border-grid text-[10px] uppercase tracking-widest text-fg-muted flex items-center justify-between">
			<h2 class="font-normal">status.legend</h2>
			<span class="text-fg-dim normal-case tracking-normal">what each verdict means</span>
		</div>
		<ul class="divide-y divide-grid">
			{#each statusLegend as s}
				<li class="px-4 sm:px-5 py-2.5 flex flex-wrap items-baseline gap-3 text-sm">
					<span class="px-1.5 py-0.5 border text-[10px] uppercase tracking-widest shrink-0 {statusTone(s.status)}">{s.status}</span>
					<span class="text-fg-dim text-xs sm:text-sm flex-1 min-w-0">
						{#each parseMarkup(s.definition) as seg}
							{#if seg.kind === 'term'}
								<JargonTip {...tipProps(seg.term)}>{seg.label}</JargonTip>
							{:else}
								{seg.text}
							{/if}
						{/each}
					</span>
				</li>
			{/each}
		</ul>
	</section>

	<!-- D: αt scale "how to read this" block. Sets up the mini-bars below. -->
	<section id="how-to-read" class="border border-grid bg-bg-1 mb-8 fade-up" style="animation-delay: 0.08s">
		<div class="px-4 sm:px-5 py-3 border-b border-grid text-[10px] uppercase tracking-widest text-fg-muted">
			<h2 class="font-normal inline">how.to.read</h2>
		</div>
		<div class="px-4 sm:px-5 py-3 text-sm text-fg-dim leading-relaxed">
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
	</section>

	<section id="paradigms" class="border border-grid bg-bg-1 mb-8 fade-up" style="animation-delay: 0.1s">
		<div class="px-4 sm:px-5 py-3 border-b border-grid text-[10px] uppercase tracking-widest text-fg-muted flex items-center justify-between">
			<h2 class="font-normal">paradigms.ledger</h2>
			<span class="text-fg-dim normal-case tracking-normal">{paradigms.length} rows · click each "show detail" for hypothesis / mechanism / outcome / lesson</span>
		</div>
		<div class="divide-y divide-grid">
			{#each paradigms as p}
				<article id={p.id} class="px-4 sm:px-5 py-4 hover:bg-bg-2 transition-colors">
					<header class="flex flex-wrap items-baseline gap-2 sm:gap-3 mb-3">
						<span class="font-display font-bold text-base sm:text-lg text-amber w-10 sm:w-12 shrink-0">{p.display}</span>
						<h3 class="font-bold text-fg text-sm sm:text-base">{p.name}</h3>
						<span class="text-[10px] uppercase tracking-widest">
							<!-- Two-axis layer tag. Renders Layer · A / B (or A × B+B for
							     compounds). Each token (layer_id, axis_a, axis_b) is wrapped
							     in a JargonTip so first-time readers can hover for the inline
							     definition — replaces the upfront architecture.layers primer.
							     `axis_a` = screener is the default structural choice —
							     rendered muted to subordinate it visually so combo/compound/
							     gate read louder. Layer 4 (overlay) uses axis_a only, no
							     axis_b. -->
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
						<span class="ml-auto flex items-center gap-2">
							<span class="px-1.5 py-0.5 border text-[10px] uppercase tracking-widest {statusTone(p.status)}">{p.status}</span>
							<span class="text-[10px] uppercase tracking-widest text-fg-muted whitespace-nowrap">{p.date}</span>
						</span>
					</header>

					<p class="text-sm text-fg leading-relaxed mb-3 sm:pl-12 italic">{p.story}</p>

					{#if p.is_t !== null || p.oos_t !== null}
						<div class="sm:pl-12 mb-3 text-[11px]">
							<div class="flex items-center gap-2 mb-1">
								<span class="w-10 text-fg-muted uppercase tracking-widest">
									<JargonTip {...tipProps('IS')}>IS</JargonTip>
								</span>
								<div class="relative h-2 flex-1 bg-bg-3 overflow-hidden">
									{#if p.is_t !== null}
										<div class="absolute inset-y-0 left-0 {tBarTone(p.is_t)}" style="width: {tBarWidthPct(p.is_t)}%"></div>
									{/if}
									<div class="absolute inset-y-0 border-l border-grid-strong" style="left: {(T_MARGINAL / T_SCALE_MAX) * 100}%"></div>
									<div class="absolute inset-y-0 border-l border-green" style="left: {(T_DOCTRINE / T_SCALE_MAX) * 100}%"></div>
								</div>
								<span class="w-14 text-right font-mono text-fg">{p.is_t === null ? '—' : (p.is_t >= 0 ? '+' : '') + p.is_t.toFixed(2)}</span>
							</div>
							<div class="flex items-center gap-2">
								<span class="w-10 text-fg-muted uppercase tracking-widest">
									<JargonTip {...tipProps('OOS')}>OOS</JargonTip>
								</span>
								<div class="relative h-2 flex-1 bg-bg-3 overflow-hidden">
									{#if p.oos_t !== null}
										<div class="absolute inset-y-0 left-0 {tBarTone(p.oos_t)}" style="width: {tBarWidthPct(p.oos_t)}%"></div>
									{/if}
									<div class="absolute inset-y-0 border-l border-grid-strong" style="left: {(T_MARGINAL / T_SCALE_MAX) * 100}%"></div>
									<div class="absolute inset-y-0 border-l border-green" style="left: {(T_DOCTRINE / T_SCALE_MAX) * 100}%"></div>
								</div>
								<span class="w-14 text-right font-mono text-fg">{p.oos_t === null ? '—' : (p.oos_t >= 0 ? '+' : '') + p.oos_t.toFixed(2)}</span>
							</div>
						</div>
					{/if}

					<!-- Detail fields (hypothesis / mechanism / metric / lesson / evidence)
					     collapsed by default. Header + story + IS/OOS mini-bar stay
					     always visible above so the row is glanceable; user opts in to
					     technical detail per row. Mirrors the brief CandidateCard
					     details-toggle pattern (see CandidateCard.svelte:251). -->
					<details class="sm:ml-12 group/details">
						<summary class="text-[10px] uppercase tracking-widest text-fg-muted hover:text-amber cursor-pointer flex items-center gap-2 select-none list-none [&::-webkit-details-marker]:hidden py-1.5">
							<span class="text-amber transition-transform inline-block group-open/details:rotate-90">▸</span>
							<span class="group-open/details:hidden">show detail</span>
							<span class="hidden group-open/details:inline">hide detail</span>
						</summary>
						<dl class="text-xs sm:text-sm text-fg-dim space-y-1.5 pt-1.5">
							<div class="flex gap-2">
								<dt class="text-cyan font-bold w-20 sm:w-24 shrink-0">Hypothesis</dt>
								<dd>{#each parseMarkup(p.hypothesis) as seg}{#if seg.kind === 'term'}<JargonTip {...tipProps(seg.term)}>{seg.label}</JargonTip>{:else}{seg.text}{/if}{/each}</dd>
							</div>
							<div class="flex gap-2">
								<dt class="text-cyan font-bold w-20 sm:w-24 shrink-0">Mechanism</dt>
								<dd>{#each parseMarkup(p.mechanism) as seg}{#if seg.kind === 'term'}<JargonTip {...tipProps(seg.term)}>{seg.label}</JargonTip>{:else}{seg.text}{/if}{/each}</dd>
							</div>
							<div class="flex gap-2">
								<dt class="text-cyan font-bold w-20 sm:w-24 shrink-0">Outcome</dt>
								<dd class="text-fg">{#each parseMarkup(p.metric) as seg}{#if seg.kind === 'term'}<JargonTip {...tipProps(seg.term)}>{seg.label}</JargonTip>{:else}{seg.text}{/if}{/each}</dd>
							</div>
							<div class="flex gap-2">
								<dt class="text-cyan font-bold w-20 sm:w-24 shrink-0">Lesson</dt>
								<dd>{#each parseMarkup(p.lesson) as seg}{#if seg.kind === 'term'}<JargonTip {...tipProps(seg.term)}>{seg.label}</JargonTip>{:else}{seg.text}{/if}{/each}</dd>
							</div>
							{#if p.evidence}
								<div class="flex gap-2">
									<dt class="text-cyan font-bold w-20 sm:w-24 shrink-0">Evidence</dt>
									<dd>
										<button
											type="button"
											class="font-mono text-[11px] text-cyan hover:text-amber underline decoration-dotted underline-offset-2 break-all text-left"
											onclick={() => evidenceDrawer.open(p.evidence!)}
											aria-label="open evidence: {p.evidence}"
										>
											{p.evidence} ↗
										</button>
									</dd>
								</div>
							{/if}
						</dl>
					</details>
				</article>
			{/each}
		</div>
	</section>

	<section id="patterns" class="border border-grid bg-bg-1 mb-8 fade-up" style="animation-delay: 0.15s">
		<div class="px-4 sm:px-5 py-3 border-b border-grid text-[10px] uppercase tracking-widest text-fg-muted flex items-center justify-between">
			<h2 class="font-normal">failure.patterns</h2>
			<span class="text-fg-dim normal-case tracking-normal">{patterns.length} reusable lessons · hover dotted terms for definitions</span>
		</div>
		<ul class="divide-y divide-grid">
			{#each patterns as p}
				<li class="px-4 sm:px-5 py-3 text-sm flex gap-3">
					<span class="text-amber font-display font-bold w-6 shrink-0">{p.n}</span>
					<div>
						<h3 class="font-bold text-fg">
							{#each parseMarkup(p.name) as seg}
								{#if seg.kind === 'term'}
									<JargonTip {...tipProps(seg.term)}>{seg.label}</JargonTip>
								{:else}
									{seg.text}
								{/if}
							{/each}
						</h3>
						<div class="text-fg-dim text-xs mt-0.5 leading-relaxed">
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
	</section>

	<section id="infra" class="border border-grid bg-bg-1 mb-8 fade-up" style="animation-delay: 0.2s">
		<div class="px-4 sm:px-5 py-3 border-b border-grid text-[10px] uppercase tracking-widest text-fg-muted flex items-center justify-between">
			<h2 class="font-normal">infrastructure.live</h2>
			<span class="text-fg-dim normal-case tracking-normal">{live.length} tracks · what is currently running</span>
		</div>
		<table class="w-full text-sm">
			<tbody>
				{#each live as l}
					<tr class="border-b border-grid last:border-b-0 hover:bg-bg-2">
						<td class="px-4 sm:px-5 py-3 w-14 sm:w-20 align-top">
							<div class="font-display font-bold text-base sm:text-lg text-amber">{l.id}</div>
						</td>
						<td class="px-2 py-3 align-top">
							<div class="font-bold text-fg">{l.name}</div>
							<div class="text-fg-dim text-xs mt-0.5">{l.what}</div>
							<div class="sm:hidden text-[10px] uppercase tracking-widest mt-1 flex flex-wrap gap-2">
								<span class="px-1.5 py-0.5 border {statusTone(l.status)}">{l.status}</span>
								<span class="text-fg-muted normal-case tracking-normal">{l.deploy}</span>
							</div>
						</td>
						<td class="hidden sm:table-cell px-2 py-3 align-top">
							<span class="px-1.5 py-0.5 border text-[10px] uppercase tracking-widest {statusTone(l.status)}">{l.status}</span>
						</td>
						<td class="hidden sm:table-cell px-4 sm:px-5 py-3 text-right align-top text-xs text-fg-muted">{l.deploy}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</section>

	<section id="methodology" class="border border-grid bg-bg-1 mb-8 fade-up" style="animation-delay: 0.25s">
		<div class="px-4 sm:px-5 py-3 border-b border-grid text-[10px] uppercase tracking-widest text-fg-muted flex items-center justify-between">
			<h2 class="font-normal">methodology.artifacts</h2>
			<span class="text-fg-dim normal-case tracking-normal">{artifacts.length} items · what survived</span>
		</div>
		<table class="w-full text-sm">
			<tbody>
				{#each artifacts as a}
					<tr class="border-b border-grid last:border-b-0 hover:bg-bg-2">
						<td class="px-4 sm:px-5 py-3 w-14 sm:w-20 align-top">
							<div class="font-display font-bold text-base sm:text-lg text-amber">{a.id}</div>
						</td>
						<td class="px-2 py-3 align-top">
							<div class="font-bold text-fg">{a.name}</div>
							<div class="text-fg-dim text-xs mt-0.5 leading-relaxed">{a.description}</div>
							<div class="sm:hidden text-[10px] uppercase tracking-widest mt-1 flex flex-wrap gap-2">
								<span class="px-1.5 py-0.5 border {statusTone(a.status)}">{a.status}</span>
								<span class="text-cyan normal-case tracking-normal break-all">{a.link}</span>
							</div>
						</td>
						<td class="hidden sm:table-cell px-2 py-3 align-top">
							<span class="px-1.5 py-0.5 border text-[10px] uppercase tracking-widest {statusTone(a.status)}">{a.status}</span>
						</td>
						<td class="hidden sm:table-cell px-4 sm:px-5 py-3 text-right align-top text-xs text-cyan font-mono break-all max-w-[260px]">{a.link}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</section>

	<!-- B3: glossary section. Non-quant-friendly definitions of the jargon used
	     throughout this page. Acronyms also referenced via inline JargonTip in
	     the "how.to.read" block above — the inline tooltips are the primary
	     reference, this <details> is the secondary lookup table. Brief-only
	     terms (PE, PS, EV/EBITDA, etc.) are filtered out so the table matches
	     terms actually used on /experiments. -->
	<section id="glossary" class="border border-grid bg-bg-1 fade-up" style="animation-delay: 0.3s">
		<details class="group/glossary">
			<summary class="px-4 sm:px-5 py-3 border-b border-grid text-[10px] uppercase tracking-widest text-fg-muted hover:bg-bg-2 cursor-pointer flex items-center justify-between list-none [&::-webkit-details-marker]:hidden">
				<span class="flex items-center gap-2">
					<span class="text-amber transition-transform inline-block group-open/glossary:rotate-90">▸</span>
					<h2 class="font-normal">glossary.terms</h2>
				</span>
				<span class="text-fg-dim normal-case tracking-normal">{experimentsGlossary.length} terms · click to expand · hover dotted-underlined inline terms above for primary reference</span>
			</summary>
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
		</details>
	</section>
	</div>
</div>

<EvidenceDrawer bind:this={evidenceDrawer} />
