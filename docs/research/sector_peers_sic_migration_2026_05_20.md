# `sector_peers` SimFin → EDGAR SIC migration (2026-05-20)

**Status:** LOCKED, implemented in branch `fix/sector-peers-sic-migration`, closes issue #169.

## Why this exists

PR #161 (merged 2026-05-20 09:55 UTC) deleted `alphalens/data/store/simfin.py`
and the `simfin>=1.0.2` dependency, but the project carried a **second,
independent** SimFin consumer that was missed: `alphalens/thematic/screening/sector_peers.py`
read `~/.alphalens/simfin_cache/{us-companies,industries}.csv` for ticker →
industry / sector resolution. That cache directory exists on hosts but was
never populated by any code path remaining in the repo (the writer was
SimFin's bulk-package downloader, gone with the SDK).

Result: the daily VPS thematic pipeline crashed at `alphalens thematic
score` with `FileNotFoundError: us-companies.csv` after PR #161 was merged.
Polygon + Gemini Flash + Gemini Pro budget was burned upstream before the
crash. No brief was generated for 2026-05-19 onward.

## Approach: SEC SIC codes as the new industry key

EDGAR's `submissions/CIK{cik}.json` endpoint returns top-level `sic`
(4-digit int) and `sicDescription` (str) for every filer. We pre-compute a
ticker→(cik, sic, sic_description) parquet once, ship it inside the
`alphalens` Python package, and rebuild it manually (~monthly) when SIC
reassignments accumulate.

### Discovery correction

The original Plan-agent design assumed SIC lived on the `companyfacts`
endpoint. Empirical probe 2026-05-20 showed companyfacts only carries
`{cik, entityName, facts}` — no `sic`. Switched to the `submissions`
endpoint (which also exposes `sicDescription`, `tickers`, `exchanges`,
`stateOfIncorporation`, and the full filing history).

### Public API kept stable

The screener's import surface is unchanged. `alphalens/thematic/screening/sector_peers.py`
is now a 25-line adapter that re-exports `get_industry_id` / `iter_industry_peers`
/ `industry_label` aliases pointing at the new SIC module — `scorer.py`
imports nothing different.

## Cohort-width trade-off

| Aspect | SimFin (old) | SEC SIC (new) |
|---|---|---|
| Granularity | 6-digit hierarchical IndustryId | 4-digit SIC |
| Quantum tickers | `101001` Quantum Computing, 4 peers | QUBT=`7372` Software, IONQ=`7373` Computer Integrated Systems — **different SIC codes, no longer peers under 4-digit match** |
| Semiconductors | sub-segmented | `3674` lumps ~100+ tickers |
| Service tickers | sub-segmented | Division-I scope, very wide |

Cohort widening AND cohort splitting both happen. The brief's
`sector_percentile` consumed by `insider_signal` / `fcff_signal` /
`valuation_signal` will be noisier:

- For broad cohorts (semiconductors, banks), percentile = "ranked against
  the whole sub-industry" — still informative.
- For tickers with no 4-digit SIC peers in the index (QUBT alone at 7372),
  percentile will be `nan`; downstream null-handling in
  `compose_weighted_score` already treats those as "signal absent".

Future refinement (out of scope for this PR): theme-conditional cohort —
peers = same SIC ∩ same theme tag in the candidates parquet. Captures
the quantum-cohort intuition SimFin had baked in, without manual taxonomy
work.

## Components

### New

- `alphalens/data/fundamentals/sic_index.py` — `get_sic`, `iter_sic_peers`,
  `sic_label`. Lazy parquet load via `lru_cache`. Hardcoded 10-entry
  SIC-range → SEC Division name map (A through J).
- `alphalens/data/fundamentals/sic_index.parquet` — checked-in artifact.
  Schema: `ticker:str, cik:str, sic:int32, sic_description:str`. ~10k rows
  for the full SEC ticker universe, ~80 KB on disk.
- `scripts/build_sic_index.py` — walks `alphalens/data/alt_data/data/ticker_cik_map.yaml`
  (~10k entries), calls `SecEdgarClient.fetch_submissions(cik)` per CIK,
  extracts SIC, writes the parquet. Wall ~30 min single-shot at 10 req/s.
- `tests/test_sic_index.py` — 24 unit tests: lookup, peers, label, division
  mapping, missing-file safety. Fixture: tmp-dir synthetic parquet.
- `docs/research/sector_peers_sic_migration_2026_05_20.md` — this memo.

### Edited

- `alphalens/thematic/screening/sector_peers.py` — gut SimFin loader, become
  a thin adapter over `sic_index`. Drop `SIMFIN_CACHE_DIR` constant.
- `tests/thematic/screening/test_sector_peers.py` — collapse to 4
  alias-identity tests (substantive contract is exercised in
  `tests/test_sic_index.py`).
- `tests/thematic/screening/test_scorer.py` — update mocked industry_id
  from `101001` → `3674` and labels to `("Semiconductors & Related Devices", "Manufacturing")`.
  Mock values are opaque; updated for realism.
- `alphalens/preaudit/profiles.py` — drop the `simfin_cache` DataDep from
  `EV_FCFF_YIELD_PROFILE`; the SIC index is a package resource, always
  present in a healthy install (no `~/.alphalens/<name>/` check needed).
- `scripts/experiment_ev_fcff_yield.py` — change `"Financial" in sector` →
  `"Finance" in sector` (SEC Division H label is "Finance, Insurance and
  Real Estate"). Update docstring.
- `scripts/run_ev_fcff_yield_audit.py` — drop `--simfin-data-dir` arg and
  `SIMFIN_DATA_DIR` env propagation.

## Operator action items

1. **One-time after merge**: rebuild the `alphalens-pipeline:latest` Docker
   image on VPS (`docker compose -f deploy/docker/docker-compose.yml build pipeline`).
   The new parquet ships inside the image — no host-side cache to provision.
2. **Optional monthly**: developer runs `.venv/bin/python scripts/build_sic_index.py`
   locally, commits the refreshed parquet, triggers image rebuild. SIC
   reassignments are rare; quarterly is also defensible.
3. **Cleanup**: the now-orphaned `~/.alphalens/simfin_cache/` directory on
   any host (VPS, Mac, runpod) can be `rm -rf`'d. No live code reads it.

## Verification

1. `tests/test_sic_index.py` — 24 unit tests green.
2. `tests/thematic/screening/test_*` — 4 sector-peers alias tests + 22
   scorer tests green.
3. Full repo suite `.venv/bin/python -m unittest discover tests` — pre-existing
   3280 tests stay green.
4. `tests/test_no_raw_sec_http.py` — invariant preserved (build script
   uses `SecEdgarClient`, not raw `requests`).
5. Docker smoke: `docker build -t alphalens-pipeline:test -f deploy/docker/Dockerfile.pipeline .`
   then `docker run --rm alphalens-pipeline:test python -c "from alphalens.data.fundamentals.sic_index import get_sic; print(get_sic('AAPL'))"`
   should print `3571`.
6. VPS rebuild + manual fire of `alphalens-thematic-daily.service`; tail
   `journalctl --user -u alphalens-thematic-daily -f` and confirm
   `thematic score` completes without `FileNotFoundError`.

## Risks

- **Index staleness**: SIC codes can be reassigned by SEC when an issuer
  files a Form 10-Q with a different SIC. The shipped parquet is a
  point-in-time snapshot. Quarterly rebuild caps this drift at one
  quarter. Daily pipeline cannot detect SIC reassignment by itself.
- **Missing CIKs**: tickers added to `ticker_cik_map.yaml` after the last
  SIC-index rebuild won't have a SIC entry; `get_sic` returns `None` and
  the brief loses cohort percentiles for that candidate (no crash).
- **Cohort split** (QUBT/IONQ no longer peers): documented above.
  Theme-conditional cohort is the deferred fix.
