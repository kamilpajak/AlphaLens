"""Live GDELT smoke test — opt-in via ``GDELT_LIVE_TEST=1``.

Catches YAML query bugs (single-word quoted phrases, malformed boolean
combos) that pass static lint but fail at the API. Each bucket gets one real
HTTP request with ``timespan=1h`` (GDELT v2 minimum) so wall time is short
and the result volume is small. Run with::

    GDELT_LIVE_TEST=1 .venv/bin/python -m unittest tests.thematic.test_gdelt_live -v

Inter-query sleep respects GDELT's 5s/req soft limit. Total wall time scales
linearly with bucket count (~10s/bucket × 9 buckets ≈ 90s today). Not part of
the default unittest discover run because it depends on the live API and
spends real rate-limit budget.
"""

from __future__ import annotations

import json
import os
import time
import unittest
import urllib.error
import urllib.request

from alphalens_research.thematic.sources import gdelt


@unittest.skipUnless(
    os.environ.get("GDELT_LIVE_TEST"), "set GDELT_LIVE_TEST=1 to run live API smoke"
)
class TestGdeltLiveSmoke(unittest.TestCase):
    """Catch GDELT *permanent* (query-malformed) failures via live calls.

    Transient failures (SSL handshake timeouts, HTTP 429s) are categorised
    separately and only fail the test if they dominate (>50% of buckets) —
    otherwise they're logged as warnings. The narrow goal here is "does any
    bucket query produce a plain-text 'permanent error' body from GDELT",
    which static YAML lint cannot prove and which is what historically
    caused silent daily-cache degradation.
    """

    def test_no_bucket_returns_gdelt_permanent_error(self):
        buckets = gdelt.load_theme_buckets()
        self.assertGreater(len(buckets), 0)
        permanent: list[tuple[str, str]] = []
        transient: list[tuple[str, str]] = []
        ok: list[str] = []
        for i, (theme, query) in enumerate(buckets.items()):
            if i > 0:
                time.sleep(gdelt.DEFAULT_INTER_QUERY_SLEEP_SEC)
            url = gdelt.build_query_url(query=query, timespan="1h", maxrecords=5)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "AlphaLens-thematic/0.1"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    body = r.read()
            except urllib.error.HTTPError as exc:
                transient.append((theme, f"HTTP {exc.code}: {exc.reason}"))
                continue
            except urllib.error.URLError as exc:
                transient.append((theme, f"URL: {exc}"))
                continue
            if not body:
                transient.append((theme, "empty body"))
                continue
            if body[:1] not in (b"{", b"["):
                snippet = body[:160].decode("utf-8", errors="replace").strip()
                permanent.append((theme, snippet))
                continue
            try:
                json.loads(body)
                ok.append(theme)
            except json.JSONDecodeError as exc:
                permanent.append((theme, f"JSON parse: {exc}"))

        print(
            f"\n[gdelt live smoke] ok={len(ok)} transient={len(transient)} "
            f"permanent={len(permanent)} (total={len(buckets)})"
        )
        for t, msg in transient:
            print(f"  TRANSIENT {t}: {msg}")
        for t, msg in permanent:
            print(f"  PERMANENT {t}: {msg}")

        self.assertEqual(
            permanent,
            [],
            f"Live GDELT smoke caught permanent (query-malformed) failures: {permanent}",
        )
        # Defence against silent degradation: at least half the buckets must round-trip.
        # If majority transient-fail, IP-level rate-limit / network is so degraded the
        # smoke proves nothing — flag it loudly rather than passing on near-empty data.
        self.assertGreaterEqual(
            len(ok),
            len(buckets) // 2,
            f"Only {len(ok)}/{len(buckets)} buckets succeeded — transient failures dominate "
            f"({transient}); re-run after rate-limit cool-down before trusting this smoke",
        )


if __name__ == "__main__":
    unittest.main()
