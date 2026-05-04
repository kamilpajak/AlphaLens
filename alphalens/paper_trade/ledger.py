"""Append-only weekly paper-trade ledger.

Every weekly score run appends one ``LedgerEntry`` row to a parquet file.
Schema is intentionally narrow: asof, holdings (list[str]), prior_holdings,
realized_return_long_net, benchmark_return_mdy, n_held, gross_return,
cost_drag_bps. The ledger is the source of truth for cumulative αt at the
checkpoint gates; the scorer state file is just a current-snapshot pointer.

Append-only invariant: writes are guarded by an asof-uniqueness check.
Writing a new entry whose asof is already in the ledger raises
``LedgerError`` — the caller must explicitly reset (e.g. delete the
parquet) to back-fill, which is logged as a manual correction.

Schema is locked at module level (``LEDGER_COLUMNS``); changes require a
versioned migration not handled here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import pandas as pd

LEDGER_COLUMNS: tuple[str, ...] = (
    "asof",
    "rebalance_n",
    "n_held",
    "holdings",
    "prior_holdings",
    "realized_return_long_gross",
    "realized_return_long_net",
    "benchmark_return_mdy",
    "cost_drag_bps",
    "universe_size",
)


class LedgerError(Exception):
    """Append-only invariant violation or schema mismatch."""


def default_ledger_path() -> Path:
    return Path.home() / ".alphalens" / "paper_trade" / "v9d_ledger.parquet"


@dataclass
class LedgerEntry:
    """One weekly paper-trade record.

    Returns are decimal (e.g. 0.012 for +1.2%). Holdings are stored as
    sorted lists for deterministic comparison across runs. ``cost_drag_bps``
    is the realized round-trip drag in basis points (e.g. 30.0 for 30bps);
    ``realized_return_long_net = realized_return_long_gross − drag/10000``.
    """

    asof: date
    rebalance_n: int
    n_held: int
    holdings: list[str]
    prior_holdings: list[str]
    realized_return_long_gross: float
    realized_return_long_net: float
    benchmark_return_mdy: float
    cost_drag_bps: float
    universe_size: int

    def __post_init__(self) -> None:
        self.holdings = sorted(self.holdings)
        self.prior_holdings = sorted(self.prior_holdings)

    def to_row(self) -> dict:
        d = asdict(self)
        d["asof"] = self.asof.isoformat()
        return d


def load_ledger(path: Path | str | None = None) -> pd.DataFrame:
    path = Path(path) if path else default_ledger_path()
    if not path.exists():
        return pd.DataFrame(columns=list(LEDGER_COLUMNS)).astype(
            {
                "rebalance_n": "int64",
                "n_held": "int64",
                "universe_size": "int64",
                "realized_return_long_gross": "float64",
                "realized_return_long_net": "float64",
                "benchmark_return_mdy": "float64",
                "cost_drag_bps": "float64",
            },
            errors="ignore",
        )
    df = pd.read_parquet(path)
    missing = set(LEDGER_COLUMNS) - set(df.columns)
    if missing:
        raise LedgerError(f"ledger schema mismatch — missing columns: {sorted(missing)}")
    return df


def append_ledger_entry(entry: LedgerEntry, path: Path | str | None = None) -> pd.DataFrame:
    """Append one entry; enforce asof-uniqueness; return the resulting frame."""
    path = Path(path) if path else default_ledger_path()
    existing = load_ledger(path)

    asof_iso = entry.asof.isoformat()
    if not existing.empty and (existing["asof"] == asof_iso).any():
        raise LedgerError(
            f"asof {asof_iso!r} already in ledger at {path}; refusing to append "
            "(append-only invariant). Delete the row manually if back-filling."
        )

    new_row = pd.DataFrame([entry.to_row()])
    combined = pd.concat([existing, new_row], ignore_index=True)
    combined = combined.sort_values("asof").reset_index(drop=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(path, index=False)
    return combined
