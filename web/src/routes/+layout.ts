// Pure SPA: nginx serves a single index.html shell and the client fetches
// /data/*.json at runtime. This is what lets pipeline-rewritten briefs surface
// without rebuilding the docker image.
export const ssr = false;
export const prerender = false;
