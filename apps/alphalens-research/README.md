# alphalens-research

The research lab — screeners, backtest, attribution, overlays, gates,
preaudit, diagnostics, and retrospective_audit. Installed from the workspace
root `pyproject.toml`.

> Live infrastructure (edgar_detector, thematic, data clients, literature_scanner,
> scorers, and the `alphalens` CLI binary) lives in the sibling
> [`apps/alphalens-pipeline/`](../alphalens-pipeline/) workspace member.
> This lab depends on it via a workspace dep; the `alphalens` CLI is only
> available when the full workspace is synced (`uv sync` from repo root).
> A focused `uv pip install -e apps/alphalens-research/` will install the
> lab but leave the CLI broken — use workspace sync instead.

## Quickstart

```bash
# From repo root
uv venv --python 3.13
uv sync

# Run a CLI subcommand
.venv/bin/alphalens edgar detect
.venv/bin/alphalens preregister threshold
.venv/bin/alphalens audit insider_form4_opportunistic \
    --is-start 2018-01-01 --is-end 2023-12-31 --rebalance-stride 21

# Tests (unittest, not pytest)
.venv/bin/python -m unittest discover apps/alphalens-research/tests \
    -t apps/alphalens-research -v
```

## Layout

- `alphalens_research/` — the importable Python package (Layer 1-5 + data + thematic)
- `alphalens_cli/` — Typer CLI; entry point `alphalens` in `pyproject.toml [project.scripts]`
- `tests/` — unittest suite + four architectural enforcers
- `scripts/` — experiment runners and backfill orchestrators

## Layer architecture (ADR 0007)

Five layers, one responsibility each, so a failure attributes to exactly one of them.
[ADR 0007](../../docs/adr/0007-layer-architecture.md) is the decision record, but it states
**pre-split** paths (`alphalens/screeners/*`, `alphalens/backtest/engine.py`, …). The post-split
paths in the table below are the authoritative ones.

| # | Layer | Responsibility | Path |
|---|---|---|---|
| 1 | Screener | cross-sectional rank at time *t* → top-N tickers | `alphalens_research/screeners/*` |
| 2 | Selection-gate | binary / graded gate on the scorer — changes *which* tickers deploy | `alphalens_research/gates/` |
| 3 | Backtest engine | runs the scorer over a strided rebalance calendar → `BacktestReport.portfolio_returns` | `alphalens_research/backtest/engine.py` |
| 4 | Risk overlay | time-series sizing on realised portfolio vol — changes *how much* exposure | `alphalens_research/overlays/` |
| 5 | Attribution | cost drag, Carhart-4F, Sharpe, multiple testing → ledger verdict | `alphalens_research/attribution/` |

Engine-side primitives (`rank_ic`, `turnover_pct`, `sharpe`) live in
`alphalens_research/backtest/metrics.py`, not in Layer 5.

A compound hypothesis combines layers (momentum + low-vol × a VIX>20 gate × a vol-target overlay)
and each combination pays its own Bonferroni cost. **Time-varying-beta hazard:** an
overlay-bearing strategy uses Sharpe improvement as its primary success metric, not the Carhart α
t-stat — see ADR 0007.

## Layer table

Lifecycle status is declared per package in its own `__init__.py` as `__status__`. This table is a
digest; the package's constant is the authority, and [`CLAUDE.md`](CLAUDE.md) holds the rules the
status gate enforces.

| Path (under `alphalens_research/`) | `__status__` | Notes |
|---|---|---|
| `screeners/` | *(none — namespace)* | the one entry in the gate's namespace allowlist; each sub-package carries its own status |
| `screeners/prescreener/` | RESEARCH_ONLY | Layer 2a — unvalidated, manual ad-hoc only |
| `screeners/momentum_lowvol/` | RESEARCH_ONLY | Layer 2 momentum + low-vol; the strategy FAILed, the scorer is reused as the base for the Layer 4 vol-target overlay test |
| `gates/` | RESEARCH_ONLY | Layer 2 selection-gates wrapper |
| `backtest/` | ACTIVE | Layer 3 — `engine`, `metrics`, `weighting`, `theme_analysis`, `llm_scorers`, `historical_validation`, plus the inference modules (`bounds_inference`, `sharpe_inference`, `romano_wolf`, `doctrine_verdict`, `top_decile_portfolio`, `daily_continuous_returns`) |
| `overlays/` | RESEARCH_ONLY | Layer 4 risk overlays — `vol_target`, `drawdown_control`, `credit_regime`, `assess` |
| `attribution/` | ACTIVE | Layer 5 — `cost_model`, `cost_validation`, `factor_analysis`, `regime`, `diagnostics`, `report`, `walk_forward`, `signal_vol_regime`, `signal_independence` |
| `preaudit/` | ACTIVE | per-strategy `SmokeProfile` + coverage gate before an audit launch |
| `diagnostics/` | ACTIVE | `survivorship_pit` is the PIT screen; the rest are exit / fill / no-fill retrospectives. **Cyclicality screens are not here** — they live in `attribution/signal_vol_regime.py` |
| `retrospective_audit/` | RESEARCH_ONLY | PIT universe loaders (U1/U2/U3) + SMD cache primitives for one-shot retrospectives |
| `discover_lane/` | RESEARCH_ONLY | one-shot Perplexity candidate-generation experiment; outside the gated roots |
| `eval/` | RESEARCH_ONLY | reading-quality / generation-fidelity harness (T1-T6), deliberately never joined to any outcome or return ledger |

Read the tree for what exists; a table row is a pointer and its absence means nothing.

## Methodology bundle is an external, tag-pinned dependency

The pre-registration ledger, the multi-phase audit, the multiple-testing thresholds and the
`audit_multi_phase` driver are **not in this package**. [`pyproject.toml`](pyproject.toml) line 22
declares them as a git dependency pinned to a tag:

```toml
"phase-robust-backtesting @ git+https://github.com/kamilpajak/phase-robust-backtesting.git@v0.3.0",
```

`uv.lock` resolves it to `rev=v0.3.0`. It is NOT a PyPI range such as `>=0.2.0`: moving it means
editing the tag in `pyproject.toml` and re-running `uv sync` from the repo root.

`multi_phase.py` and `multiple_testing.py` live in THAT package —
`import phase_robust_backtesting.multi_phase`. No such module exists under
`alphalens_research/backtest/`; `find apps/alphalens-research -name 'multi_phase*'` returns
nothing. Extraction rationale:
[ADR 0006](../../docs/adr/0006-phase-robust-backtesting-extraction.md).

`alphalens audit <strategy>` resolves a short strategy name to a file path and delegates in-process
to `phase_robust_backtesting.audit_multi_phase.run_audit`.

## Research gates

The mandatory procedures — pre-audit smoke, the Layer 4 overlay pre-screen, the data-vendor PIT
validation gate, the Little's-Law cost-model audit and the true-PIT universe rule — live in
[`docs/research/methodology-gates.md`](../../docs/research/methodology-gates.md).

Architectural decisions live in [`docs/adr/`](../../docs/adr/).
