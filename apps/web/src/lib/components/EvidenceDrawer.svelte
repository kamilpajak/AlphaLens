<script lang="ts">
	// Evidence drawer — a right-side modal that fetches a synced research doc
	// from `/docs/research/<path>`, renders it as sanitized HTML, and shows it.
	//
	// Extracted from `routes/experiments/+page.svelte` so the route stays a thin
	// view layer. The drawer owns its full FSM (open / loading / content /
	// error), the per-session HTML cache, and the Esc-to-close key handling.
	// The parent opens it imperatively via `bind:this`:
	//
	//   let drawer: EvidenceDrawer;
	//   <EvidenceDrawer bind:this={drawer} />
	//   <button onclick={() => drawer.open('foo.md')}>…</button>
	//
	// Markdown / JSON go through `$lib/markdown`, which sanitizes the output
	// before it reaches the `{@html}` sink (see that module for the XSS rationale).

	import { renderJson, renderMarkdown } from '$lib/markdown';

	let drawerOpen = $state(false);
	let drawerPath = $state('');
	let drawerContent = $state('');
	let drawerLoading = $state(false);
	let drawerError = $state<string | null>(null);

	// Cache parsed evidence HTML across openings in the same session. The
	// referenced research files are static (synced at build time via
	// scripts/sync-research-docs.mjs) so repeat fetches add no information,
	// just network round-trips and marked.parse() CPU work.
	const evidenceCache = new Map<string, string>();

	export async function open(path: string) {
		drawerPath = path;
		drawerOpen = true;
		drawerError = null;
		const cached = evidenceCache.get(path);
		if (cached !== undefined) {
			drawerContent = cached;
			drawerLoading = false;
			return;
		}
		drawerLoading = true;
		drawerContent = '';
		try {
			const url = `/docs/research/${path}`;
			const resp = await fetch(url);
			if (!resp.ok) {
				drawerError = `Failed to load ${path} (HTTP ${resp.status})`;
				return;
			}
			const text = await resp.text();
			// Sanitize before the `{@html}` sink — research docs are hand-edited
			// content synced at build time, so a stray inline `<script>` /
			// `onerror=` would otherwise become stored XSS. See $lib/markdown.
			const parsed = path.endsWith('.json')
				? await renderJson(text)
				: await renderMarkdown(text);
			drawerContent = parsed;
			evidenceCache.set(path, parsed);
		} catch (e) {
			drawerError = `Network error: ${(e as Error).message}`;
		} finally {
			drawerLoading = false;
		}
	}

	function closeDrawer() {
		drawerOpen = false;
	}

	function onDrawerKey(e: KeyboardEvent) {
		// Guard on drawerOpen so the global listener is a no-op when drawer
		// is closed (Svelte 5 disallows conditional <svelte:window>).
		if (!drawerOpen) return;
		if (e.key === 'Escape') closeDrawer();
	}
</script>

<!-- Window-level Esc handler. Must be at component root per Svelte 5 (no
     conditional <svelte:window>); handler internally guards on drawerOpen
     so it's a no-op when the drawer is closed. -->
<svelte:window onkeydown={onDrawerKey} />

{#if drawerOpen}
	<div class="fixed inset-0 z-50 flex" role="presentation">
		<button
			type="button"
			class="absolute inset-0 bg-bg/85 backdrop-blur-sm"
			onclick={closeDrawer}
			aria-label="close evidence drawer"
		></button>
		<aside
			class="relative ml-auto h-full w-full max-w-3xl bg-bg-1 border-l border-grid overflow-y-auto"
			role="dialog"
			aria-modal="true"
			aria-label="evidence"
		>
			<header class="sticky top-0 z-10 bg-bg-1 border-b border-grid px-4 sm:px-6 py-3 flex items-center gap-3">
				<div class="text-[10px] uppercase tracking-[0.3em] text-fg-muted">// evidence</div>
				<div class="font-mono text-xs text-cyan break-all">{drawerPath}</div>
				<button
					type="button"
					class="ml-auto px-3 py-1 text-[11px] uppercase tracking-widest text-fg hover:text-amber transition-colors"
					onclick={closeDrawer}
					aria-label="close"
				>
					[esc] close
				</button>
			</header>
			<div class="px-4 sm:px-6 py-4">
				{#if drawerLoading}
					<div class="text-fg-dim text-sm">loading…</div>
				{:else if drawerError}
					<div class="text-red text-sm">{drawerError}</div>
				{:else}
					<div class="prose prose-invert prose-sm sm:prose-base max-w-none">{@html drawerContent}</div>
				{/if}
			</div>
		</aside>
	</div>
{/if}
