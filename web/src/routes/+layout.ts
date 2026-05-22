// Pure SPA: nginx serves a single index.html shell and the client fetches
// the briefs API at runtime through `$lib/api` (same-origin `/api/v1/*` by
// default, override with `VITE_API_BASE` for cross-origin deploys). This
// lets the daily pipeline's brief refresh surface without rebuilding the
// docker image.
export const ssr = false;
export const prerender = false;
