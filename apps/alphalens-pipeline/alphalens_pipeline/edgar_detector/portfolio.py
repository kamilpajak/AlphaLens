from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml


class Relevance(Enum):
    HELD = "held"
    WATCHLIST = "watchlist"
    FOREIGN = "foreign"


def default_portfolio_path() -> Path:
    return Path.home() / ".alphalens" / "edgar-detect" / "portfolio.yaml"


@dataclass
class PortfolioState:
    held: list[str] = field(default_factory=list)
    watchlist: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | str | None = None) -> PortfolioState:
        path = Path(path) if path else default_portfolio_path()
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text()) or {}
        return cls(
            held=list(data.get("held") or []),
            watchlist=list(data.get("watchlist") or []),
        )

    def save(self, path: Path | str | None = None) -> None:
        path = Path(path) if path else default_portfolio_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({"held": self.held, "watchlist": self.watchlist}))

    def relevance_for(self, ticker: str) -> Relevance:
        if ticker in self.held:
            return Relevance.HELD
        if ticker in self.watchlist:
            return Relevance.WATCHLIST
        return Relevance.FOREIGN
