# Layer 2d Phase 2.5 PIT universe build runbook

**Prerequisites:**
- Phase 2.5.1-2.5.3 shipped (XBRL client + yfinance cache + PIT builder).
- `alphalens/data/alt_data/data/ticker_cik_map.yaml` seeded (Phase 2 P3).
- `alphalens/data/alt_data/data/iwm_current.yaml` seeded (Phase 2 P4).
- `SEC_EDGAR_USER_AGENT` env var set.

## Full build (one-shot, ~6-7h wall clock)

```bash
export SEC_EDGAR_USER_AGENT="AlphaLens your@email.com"
.venv/bin/python scripts/build_pit_universe.py
```

This runs all three stages end-to-end. Each stage is resumable — rerun
after any crash and only missing work is redone.

**Wall-clock estimate:**
- Stage 1 (companyfacts):  ~1900 CIKs × 1 call × 8 rps   = ~4 min
- Stage 2 (prices):        ~1900 tickers × ~1s/ticker    = ~35 min
- Stage 3 (snapshots):     ~200 month-ends × build       = <1 min
- **Total: ~40 min** for current IWM universe (1930 tickers, 2009-2026)

(The ~6-7h plan estimate assumed the full SEC ticker master of ~10k
names; using IWM-only cuts it materially.)

## Stage-by-stage execution

### Stage 1 — companyfacts (XBRL shares outstanding)

```bash
.venv/bin/python scripts/build_pit_universe.py --stage companyfacts
```

Fetches `data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json` for each
ticker's CIK, caches under `~/.alphalens/companyfacts/{CIK}.json`.
Skip-if-cached; rerun is cheap after partial failure.

Progress log every 100 tickers. SEC EDGAR polite rate 10 rps (`SecEdgarClient`
enforces 8 rps internally for safety headroom).

### Stage 2 — prices (yfinance daily OHLCV)

```bash
.venv/bin/python scripts/build_pit_universe.py --stage prices \
    --start 2009-01-01 --end 2026-04-22
```

Bulk-downloads per-ticker history from yfinance, persists parquet under
`~/.alphalens/prices/{TICKER}.parquet`. Fetch errors (delisted / network)
logged at WARNING, ticker skipped.

Rate: 1s sleep between tickers (Yahoo-friendly). Total ~35 min for 1930
tickers. Known survivorship bias ~50-100 bps/y (R6/R8) — evaluated in
Phase 3b sensitivity.

### Stage 3 — monthly snapshots

```bash
.venv/bin/python scripts/build_pit_universe.py --stage snapshots
```

Assembles month-end PIT universe yaml at
`~/.alphalens/pit_universe/{YYYY-MM}.yaml` for each month between
`--start` and `--end`. Each snapshot contains the ticker list whose
`shares × close` fell in `[cap_min, cap_max]` (default $300M–$3B) as of
the month-end date. Skip-if-exists; rerun is fast.

Snapshot schema:
```yaml
asof: 2024-06-30
tickers:
  - AAPL
  - MSFT
  - ...
```

## Smoke testing with a small universe

Before the full 40-min run, verify end-to-end on a handful of tickers:

```bash
.venv/bin/python scripts/build_pit_universe.py \
    --tickers AAPL,MSFT,NVDA,UPST,SMCI \
    --start 2020-01-01 --end 2024-12-31
```

This runs all three stages on 5 tickers in <1 min, confirms:
- companyfacts fetched/cached
- prices downloaded/cached
- snapshots written with sensible market caps

## Cap-band override

Default cap band matches R4 small-cap thesis. Override for sensitivity:

```bash
# Mid-cap variant: $3B-$20B
.venv/bin/python scripts/build_pit_universe.py --stage snapshots \
    --cap-min 3000000000 --cap-max 20000000000
```

Snapshots are written alongside (don't overwrite) default snapshots — use
separate output dirs if you want to keep both. (Future enhancement: flag
for output dir.)

## Post-build verification

```bash
# Count snapshot files (should match month-end count for date range)
ls ~/.alphalens/pit_universe/ | wc -l

# Average universe size per snapshot
.venv/bin/python -c "
from pathlib import Path
import yaml
snaps = list(Path.home().glob('.alphalens/pit_universe/*.yaml'))
counts = [len(yaml.safe_load(p.read_text())['tickers']) for p in snaps]
print(f'snapshots: {len(snaps)}, mean universe: {sum(counts)/len(counts):.0f}')
"
```

Sanity: R2000-approximate at $300M-$3B should yield 200-800 tickers per
month depending on regime (fewer in late-cycle bull markets where caps
inflate above $3B).

## Known limitations

1. **Multi-class shares (A/B/C)**: `EntityCommonStockSharesOutstanding`
   aggregates all classes. For GOOGL vs GOOG, BRK.A vs BRK.B, the cap
   computation uses total shares — close-price by ticker class, so
   market caps may be slightly off for filers with >1 share class.
2. **yfinance delisted coverage**: partial — some bankrupt/M&A tickers
   have no pre-delisting data retrievable. Documented ~50-100 bps/y
   survivorship drag; evaluate under Phase 3b sensitivity (0/75/150 bps).
3. **IWM-current-only universe**: builds snapshots only for tickers in
   the current IWM holdings. Truly-delisted names never appear. For a
   larger reconstruction, swap `load_iwm_current` for a broader seed
   (e.g. full SEC `company_tickers.json`).
