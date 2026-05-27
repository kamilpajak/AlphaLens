// Dev-only mock API. Serves the Playwright api-mock fixtures over HTTP so
// `pnpm dev` shows real-shaped brief/candidate data (including OK trade-setup
// ladders) without standing up Django + Postgres.
//
// Vite proxies `/api/*` to VITE_API_TARGET (default http://127.0.0.1:8081),
// stripping the `/api` prefix, so this server answers the production contract:
//   GET /v1/days?limit=N      -> paginated index envelope
//   GET /v1/days/{YYYY-MM-DD}  -> full DayBrief (or 404)
//
// Run:  node scripts/dev-mock-api.mjs   (or: PORT=8081 node scripts/dev-mock-api.mjs)
// Then: pnpm dev   and open http://localhost:5173/

import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(__dirname, '..', 'tests', 'fixtures', 'api-mock');
const PORT = Number(process.env.PORT ?? 8081);

const DAYS_INDEX = JSON.parse(readFileSync(join(FIXTURES, 'days.json'), 'utf8'));
const DAYS_INDEX_BODY = JSON.stringify({
	data: DAYS_INDEX,
	meta: { total: DAYS_INDEX.length, limit: 200, offset: 0 }
});

function readDay(date) {
	try {
		return readFileSync(join(FIXTURES, 'days', `${date}.json`), 'utf8');
	} catch {
		return null;
	}
}

function json(res, status, body) {
	res.writeHead(status, { 'content-type': 'application/json' });
	res.end(body);
}

const server = createServer((req, res) => {
	const url = new URL(req.url, `http://localhost:${PORT}`);

	if (url.pathname === '/v1/days') {
		return json(res, 200, DAYS_INDEX_BODY);
	}

	const dayMatch = url.pathname.match(/^\/v1\/days\/(\d{4}-\d{2}-\d{2})$/);
	if (dayMatch) {
		const body = readDay(dayMatch[1]);
		if (body) return json(res, 200, body);
		return json(res, 404, JSON.stringify({ detail: `no brief for date=${dayMatch[1]}` }));
	}

	return json(res, 404, JSON.stringify({ detail: `unhandled mock path: ${url.pathname}` }));
});

server.listen(PORT, '127.0.0.1', () => {
	const dates = DAYS_INDEX.map((d) => d.date).join(', ');
	console.log(`[dev-mock-api] listening on http://127.0.0.1:${PORT}  (dates: ${dates})`);
});
