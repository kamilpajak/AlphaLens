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

## Actions taken in this commit

1. **CLI score-on-empty fix** (`alphalens_cli/commands/thematic.py`). Score command crashed with `KeyError: 'layer4_weighted_score'` when `score_candidates(empty_df)` returned an empty df without the synthetic column. Patched to early-return after writing the empty parquet so downstream `brief` + `api rebuild-cache` can short-circuit gracefully on a thin day. Independent of PR #185 in spirit — same bug exists on `main`.

2. **Drop ETF gate from `GATE_NAMES`** (`alphalens/thematic/mapping/orchestrator.py`). `GATE_NAMES = ("tenk", "press", "insider")` now. `_record("etf", ...)` removed from `verify_candidate`. The `_gate_etf` function + `etf_holdings` module + the theme-ETF YAML are *retained* in-tree; they remain unit-tested and importable, so a future operator who decides to expand thematic-ETF coverage (semantic match, hand-curated alias table, etc.) can re-add the gate by re-inserting one line into `verify_candidate`. The decision today is "the gate's UNK output isn't actionable signal," not "the gate's logic is broken." Tests asserting `etf` in `gates_passed_str` / `gates_failed` updated.

3. **Relax 10-K PIT guard** (`alphalens/thematic/verification/tenk_grep.py::fetch_10k_text`). Guard changed from `asof < today` to `asof < today - 1 day`. After fetch, `_enforce_pit_after_fetch` checks the filing date against `asof`: a 10-K filed today is still cached (so tomorrow's run sees it), but its text is *not* surfaced to today's verification — preserving PIT correctness for the edge case the relaxation creates. Daily systemd timer can now warm the cache on first call.

## Verification

- 3442 / 3442 tests pass; 19 skipped (network-gated, unchanged).
- `ruff format` + `ruff check` clean.
- New tests:
  - `tests/test_thematic_cli.py::test_score_empty_candidates_writes_empty_scored_parquet` (red→green for the score fix)
  - `tests/thematic/test_theme_mapping.py::test_etf_dropped_from_gate_names` (pins `GATE_NAMES` shape)
  - `tests/thematic/test_tenk_grep.py::test_fetch_10k_text_yesterday_asof_primes_cold_cache` (asserts yesterday-asof now primes)
  - `tests/thematic/test_tenk_grep.py::test_fetch_10k_text_caches_but_returns_none_when_filing_date_after_asof` (asserts PIT safety after relaxed fetch)

## Deferred follow-ups (not in this commit)

- **10-K cache warm-up audit** — once the relaxed guard runs in production for a few days, audit whether the 10-K gate's pass rate is materially > 0% on rolling candidate sets. If yes, the gate is restored to usefulness; if no, investigate the CIK-resolver hit rate for the small-cap universe Pro is emitting.
- **Press gate Polygon flakiness** — `press_window_fetch_failed: HTTP Error 429` hit on both 2026-05-22 production runs (06:30 UTC + 09:44 UTC). Pre-fetch + per-candidate fallback path works, but the variance (`kept 0` vs `kept 3` for the same cached inputs) is too high. Possible mitigations: (a) wait/backoff between batch attempts, (b) cache the pre-fetched window across runs, (c) accept the variance and rely on three independent gates instead of one.
- **ETF coverage expansion** — if downstream consumers report that thematic-ETF inclusion is a material signal, revisit the gate with either (i) `theme_aliases.yaml` mapping free-form labels to canonical keys, or (ii) embedding-based semantic match against ETF series names. Both are out of scope for this commit.

## References

- PR #185 (`refactor/two-tier-clustering`): two-tier clustering + diversity guardrail, drop EDGAR. The 0-vs-1 candidate differential between `main` and the branch was variance on the press gate, not the new cap.
- `docs/research/thematic_event_tool_v1_design_2026_05_15.md`: original 4-gate verification design.
- Conversation with the zen reviewer (gemini-3-pro-preview, thinking=high) on 2026-05-22 — agreed on (1) drop-ETF over alias/embedding work, and (2) relax the PIT guard rather than add a separate priming script.
