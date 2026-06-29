#!/usr/bin/env node
// Dev-only: serve the SPA's `/v1/*` API from the real (Cloudflare-Access-protected)
// production API, so `pnpm dev:vps` renders the latest LIVE brief locally instead
// of hand-authored fixtures.
//
// How it fits together:
//   browser → vite dev (`/api/v1/*`, proxy strips `/api`) → this proxy (`/v1/*`)
//           → upstream `$DEV_API_URL/v1/*` with a `cf-access-token` header
//
// Auth: a short-lived Cloudflare Access token from `cloudflared access token`.
// Run ONCE to obtain it (opens a browser for Google SSO):
//   cloudflared access login --app=$DEV_API_URL
//
// Config (env or apps/web/.env — gitignored, so your domain stays out of the repo):
//   DEV_API_URL          required, e.g. https://api.<your-domain>
//   DEV_API_PROXY_PORT   optional, default 8099
import http from 'node:http';
import { execFileSync, spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';

// Minimal .env reader (cwd is apps/web when pnpm runs this script). We only need
// the two keys below; no dependency on a dotenv package.
function fromEnvFile(key) {
	try {
		const line = readFileSync('.env', 'utf8')
			.split('\n')
			.find((l) => l.trim().startsWith(`${key}=`));
		return line ? line.slice(line.indexOf('=') + 1).trim().replace(/^["']|["']$/g, '') : '';
	} catch {
		return '';
	}
}

const API = (process.env.DEV_API_URL || fromEnvFile('DEV_API_URL')).replace(/\/+$/, '');
const PORT = Number(process.env.DEV_API_PROXY_PORT || fromEnvFile('DEV_API_PROXY_PORT') || 8099);

if (!API) {
	console.error(
		'[dev:vps] DEV_API_URL is not set.\n' +
			'  Add it to apps/web/.env, e.g.  DEV_API_URL=https://api.<your-domain>'
	);
	process.exit(1);
}

function fetchToken() {
	try {
		return execFileSync('cloudflared', ['access', 'token', `--app=${API}`], {
			encoding: 'utf8'
		}).trim();
	} catch {
		return '';
	}
}

let token = fetchToken();
if (!token) {
	console.error(
		`[dev:vps] No Cloudflare Access token for ${API}.\n` +
			`  Run once (opens a browser):  cloudflared access login --app=${API}`
	);
	process.exit(1);
}

// The dashboard is read-only (all `/v1/*` calls are GET), so we forward method +
// path only — no request body plumbing. A non-GET would 405 upstream, which is
// fine for a viewer.
async function proxy(req, res) {
	const url = API + req.url;
	const send = (tok) =>
		fetch(url, { method: req.method, headers: { 'cf-access-token': tok, accept: 'application/json' } });

	let upstream = await send(token);
	// Token TTL is ~24h; if it expired mid-session, refresh once and retry.
	if (upstream.status === 401) {
		const fresh = fetchToken();
		if (fresh) {
			token = fresh;
			upstream = await send(token);
		}
	}
	const body = Buffer.from(await upstream.arrayBuffer());
	res.writeHead(upstream.status, {
		'content-type': upstream.headers.get('content-type') || 'application/json'
	});
	res.end(body);
}

const server = http.createServer((req, res) => {
	proxy(req, res).catch((e) => {
		res.writeHead(502, { 'content-type': 'application/json' });
		res.end(JSON.stringify({ detail: `dev proxy error: ${e.message}` }));
	});
});

server.listen(PORT, '127.0.0.1', () => {
	console.log(`[dev:vps] proxying /v1/* → ${API}  (127.0.0.1:${PORT})`);
	// Proxy-only mode (no vite) — for a smoke check or pointing another tool at it.
	if (process.env.DEV_API_PROXY_ONLY) {
		console.log('[dev:vps] proxy-only mode (DEV_API_PROXY_ONLY); not launching vite');
		return;
	}
	// Launch the normal dev server pointed at this proxy. `pnpm run dev` keeps the
	// `predev` doc-sync hook; VITE_API_TARGET makes vite's /api proxy forward here.
	const child = spawn('pnpm', ['run', 'dev'], {
		stdio: 'inherit',
		env: { ...process.env, VITE_API_TARGET: `http://127.0.0.1:${PORT}` }
	});
	const shutdown = () => {
		try {
			child.kill('SIGINT');
		} catch {
			/* already gone */
		}
		server.close();
		process.exit(0);
	};
	child.on('exit', (code) => {
		server.close();
		process.exit(code ?? 0);
	});
	process.on('SIGINT', shutdown);
	process.on('SIGTERM', shutdown);
});
