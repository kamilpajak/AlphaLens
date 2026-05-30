"""Resolve feed-tagged tickers + optional alias-table mentions to entities.

PR-1 MVP: the source-of-truth for tickers is the feed-side tagging from
``news_ingest`` (Polygon, EDGAR, RSS, GDELT all already populate
``Article.tickers_raw``). The resolver normalizes those + applies an
optional alias table for cases where the body mentions a company name
the feed did not pre-tag.

A heavier mention-extraction / NER pass lives in a follow-up — this
MVP unblocks PR-2 hybrid integration without forcing a model dependency.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from alphalens_pipeline.thematic.extraction.templates.spec import (
    Article,
    ResolvedEntity,
)

# Same default the EDGAR detector writes to. Re-reading from a single
# canonical path is the "one canonical client per vendor" doctrine
# applied at the data-on-disk layer — see CLAUDE.md.
DEFAULT_COMPANY_TICKERS_PATH = Path.home() / ".alphalens" / "edgar-detect" / "company_tickers.json"


class EntityResolver:
    """Looks up tickers against company_tickers.json + optional aliases.

    The resolver is intentionally permissive: a feed-tagged ticker that
    isn't in the lookup table still resolves (with ``name=ticker`` as
    fallback). The feed is the source of truth for "this article is
    about $X" — the table only enriches the human-readable name.
    """

    def __init__(
        self,
        company_tickers_path: Path = DEFAULT_COMPANY_TICKERS_PATH,
        alias_path: Path | None = None,
    ) -> None:
        self._ticker_to_name = self._load_tickers(company_tickers_path)
        self._aliases = self._load_aliases(alias_path) if alias_path else {}

    @staticmethod
    def _load_tickers(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        # SEC ships the file as a dict-of-rows keyed by string indices
        # ("0", "1", ...). We project to ticker → title.
        out: dict[str, str] = {}
        for row in raw.values():
            ticker = str(row.get("ticker", "")).strip().upper()
            name = str(row.get("title", "")).strip()
            if ticker:
                out[ticker] = name or ticker
        return out

    @staticmethod
    def _load_aliases(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(k): str(v).upper() for k, v in raw.items()}

    def resolve(self, article: Article) -> list[ResolvedEntity]:
        """Return de-duplicated resolved entities from feed tags + aliases.

        Order is preserved: feed-tagged tickers come first in tag order,
        alias hits come after in alias-table iteration order. PR-1 engine
        uses positional order to assign template roles (acquirer = first,
        target = second) so the order contract here is load-bearing.
        """
        seen: set[str] = set()
        out: list[ResolvedEntity] = []

        for raw_ticker in article.tickers_raw or []:
            ticker = str(raw_ticker).strip().upper()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            out.append(
                ResolvedEntity(
                    ticker=ticker,
                    name=self._ticker_to_name.get(ticker, ticker),
                    role="company",
                )
            )

        if self._aliases:
            haystack = f"{article.title}\n{article.body}"
            for phrase, ticker in self._aliases.items():
                if ticker in seen:
                    continue
                # Word-boundary match so "Apple" does not eat "Pineapple".
                # Case-insensitive so "the iPhone maker" hits "The iPhone
                # maker" as written in the headline.
                pattern = r"\b" + re.escape(phrase) + r"\b"
                if re.search(pattern, haystack, re.IGNORECASE):
                    seen.add(ticker)
                    out.append(
                        ResolvedEntity(
                            ticker=ticker,
                            name=self._ticker_to_name.get(ticker, ticker),
                            role="company",
                        )
                    )
        return out
