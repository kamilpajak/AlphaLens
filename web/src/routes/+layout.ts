// Pure SPA: nginx serves a single index.html shell and the client fetches
// /api/v1/* from the FastAPI service at runtime. This is what lets the
// daily pipeline's brief refresh surface without rebuilding the docker
// image.
export const ssr = false;
export const prerender = false;
