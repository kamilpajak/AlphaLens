# alphalens-pipeline

Live production layer of the AlphaLens monorepo. Hosts the data clients,
PIT storage, scorer library, and the daily / cron jobs that run in
launchd (macOS) + systemd (VPS).

## Sub-packages

| Path | Status | Notes |
|------|--------|-------|
| `alphalens_pipeline/edgar_detector/` | ACTIVE | Layer 1 SEC EDGAR detection — launchd `detect`-only |
| `alphalens_pipeline/thematic/` | ACTIVE | Phase A-E daily pipeline — VPS systemd |
| `alphalens_pipeline/literature_scanner/` | ACTIVE | Monthly + weekly Perplexity scan — launchd |
| `alphalens_pipeline/data/` | ACTIVE namespace | 4 canonical clients (EDGAR/AV/Polygon/Gemini) + PIT store + universes + fundamentals + macro |
| `alphalens_pipeline/core/` | ACTIVE namespace | candidate-queue plumbing (SQLite + dataclass) |
| `alphalens_pipeline/scorers/` | ACTIVE | reusable validated scorer library (carved out from screeners) |
| `alphalens_cli/` | ACTIVE | the `alphalens` binary; lazy-imports research-side modules for `audit`/`preaudit`/`preregister` |

## Boundary

`alphalens-pipeline` is the **infrastructure + live services tier**. It must not
import from `alphalens-research` (the lab tier). The only exception is the CLI,
which orchestrates both tiers via lazy imports inside command bodies — this
preserves the dependency direction `research → pipeline` without creating a
workspace-level cycle. Enforcement: `apps/alphalens-research/tests/test_module_dependencies.py`.
