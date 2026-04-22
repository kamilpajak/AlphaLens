"""PIT insider-cluster scorer.

Ties M0 (ticker↔CIK) + M1 (EDGAR client) + M2a (XML parse) + M2b (filter)
+ M4 (cluster detection) into a single ``features_as_of(ticker, asof)``
query mirroring ``alphalens.fundamentals.simfin_store.features_as_of``.

PIT discipline is enforced at the filings-index step: only filings whose
``filingDate ≤ asof`` are fetched and considered, so transactions that
had occurred but not yet been filed publicly are excluded.

A small per-``(ticker, asof)`` disk cache (JSON) avoids re-fetching on
repeat queries during a backtest sweep. ``None`` results are cached
explicitly so an empty cluster doesn't look like a cache miss.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from alphalens.alt_data.form4_filter import filter_eligible
from alphalens.alt_data.form4_records import Form4ParseError, parse_form4_xml
from alphalens.alt_data.sec_edgar_client import SecEdgarClient, SecEdgarError
from alphalens.alt_data.ticker_cik_map import TickerCikMap
from alphalens.screeners.insider.cluster import detect_cluster

logger = logging.getLogger(__name__)

# Fetch a little more than the cluster window so the cluster detector sees
# everything it needs. 60d = 30d window + safety for late-filed transactions.
_LOOKBACK_DAYS = 60


def _default_cache_root() -> Path:
    return Path.home() / ".alphalens" / "insider_form4"


@dataclass
class _ScorerConfig:
    window_days: int = 30
    min_distinct_insiders: int = 3
    plan_age_threshold_days: int = 90


class InsiderScorer:
    def __init__(
        self,
        edgar_client: SecEdgarClient,
        ticker_cik_map: TickerCikMap,
        cache_dir: Path | None = None,
        config: _ScorerConfig | None = None,
    ):
        self._edgar = edgar_client
        self._cik_map = ticker_cik_map
        self._cache_dir = cache_dir
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self._cfg = config or _ScorerConfig()

    def features_as_of(self, ticker: str, asof: date) -> dict | None:
        cached = self._cache_load(ticker, asof)
        if cached is not None:
            return cached["features"]

        cik = self._cik_map.lookup(ticker)
        if cik is None:
            self._cache_store(ticker, asof, None)
            return None

        features = self._compute(ticker, cik, asof)
        self._cache_store(ticker, asof, features)
        return features

    def _compute(self, ticker: str, cik: str, asof: date) -> dict | None:
        try:
            submissions = self._edgar.fetch_submissions(cik)
        except SecEdgarError as exc:
            logger.warning("edgar submissions fetch failed for %s: %s", ticker, exc)
            return None

        cutoff = asof - timedelta(days=_LOOKBACK_DAYS)
        filings = _iter_form4_filings(submissions, asof=asof, min_filing_date=cutoff)

        records = []
        for f in filings:
            try:
                xml = self._edgar.fetch_form4_xml(
                    cik=cik,
                    accession_number=f["accession"],
                    primary_doc=f["primary"],
                )
                records.extend(
                    parse_form4_xml(
                        xml,
                        accession_number=f["accession"],
                        filing_date=f["filing_date"],
                    )
                )
            except (SecEdgarError, Form4ParseError) as exc:
                logger.warning(
                    "skipping Form 4 %s for %s: %s", f["accession"], ticker, exc
                )

        eligible = filter_eligible(records)
        cluster = detect_cluster(
            eligible,
            asof=asof,
            window_days=self._cfg.window_days,
            min_distinct_insiders=self._cfg.min_distinct_insiders,
            plan_age_threshold_days=self._cfg.plan_age_threshold_days,
        )
        if cluster is None:
            return None
        return {
            "insider_count": cluster.insider_count,
            "aggregate_dollar": float(cluster.aggregate_dollar),
            "cluster_window_days": self._cfg.window_days,
            "asof": asof.isoformat(),
        }

    def _cache_path(self, ticker: str, asof: date) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / f"{ticker.upper()}_{asof.isoformat()}.json"

    def _cache_load(self, ticker: str, asof: date) -> dict | None:
        path = self._cache_path(ticker, asof)
        if path is None or not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("insider cache corrupt for %s@%s: %s", ticker, asof, exc)
            return None

    def _cache_store(self, ticker: str, asof: date, features: dict | None) -> None:
        path = self._cache_path(ticker, asof)
        if path is None:
            return
        payload = {
            "features": features,
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(payload))


def _iter_form4_filings(
    submissions: dict, *, asof: date, min_filing_date: date
) -> list[dict]:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    filing_dates = recent.get("filingDate") or []
    primary_docs = recent.get("primaryDocument") or []

    out: list[dict] = []
    for form, accession, fd_str, primary in zip(
        forms, accessions, filing_dates, primary_docs
    ):
        if form not in {"4", "4/A"}:
            continue
        try:
            fd = date.fromisoformat(fd_str)
        except ValueError:
            continue
        if fd > asof or fd < min_filing_date:
            continue
        out.append({
            "form": form,
            "accession": accession,
            "filing_date": fd,
            "primary": primary,
        })
    return out
