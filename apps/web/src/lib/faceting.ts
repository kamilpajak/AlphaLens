// Shared faceted-filter core — pure, framework-free, domain-neutral. Both the
// /edge outcomes toolbar and the two /experiments ledgers derive facet counts,
// test facet membership, and build the `LedgerFilterBar` chip arrays the same
// way; this module is the single home for that logic so the four sites cannot
// drift. Domain-specific pieces (the EdgeOutcome predicate, URL round-trip) stay
// in their own modules and consume these primitives.

/** A distinct facet value with how many rows carry it. */
export interface FacetOption {
	/** The raw value (empty string for null/missing is dropped by `deriveFacet`). */
	key: string;
	count: number;
}

/** A single chip for `LedgerFilterBar`: its key + label, the row count it covers,
 *  a Tailwind `text-* border-*` tone, and the plain-text `ChipTip` definition. */
export interface FilterChip {
	key: string;
	label: string;
	count: number;
	tone: string;
	def: string;
}

/** Normalize a nullable value to the empty-string bucket so null/missing values
 *  are a single, matchable facet key rather than scattered nullish holes. */
export function facetValue(value: string | null | undefined): string {
	return value ?? '';
}

/** Distinct values of `pick` over `rows` with their counts, in descending-count
 *  then key order (stable, deterministic). The empty-string bucket is dropped so
 *  a facet with only missing values contributes no chip — a consequence is that
 *  rows with a missing value (e.g. a PENDING/blank classification) get no chip
 *  and so can only be excluded by a facet selection, never isolated. */
export function deriveFacet<T>(rows: T[], pick: (o: T) => string | null | undefined): FacetOption[] {
	const counts = new Map<string, number>();
	for (const o of rows) {
		const k = facetValue(pick(o));
		if (k === '') continue;
		counts.set(k, (counts.get(k) ?? 0) + 1);
	}
	return [...counts.entries()]
		.map(([key, count]) => ({ key, count }))
		.sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
}

/** Whether a row's value passes ONE facet: an empty selection imposes no
 *  constraint (all pass); otherwise the value must be in the selection. This is
 *  the union-within-a-facet half of faceted-search semantics. */
export function facetMatches(selected: Set<string>, value: string | null | undefined): boolean {
	return selected.size === 0 || selected.has(facetValue(value));
}

/** Config for the leading "all" chip (the one that clears the selection). */
export interface AllChipConfig {
	count: number;
	/** Key that `LedgerFilterBar` treats as the clear-all chip (default 'ALL'). */
	key?: string;
	label?: string;
	def?: string;
	tone?: string;
}

export interface BuildChipsConfig {
	all: AllChipConfig;
	label: (key: string) => string;
	tone: (key: string) => string;
	def: (key: string) => string;
}

/** Build the `LedgerFilterBar` chip array: the leading "all" chip followed by one
 *  chip per facet option, IN THE GIVEN ORDER (the caller decides — count-desc via
 *  `deriveFacet`, or a curated legend order). The label/tone/def callbacks map
 *  each facet key to its display fields. */
export function buildFilterChips(facet: FacetOption[], cfg: BuildChipsConfig): FilterChip[] {
	const all: FilterChip = {
		key: cfg.all.key ?? 'ALL',
		label: cfg.all.label ?? 'all',
		count: cfg.all.count,
		tone: cfg.all.tone ?? 'text-fg border-fg-muted',
		def: cfg.all.def ?? 'Show all.'
	};
	return [
		all,
		...facet.map((f) => ({
			key: f.key,
			label: cfg.label(f.key),
			count: f.count,
			tone: cfg.tone(f.key),
			def: cfg.def(f.key)
		}))
	];
}
