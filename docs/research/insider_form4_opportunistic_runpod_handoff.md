# Runpod handoff — Form-4 backfill for insider_form4_opportunistic

**Locked:** 2026-05-05
**Pre-reg ID:** `insider_form4_opportunistic_2026_05_05`
**Plan:** `/Users/jacoren/.claude/plans/sunny-brewing-biscuit.md`
**Design memo:** `docs/research/insider_form4_opportunistic_design_2026_05_05.md`

## Goal

Backfill SEC EDGAR Form-4 / Form-4-A filings to a hive-partitioned parquet
store at `~/.alphalens/form4_parquet/transaction_year={YYYY}/...` for the
window 2006-01-01 → 2026-04-30. Output is consumed by Phase A check
(`scripts/phase_a_insider_form4.py`) and the multi-phase audit driver
(`scripts/experiment_insider_form4_opportunistic.py` via `audit_multi_phase`).

## Wall-time + cost estimate

- Universe: ~5000 unique CIKs (R3000 PIT membership union 2006-2026).
- Per-CIK cost: 1× submissions JSON fetch + N× Form-4 XML fetches (avg N≈100-500).
- SEC rate limit: 10 req/s per IP.
- Total fetches: ~2-3M.
- Wall: ~3-5 days continuous on runpod CPU pod.
- Pod: any cheap CPU pod ($0.10-0.25/h); GPU NOT required.
- Cost: ~$15-30 total.

## Setup steps

### 1. Build the CIK universe (one-off, run on Mac)

```bash
# Pseudocode — actual implementation will use existing R2000/R3000 PIT loaders
.venv/bin/python -c "
from datetime import date
import pandas as pd
from alphalens.data.alt_data.russell_universe import load_iwm_current
# Walk historical R3000 snapshots if available; otherwise use IWM current
# as conservative seed and accept survivorship-biased CIK universe (not
# ideal but acceptable for backfill scope — PIT discipline applied at
# scorer time, not at data-collection time).
# Output: ~/.alphalens/form4_cik_universe.txt — one CIK per line.
"
```

A pragmatic shortcut: use the SEC company_tickers.json as the CIK universe.
This is already cached by the watchdog at
`~/.alphalens/watchdog/company_tickers.json`. Filter to companies whose
ticker has appeared on US exchanges 2006-2026. Approximate but adequate for
backfill scope (any non-public CIKs will simply have no Form-4 filings).

### 2. Provision runpod pod

```bash
runpod ssh <pod-id>
git clone https://github.com/kamilpajak/AlphaLens.git
cd AlphaLens
uv venv --python 3.13
uv sync
```

Mount persistent volume `xymjkwj580` (per `feedback_runpod_primary_compute.md`) at `~/.alphalens`.

### 3. Upload CIK list

```bash
scp ~/.alphalens/form4_cik_universe.txt runpod-pod:~/AlphaLens/ciks.txt
```

### 4. Launch backfill

**Write directly to network volume** (not ephemeral disk) — backfill spans
multiple pod sessions, needs persistence + resume across pod stops.

```bash
# On pod, after bootstrap.sh
mkdir -p /network/form4_parquet

nohup .venv/bin/python scripts/run_form4_backfill.py \
    --user-agent "Kamil Pajak research pajakkamil@gmail.com" \
    --cik-list /network/form4_cik_universe.txt \
    --parquet-root /network/form4_parquet \
    --manifest /network/form4_backfill_manifest.json \
    --start-year 2006 --end-year 2026 \
    --checkpoint-every 20 \
    > /network/form4_backfill.log 2>&1 &

echo $! > /network/form4_backfill.pid
```

The CIK universe file (`form4_cik_universe.txt`, 8001 entries) ships from
the local Mac:
```bash
# From local Mac, before pod launch:
.venv/bin/python scripts/build_form4_cik_universe.py
# Produces: ~/.alphalens/form4_cik_universe.txt
# Upload to network volume via seed-pod or direct rsync.
rsync ~/.alphalens/form4_cik_universe.txt root@<pod>:/network/
```

User-Agent **must** include an email or URL contact per SEC policy
(403 otherwise).

### 5. Monitor

```bash
tail -f ~/.alphalens/form4_backfill.log
```

The runner logs every CIK completion: `[i/N] cik=XXXXXXXXXX wrote N records (running total M)`.

### 6. Verify partial output

```bash
ls -lh ~/.alphalens/form4_parquet/
# Expect: transaction_year=2006 ... transaction_year=2026 directories
ls ~/.alphalens/form4_parquet/transaction_year=2022/ | head
# Expect: part-{timestamp}-{hex}.parquet files (one per CIK group of writes)
```

### 7. Resume on interruption

The manifest tracks per-CIK completion. Re-running the same command
automatically skips already-complete CIKs.

```bash
# Just relaunch the same command. Already-complete CIKs are no-ops.
```

## Post-backfill verification (run on runpod or locally after rsync)

```bash
.venv/bin/python -c "
from datetime import date
from pathlib import Path
import pyarrow.dataset as ds

root = Path.home() / '.alphalens' / 'form4_parquet'
total = 0
for d in sorted(root.glob('transaction_year=*')):
    n = ds.dataset(str(d), partitioning=None, format='parquet').count_rows()
    print(f'{d.name}: {n:,} records')
    total += n
print(f'TOTAL: {total:,}')
"
```

Expected ranges:
- 2006-2008 (lookback only): ~150-300k records/year
- 2009-2017 (TRAIN): ~250-400k records/year
- 2018-2023 (OOS): ~300-500k records/year
- 2024-2026 (final lock): ~250-400k records (partial 2026)
- TOTAL: ~6-10M records

If TOTAL is < 4M, backfill is incomplete or universe was too narrow. If
>15M, likely included non-corporate-officer CIKs (individuals, funds) —
filter at scorer time (form4_filter ensures officer/director only).

## Next session — after backfill completes

1. Sync parquet from runpod to local `~/.alphalens/form4_parquet/` if
   running locally; otherwise stay on runpod.
2. Run Phase A check:
   ```bash
   .venv/bin/python scripts/phase_a_insider_form4.py \
       --train-start 2009-01-01 --train-end 2017-12-31 \
       --universe-mode R2000 \
       --out docs/research/insider_form4_opportunistic_phase_a.json
   ```
   - All 3 gates (A0 density, A1 breadth, A2 direction) must `passed: true`.
   - If any gate FAILs → ABANDON, log to ledger as DENSITY-FAIL or BREADTH-FAIL or SIGN-FLIP. NO program-Bonferroni burn.
3. Wire R2000 PIT universe loader into `experiment_insider_form4_opportunistic.py`
   (currently exits with code 3 by design until integration is complete).
   Specifically: replace the integration-pending stub with a per-rebalance
   call to `alphalens.data.alt_data.pit_universe.build_pit_universe` or the
   appropriate R2000 PIT loader.
4. Run multi-phase audit on R2000 primary:
   ```bash
   .venv/bin/alphalens audit insider_form4_opportunistic \
       --rebalance-stride 21 \
       --is-start 2018-01-01 --is-end 2023-12-31 \
       --universe-mode R2000 \
       --out docs/research/insider_form4_opportunistic_R2000_multi_phase_audit.json
   ```
5. If primary R2000 PASS or PASS_MARGINAL → run secondary R3000 diagnostic
   (same command with `--universe-mode R3000`). Report both verdicts in postmortem.
6. If primary R2000 PASS or PASS_MARGINAL → run final lock single-phase on
   2024-2026:
   ```bash
   .venv/bin/python scripts/experiment_insider_form4_opportunistic.py \
       --is-start 2024-01-01 --is-end 2026-04-30 \
       --phase-offset 0 --rebalance-stride 21 \
       --universe-mode R2000
   ```
   Postmortem must explicitly acknowledge prior cluster screener test on
   this window (different feature spec, but burnt window association noted).
7. Compute Romano-Wolf bounds CI with `block_size=126` (per pre-reg lock)
   on pooled phase returns. Report `bounds_alpha_t_lower/upper`.
8. Complete ledger:
   ```bash
   .venv/bin/alphalens preregister complete insider_form4_opportunistic_2026_05_05 \
       --verdict {PASS|PASS_MARGINAL|INCONCLUSIVE|FAIL} \
       --mean-alpha-t <value> \
       --mean-excess-net <value> \
       --audit-path docs/research/insider_form4_opportunistic_R2000_multi_phase_audit.json \
       --notes "<bounds CI lower/upper, breadth, postmortem path, R3000 diagnostic verdict>"
   ```

## Known risks (carry over from design memo)

- 2006-2008 lookback data quality: probe Cohen-Malloy classifier output
  distribution Q1-2010 vs Q1-2020; if divergent (>10pp routine fraction
  shift), restrict TRAIN to 2014-2018.
- Latent E4 ticker-as-of-filing risk: freeze ticker_cik_map.yaml snapshot
  date (`alphalens/data/alt_data/ticker_cik_map.py`); flag in postmortem if
  reverse-merger CIKs material.
- Cohen-Malloy decay since 2012 publication: treated as expected info, not
  bug — informative if observed.
- R2000 cost drag higher than R3000 — report cost-gross alongside cost-net
  Sharpe; if cost-net Sharpe < 0 with cost-gross > 0, flag as
  "implementation-bottlenecked" in verdict prose.
