// Build-time-rendered tooltip formulas, keyed by their src/lib/formulas.json
// name. Provided by the `virtualFormulas` plugin in vite.config.ts; the values
// are MathML strings produced by Temml in Node (never in the browser).
declare module 'virtual:formulas' {
	const formulas: Record<string, string>;
	export default formulas;
}
