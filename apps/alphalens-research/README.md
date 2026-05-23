# alphalens-research

The research lab — screeners, backtest, attribution, overlays, gates,
preaudit, diagnostics, and paper-trade. Installed from the workspace
root `pyproject.toml`.

> Live infrastructure (watchdog, thematic, data clients, literature_review,
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
.venv/bin/alphalens watchdog run-once
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

See the root [`README.md`](../../README.md) for the full layer-status table, the research methodology, and the live operational jobs. Architectural decisions live in [`docs/adr/`](../../docs/adr/).
