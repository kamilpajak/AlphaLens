<script lang="ts">
	import { marked } from 'marked';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	// Build a flat list of (level, text, slug) tuples for the TOC sidebar
	// before handing the markdown to `marked` for full-document rendering.
	// Slug rule: lowercase, alphanumerics + spaces collapse to '-' — matches
	// the default GitHub-style anchor convention so deep-link copy works.
	interface TocEntry {
		level: 2 | 3;
		text: string;
		slug: string;
	}

	function slugify(text: string): string {
		return text
			.toLowerCase()
			.replace(/[^a-z0-9\s-]/g, '')
			.trim()
			.replace(/\s+/g, '-');
	}

	function extractToc(md: string): TocEntry[] {
		const toc: TocEntry[] = [];
		for (const line of md.split('\n')) {
			const m = line.match(/^(##|###)\s+(.+?)\s*$/);
			if (!m) continue;
			const level = m[1].length as 2 | 3;
			const text = m[2];
			toc.push({ level, text, slug: slugify(text) });
		}
		return toc;
	}

	// Register a renderer override so every <h2> / <h3> the document emits
	// gets an id attribute matching the TOC's slug. Without this the
	// anchor links from the sidebar would fall through to no-op (default
	// `marked` doesn't slug headings).
	function buildRenderer() {
		const renderer = new marked.Renderer();
		renderer.heading = ({ tokens, depth }) => {
			const text = renderer.parser.parseInline(tokens);
			const slug = slugify(text.replace(/<[^>]+>/g, ''));
			return `<h${depth} id="${slug}">${text}</h${depth}>`;
		};
		return renderer;
	}

	const toc = $derived(extractToc(data.markdown));
	const html = $derived(
		marked.parse(data.markdown, { renderer: buildRenderer(), gfm: true }) as string
	);

	let activeSlug = $state<string | null>(null);

	// Smooth-scroll on TOC click. Avoid hash-routing (would push history
	// state every click) — explicit element.scrollIntoView keeps the URL
	// stable while updating the highlighted entry.
	function jumpTo(slug: string) {
		const el = document.getElementById(slug);
		if (!el) return;
		el.scrollIntoView({ behavior: 'smooth', block: 'start' });
		activeSlug = slug;
	}
</script>

<svelte:head>
	<title>AlphaLens — Ideal Shape</title>
</svelte:head>

<div class="max-w-[1400px] mx-auto px-3 sm:px-4 py-6">
	<header class="border border-grid bg-bg-1 corners relative fade-up mb-5">
		<div class="px-4 sm:px-6 py-5">
			<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted">// north star</div>
			<h1
				class="font-display font-bold text-3xl sm:text-4xl lg:text-5xl text-amber tracking-tight mt-1"
			>
				ideal shape
			</h1>
			<p class="text-fg-dim text-xs sm:text-sm mt-3 max-w-2xl leading-relaxed">
				Direction AlphaLens is already walking PR-by-PR — not a rewrite. 3-tier interaction
				model, feedback loop, 8 tracks. Source of truth dla
				<span class="text-amber">dlaczego</span> każdego następnego feature'a.
			</p>
		</div>
	</header>

	<div class="grid grid-cols-1 lg:grid-cols-12 gap-6">
		<!-- TOC sidebar -->
		<aside
			class="lg:col-span-3 lg:sticky lg:top-6 lg:self-start fade-up"
			style="animation-delay: 0.05s"
		>
			<div class="border border-grid bg-bg-1 p-4">
				<div class="text-[10px] uppercase tracking-widest text-cyan mb-3">// contents</div>
				<nav class="flex flex-col gap-1">
					{#each toc as entry (entry.slug)}
						<button
							type="button"
							onclick={() => jumpTo(entry.slug)}
							class="text-left text-xs leading-snug text-fg-dim hover:text-amber transition-colors py-1 cursor-pointer"
							class:pl-0={entry.level === 2}
							class:pl-4={entry.level === 3}
							class:text-amber={activeSlug === entry.slug}
							data-testid="vision-toc-entry"
						>
							{entry.text}
						</button>
					{/each}
				</nav>
			</div>
		</aside>

		<!-- Rendered markdown -->
		<article
			class="lg:col-span-9 border border-grid bg-bg-1 px-5 sm:px-8 py-6 fade-up
				prose prose-invert max-w-none
				prose-headings:font-display prose-headings:text-amber prose-headings:tracking-tight
				prose-h1:text-3xl prose-h2:text-2xl prose-h2:mt-10 prose-h2:mb-4 prose-h2:border-b prose-h2:border-grid prose-h2:pb-2
				prose-h3:text-lg prose-h3:text-cyan prose-h3:mt-6 prose-h3:mb-3
				prose-p:text-fg prose-p:leading-relaxed
				prose-a:text-cyan prose-a:no-underline hover:prose-a:text-amber
				prose-strong:text-amber prose-strong:font-bold
				prose-code:text-cyan prose-code:bg-bg-2 prose-code:px-1 prose-code:rounded prose-code:text-xs prose-code:before:content-none prose-code:after:content-none
				prose-pre:bg-bg-2 prose-pre:border prose-pre:border-grid prose-pre:text-xs
				prose-blockquote:border-l-violet prose-blockquote:text-fg-dim prose-blockquote:font-normal prose-blockquote:not-italic
				prose-ul:text-fg prose-li:my-1
				prose-table:text-xs prose-th:text-amber prose-th:border-grid prose-td:border-grid prose-td:text-fg-dim
				prose-hr:border-grid"
			style="animation-delay: 0.1s"
			data-testid="vision-content"
		>
			{@html html}
		</article>
	</div>
</div>
