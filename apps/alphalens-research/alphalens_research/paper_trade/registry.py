"""Strategy registry for paper-trade prospective replication tracks.

Each :class:`Strategy` entry binds a strategy identifier (used in CLI args
and on-disk filenames) to the concrete scorer/universe/refresh callables
and the ledger/state/verdict filenames.

Resolution is **lazy** via :func:`importlib.import_module` per
``callable_path`` (``module.path:fn_name`` form) so the CLI startup does
not pay the import cost of every registered strategy. The Layer 1
edgar-detect cron fires ``alphalens edgar detect`` frequently and must
not import scorer modules.

Adding a new strategy
---------------------
1. Implement the three callables (scorer / universe / refresh) somewhere
   in ``alphalens_research.paper_trade.*`` exposing the same call signatures as
   the v9D originals.
2. Add a :class:`Strategy` entry to :data:`REGISTRY` below.
3. Pre-register the hypothesis in the OSS phase-robust ledger.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Strategy:
    """Concrete binding from a strategy id to its on-disk + runtime contract.

    ``*_callable_path`` strings follow the setuptools / uvicorn convention
    ``module.path:fn_name`` and are resolved at first use via
    :func:`resolve_callable`.
    """

    id: str
    description: str
    ledger_filename: str
    state_filename: str
    verdict_filename: str
    scorer_callable_path: str
    universe_callable_path: str
    refresh_callable_path: str


REGISTRY: dict[str, Strategy] = {
    "v9d": Strategy(
        id="v9d",
        description=(
            "v9D long-only top-decile cross-sectional residual "
            "(-IVP30 orthogonalised against reversal_1m, momentum_6m, "
            "rv_30d). MDY benchmark, 5d rebalance, 30bps RT cost."
        ),
        ledger_filename="v9d_ledger.parquet",
        state_filename="v9d_state.yaml",
        verdict_filename="v9d_verdict.md",
        scorer_callable_path="alphalens_research.paper_trade.scorer_v9d:score_top_decile",
        universe_callable_path="alphalens_research.paper_trade.scorer_v9d:pit_union",
        refresh_callable_path="alphalens_research.paper_trade.scorer_v9d:incremental_refresh_smd",
    ),
}


def get_strategy(strategy_id: str) -> Strategy:
    """Look up a registered strategy or raise :class:`KeyError` with the choices."""
    if strategy_id not in REGISTRY:
        raise KeyError(f"Unknown strategy {strategy_id!r}. Choices: {sorted(REGISTRY)}")
    return REGISTRY[strategy_id]


def resolve_callable(callable_path: str) -> Callable[..., Any]:
    """Resolve a ``module.path:fn_name`` string to the live callable.

    Raises :class:`ValueError` if the string is malformed; propagates
    :class:`ImportError` / :class:`AttributeError` from the underlying
    import machinery so callers see the original failure mode.
    """
    module_path, _, fn_name = callable_path.partition(":")
    if not module_path or not fn_name:
        raise ValueError(f"callable_path must be 'module:fn', got {callable_path!r}")
    module = importlib.import_module(module_path)
    return getattr(module, fn_name)


def default_paper_trade_dir() -> Path:
    """Common parent directory for all strategies' on-disk artefacts."""
    return Path.home() / ".alphalens" / "paper_trade"
