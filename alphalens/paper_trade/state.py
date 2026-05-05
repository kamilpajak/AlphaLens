"""Paper-trade portfolio state — the most-recent v9D top decile selection.

Mirrors ``alphalens.watchdog.portfolio.PortfolioState`` interface (load/save
to YAML at a default path under ``~/.alphalens/``). Purpose: the weekly
score job reads this to recover last-week's holdings, computes their
realized return over the past 7d, then writes new holdings.

The ledger (``alphalens.paper_trade.ledger``) keeps the historical
record; this module is just the current pointer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml


def default_state_path() -> Path:
    return Path.home() / ".alphalens" / "paper_trade" / "v9d_state.yaml"


@dataclass
class PaperTradeState:
    """Current paper-trade holdings + scoring metadata.

    ``held``: list of tickers (long, equal-weighted within).
    ``scores``: optional per-ticker residual score from the scoring run
    (used for diagnostics only — not persisted across reloads if missing).
    ``as_of``: scoring date (the trading-day snapshot used to build features).
    ``rebalance_n``: 1-based rebalance counter — increments by 1 each weekly
    score run. Used to detect skipped weeks and as a sanity check on the
    ledger length.
    """

    held: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    as_of: date | None = None
    rebalance_n: int = 0

    @classmethod
    def load(cls, path: Path | str | None = None) -> PaperTradeState:
        path = Path(path) if path else default_state_path()
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text()) or {}
        as_of_raw = data.get("as_of")
        as_of_parsed: date | None
        if as_of_raw is None:
            as_of_parsed = None
        elif isinstance(as_of_raw, date):
            as_of_parsed = as_of_raw
        else:
            as_of_parsed = date.fromisoformat(str(as_of_raw))
        return cls(
            held=list(data.get("held") or []),
            scores=dict(data.get("scores") or {}),
            as_of=as_of_parsed,
            rebalance_n=int(data.get("rebalance_n") or 0),
        )

    def save(self, path: Path | str | None = None) -> None:
        path = Path(path) if path else default_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "held": list(self.held),
            "scores": {k: float(v) for k, v in self.scores.items()},
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "rebalance_n": int(self.rebalance_n),
        }
        path.write_text(yaml.safe_dump(payload, sort_keys=False))
