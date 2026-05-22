# alphalens-research

The research engine — screeners, backtest, attribution, watchdog, paper-trade, and the thematic daily pipeline. Installed from the workspace root `pyproject.toml`.

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
