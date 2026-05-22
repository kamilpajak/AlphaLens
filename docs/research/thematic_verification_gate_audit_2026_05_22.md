# Thematic Verification Gate Audit — 2026-05-22

**Status:** COMPLETED
**Branch:** `refactor/two-tier-clustering` (PR #185)
**Trigger:** User asked "is today's 1-candidate brief a bug?" → empirical investigation surfaced two pre-existing structural issues.

## Why this memo exists

The 2026-05-21 daily brief shipped with `n_flash=1` (just C3.ai/AI), an order of magnitude below the trailing two days (`n_flash=8` on 05-19, `n_flash=7` on 05-20). Initial diagnosis blamed PR #185's new `_MAX_VERIFY_ATTEMPTS_PER_THEME=5` cap. Empirical re-runs falsified that hypothesis and surfaced two unrelated bugs that have been silently degrading verification yield on `main` as well as on the branch.

## Empirical timeline

| Wall time | Source | Result | Notes |
|---|---|---|---|
| 06:30 UTC | Daily systemd timer on `main` (`fd9dce8`) | `kept 1 / dropped 16` | C3.ai/AI verified via press gate |
| 09:44 UTC | Manual rerun on PR #185 image (default flags) | `kept 0 / dropped 15` | Polygon press window 429 → press gate UNK |
| ~10:15 UTC | Manual rerun on PR #185 image w/ `--keep-unverified` | 16 candidates emitted (3 verified) | Press gate worked this run → exposed the data below |

The `--keep-unverified` run produced a per-(theme, ticker) gate verdict table:

```
theme                      ticker   conf  PASS         FAIL         UNK
------------------------------------------------------------------------
AI                         PATH    0.85  press        -            etf,tenk,insider
AI                         AI      0.80  press        insider      etf,tenk
AI                         SYM     0.80  -            press,insider etf,tenk
Artificial Intelligence    AI      0.80  press        insider      etf,tenk
IPO                        DFIN    0.85  -            press,insider etf,tenk
...
total: 16    verified: 3
```

## Findings

### Hypothesis falsified: per-theme attempt cap is NOT the cause

`_MAX_CANDIDATES_PER_THEME=3` (the new cap) caps each theme at three rows after the verifier walks them in `gemini_confidence`-DESC order. C3.ai/AI sits at rank ≤3 in both "AI" and "Artificial Intelligence" themes, so the cap does not exclude it. The earlier 0-candidate result came from press gate flakiness, not the cap.

### Bug 1 — ETF gate: 100% UNK by structural mismatch

`alphalens/thematic/verification/etf_holdings.py::is_in_thematic_etf` consults a thematic-ETF map keyed by 9 canonical snake_case industry tokens:

```
ai_semi, ai_software, biotech, clean_energy, cyber,
defense, materials, med_devices, quantum
```

`_resolve_theme_keys` accepts either an exact-lowercase match (`t.lower() in cfg`) or token-prefix match (`t.startswith(cfg_key + "_")`). The Layer 2 Gemini Flash extraction emits "free-form" themes; 7-day inventory shows ~1597 distinct labels across 1400 events. For "AI" alone: `AI` (115×), `Artificial Intelligence` (21×), `artificial_intelligence` (14×), `AI infrastructure` (18×). None of these match an ETF cfg key. Today's themes (`AI, Artificial Intelligence, IPO, consumer warning, discounts, gas prices, government intervention`) returned `relevant=[]` for all 7 → gate returned `None` (UNK) for every candidate. This isn't a vocab mismatch fixable by aliases alone: thematic ETFs only exist for ~10 specific industries; the rest of the news theme space (IPO, gas prices, discounts) has no ETF counterpart by reality. The gate is **narrow by design** and silently degrades to UNK on broad-news days.

### Bug 2 — 10-K gate: empty cache + over-strict PIT guard

`alphalens/thematic/verification/tenk_grep.py::fetch_10k_text` enforced:

```python
if asof is not None and asof < dt.date.today():
    return None  # PIT replay miss
```

The daily systemd timer runs with `--date yesterday` (asof = today - 1 day), so the guard *always* refuses to prime the cache. No separate priming job exists. `~/.alphalens/thematic_tenk/` has stood empty since the directory was created on 2026-05-19 — every candidate hits cache-miss → guard refuses fetch → `None` → UNK. 10-Ks are annual filings; refusing to fetch on `asof = today - 1day` is operationally over-cautious because a 10-K filed within the past day still describes a fiscal year that ended months earlier.

### Combined impact on verification yield

Of the nominal 4-gate verification, only `press` and (sometimes) `insider` are functional in production. The "4-gate verified" architecture has effectively been a `~1.3-gate verified` architecture for as long as this VPS deployment has been running. False-negative rate is structurally elevated; the verified-candidate count understates true thematic match by a wide margin.

## Actions taken across the two commits on this branch (4da12ea + zen pre-merge follow-up)

1. **CLI score-on-empty fix** (`alphalens_cli/commands/thematic.py`). Score command crashed with `KeyError: 'layer4_weighted_score'` when `score_candidates(empty_df)` returned an empty df without the synthetic column. Patched to early-return after writing the empty parquet so downstream `brief` + `api rebuild-cache` can short-circuit gracefully on a thin day. Independent of PR #185 in spirit — same bug exists on `main`.

2. **Delete ETF gate decisively** (`alphalens/thematic/mapping/orchestrator.py`, `alphalens/thematic/verification/etf_holdings.py`, `alphalens/thematic/config/theme_etfs.yaml`, `tests/thematic/test_etf_holdings.py`). `GATE_NAMES = ("tenk", "press", "insider")` now. The first commit (4da12ea) retained the etf_holdings module as a "restore-ready" stub; zen pre-merge review pushed back that ~400 LOC of dead XML parsing + SEC fetch logic + its test suite is a maintenance tax, and that git history is the truer restore-ready state. The zen-review follow-up commit deletes the module, the YAML config, and the dedicated unit-test file outright. To re-add the gate later, restore those three files from git history and re-add the `_gate_etf` wrapper + `_record("etf", ...)` line in `verify_candidate`.

3. **PIT-correct `find_latest_10k`** (`alphalens/thematic/verification/tenk_grep.py`). First commit relaxed the file-level guard from `asof < today` to `asof < today - 1 day` and added a post-fetch `_enforce_pit_after_fetch` helper that returned `None` when the latest 10-K's filing date post-dated `asof`. Zen pre-merge review flagged this as a regression: when the latest filing post-dates `asof`, the post-fetch helper would discard a valid prior-year 10-K instead of falling back to it. The zen-review follow-up commit instead pushes the asof filter INTO `find_latest_10k(asof=...)` so the SEC index is filtered at the source, surfaces the latest-≤-asof filing natively, and removes both the file-level day-staleness guard AND the post-fetch helper. The remaining `fetch_10k_text` shape: cache hit → return; else fetch submissions → `find_latest_10k(asof)` → if None return None; else short-circuit on `cache_path.exists()` (avoids re-HTML-fetch on TTL re-arm) → else HTML-fetch + extract + write + return.

4. **10-K cache TTL** (`alphalens/thematic/verification/tenk_grep.py::_find_cached`). New `_CACHE_TTL_DAYS = 380` constant. `_find_cached` returns `None` when the latest eligible cache file is older than 380 days relative to `asof` (or today, when asof is None), forcing a SEC-index check that catches a newer filing the cache had been masking. Combined with the `cache_path.exists()` short-circuit in `fetch_10k_text`, the gate doesn't re-fetch HTML for filings it has already cached — only the cheap submissions JSON is re-consulted.

## Verification

- 3443 / 3443 tests pass; 19 skipped (network-gated, unchanged).
- `ruff format` + `ruff check` clean on touched files (the ~9 pre-existing repo-wide lint errors are in untouched modules from PR #185's main refactor).
- New tests:
  - `tests/test_thematic_cli.py::test_score_empty_candidates_writes_empty_scored_parquet` (red→green for the score fix)
  - `tests/thematic/test_theme_mapping.py::test_etf_dropped_from_gate_names` (pins `GATE_NAMES` shape)
  - `tests/thematic/test_tenk_grep.py::test_fetch_10k_text_yesterday_asof_primes_cold_cache` (asserts yesterday-asof now primes)
  - `tests/thematic/test_tenk_grep.py::test_fetch_10k_text_picks_prior_year_when_latest_filing_post_dates_asof` (asserts asof-filter at SEC index source picks a valid prior 10-K instead of returning None)
  - `tests/thematic/test_tenk_grep.py::test_find_cached_evicts_files_older_than_ttl` (asserts TTL eviction)
  - `tests/thematic/test_tenk_grep.py::test_fetch_10k_text_short_circuits_html_fetch_when_cache_file_matches_sec_index` (asserts anti-hammering short-circuit after TTL eviction)

## Deferred follow-ups (not in these two commits)

- **Press gate Polygon flakiness** — `press_window_fetch_failed: HTTP Error 429` hit on both 2026-05-22 production runs (06:30 UTC + 09:44 UTC). Pre-fetch + per-candidate fallback path works, but the variance (`kept 0` vs `kept 3` for the same cached inputs) is too high. Possible mitigations: (a) wait/backoff between batch attempts, (b) cache the pre-fetched window across runs, (c) accept the variance and rely on three independent gates instead of one.
- **10-K cache warm-up audit** — once the relaxed guard runs in production for a few days, audit whether the 10-K gate's pass rate is materially > 0% on rolling candidate sets. If yes, the gate is restored to usefulness; if no, investigate the CIK-resolver hit rate for the small-cap universe Pro is emitting.
- **Web mock fixtures regeneration** — `web/tests/fixtures/api-mock/days/*.json` still contain `"etf"` in `gates_unknown` arrays from historical brief snapshots. Frontend GatePill silently handles missing keys, so no breakage; the fixtures will diverge from live API output until they're regenerated from a post-deploy snapshot.

## References

- PR #185 (`refactor/two-tier-clustering`): two-tier clustering + diversity guardrail, drop EDGAR. The 0-vs-1 candidate differential between `main` and the branch was variance on the press gate, not the new cap.
- `docs/research/thematic_event_tool_v1_design_2026_05_15.md`: original 4-gate verification design.
- Zen pre-merge review (gemini-3-pro-preview, thinking=high) on 2026-05-22:
  - Round 1 (continuation `09bdf1ef`) — agreed on (1) drop-ETF over alias/embedding work, and (2) relax the PIT guard rather than add a separate priming script.
  - Round 2 (continuation `5ef409db`) on commit 4da12ea — pushed (3) move asof filter INTO `find_latest_10k`, (4) add cache TTL, (5) delete ETF module decisively instead of retaining as restore-ready stub.
