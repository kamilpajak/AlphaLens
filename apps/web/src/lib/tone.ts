// Single source of truth for the terminal-ops colour vocabulary. The paradigm
// `statusTone`, the `toolStatusTone`, and the /edge `toneClasses` all used to
// hand-roll their own `text-X border-X` maps in parallel (different class order,
// slightly different `muted` fallback). They now each map their domain enum to a
// `SemanticTone` and call `toneClass()` here, so a palette change is one edit.

export type SemanticTone = 'green' | 'red' | 'amber' | 'cyan' | 'violet' | 'magenta' | 'muted';
export type ToneVariant = 'text' | 'border' | 'bg';

// Per-tone class for each variant. `muted` is the neutral fallback: dim text on a
// stronger grid border (matches the prior /edge `toneClasses` muted).
const TONE_CLASSES: Record<SemanticTone, Record<ToneVariant, string>> = {
	green: { text: 'text-green', border: 'border-green', bg: 'bg-green' },
	red: { text: 'text-red', border: 'border-red', bg: 'bg-red' },
	amber: { text: 'text-amber', border: 'border-amber', bg: 'bg-amber' },
	cyan: { text: 'text-cyan', border: 'border-cyan', bg: 'bg-cyan' },
	violet: { text: 'text-violet', border: 'border-violet', bg: 'bg-violet' },
	magenta: { text: 'text-magenta', border: 'border-magenta', bg: 'bg-magenta' },
	muted: { text: 'text-fg-muted', border: 'border-grid-strong', bg: 'bg-fg-muted' }
};

export const TONE_KEYS = Object.keys(TONE_CLASSES) as SemanticTone[];

/** Compose the Tailwind classes for a semantic tone. `variants` selects and
 *  orders the pieces (default `['text', 'border']` — the chip vocabulary; /edge
 *  passes `['border', 'text']`; bars pass `['bg']`). */
export function toneClass(tone: SemanticTone, variants: ToneVariant[] = ['text', 'border']): string {
	return variants.map((v) => TONE_CLASSES[tone][v]).join(' ');
}
