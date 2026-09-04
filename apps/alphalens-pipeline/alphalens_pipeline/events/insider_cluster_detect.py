"""Live insider-cluster detection for one brief date (event lane, epic #1293).

Reads the Form-4 store, detects the clusters whose event belongs to the brief
date (``insider_cluster.event_brief_date``), fetches the EDGAR acceptance time
of the completing filing through the canonical SEC client, stamps the
pre-registered hard exclusions (fact-based, never a judgment) and writes the
event-candidates parquet. EVERY detected cluster is written — eligible rows are
what ``thematic score`` merges, the rest form the shadow arm (``eligible=False``
with ``exclusion_reason``), so the value of each exclusion stays measurable.

Row shape: the thematic-facing columns a brief needs (``theme``, ``ticker``,
``company_name``, ``rationale``, ``market_cap``, empty gate lists, ``verified``,
``source_event_*``) plus the ``event_*`` facts. It must never carry a scorer
enrichment name (``selection_score``, ``catalyst_template_*``, ``technical_*``,
``scorer_config_version``...) because ``score_candidates`` left-merges its
enrichment on ``ticker`` and pandas would suffix the collision.

Network-facing inputs are injectable (``acceptance_fn``, ``mcap_fn``,
``earnings_fn``, ``sic_fn``) so the rules are unit-tested against a tiny store.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pandas as pd

from alphalens_pipeline.data.parquet_io import write_parquet_atomic
from alphalens_pipeline.events import DEFAULT_ACCEPTANCE_CACHE_DIR
from alphalens_pipeline.events import insider_cluster as ic
from alphalens_pipeline.paper.calendar import DEFAULT_EXCHANGE, advance_trading_sessions
from alphalens_pipeline.thematic.sources.form4_store import (
    DEFAULT_FORM4_ROOT,
    load_form4_partitions,
)

logger = logging.getLogger(__name__)

DEFAULT_COMPANY_TICKERS_PATH = Path.home() / ".alphalens" / "edgar-detect" / "company_tickers.json"
# Legs older than this cannot complete a cluster on the brief date (2-session window)
# nor suppress one through the 20-session dedup; 60 calendar days covers both.
LEGS_LOOKBACK_DAYS = 60

EVENT_CANDIDATE_COLUMNS: tuple[str, ...] = (
    # thematic-shaped (names the scorer / brief / Django already read)
    "theme",
    "ticker",
    "company_name",
    "rationale",
    "llm_confidence",
    "market_cap",
    "gates_passed",
    "gates_passed_str",
    "n_gates_passed",
    "gates_failed",
    "gates_failed_str",
    "n_gates_failed",
    "gates_unknown",
    "gates_unknown_str",
    "n_gates_unknown",
    "verified",
    "gate_verdict_json",
    "source_event_url",
    "source_event_title",
    "source_event_published_at",
    "theme_search_keywords",
    # event lane
    "source",
    "event_n_insiders",
    "event_cluster_usd",
    "event_buyers_json",
    "event_first_leg_date",
    "event_completing_accession",
    "event_acceptance_utc",
    "event_arrival_session",
    "event_filing_lag_bdays",
    "event_sic",
    "event_next_earnings_date",
    "event_gate_version",
    "eligible",
    "exclusion_reason",
)
# Pre-registered order: the FIRST hit is the stamped reason.
EXCLUSION_ORDER = (
    "late_filing",
    "mcap_unknown",
    "mcap_out_of_bracket",
    "sic_excluded",
    "earnings_window",
)

AcceptanceFn = Callable[[str, str, list[str | None]], dt.datetime | None]
McapFn = Callable[[str], float | None]
EarningsFn = Callable[[str], dt.date | None]
SicFn = Callable[[str], int | None]


def load_company_names(path: Path = DEFAULT_COMPANY_TICKERS_PATH) -> dict[str, str]:
    """``ticker -> title`` from the SEC ``company_tickers.json`` cache (empty on any failure)."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, str] = {}
    for row in raw.values():
        ticker = str(row.get("ticker", "")).strip().upper()
        if ticker:
            out[ticker] = str(row.get("title", "")).strip() or ticker
    return out


def load_legs(*, form4_root: Path, asof: dt.date) -> pd.DataFrame:
    """Qualifying legs filed in ``(asof - LEGS_LOOKBACK_DAYS, asof]`` from the store.

    Reads the ``asof`` year and the previous one (a December transaction filed in
    January sits in the earlier ``transaction_year`` partition). The ``<= asof``
    clip gives live and replay runs the same view of the store.
    """
    raw = load_form4_partitions(form4_root=form4_root, years={asof.year, asof.year - 1})
    if raw.empty:
        return pd.DataFrame(columns=["ticker", "filed_date", "usd", "reporting_owner_cik"])
    raw = raw.copy()
    raw["ticker"] = raw["ticker"].astype(str).str.upper()
    legs = ic.qualifying_legs(raw)
    lo = asof - dt.timedelta(days=LEGS_LOOKBACK_DAYS)
    return legs[(legs["filed_date"] > lo) & (legs["filed_date"] <= asof)].reset_index(drop=True)


def _attach_ciks(clusters: pd.DataFrame, legs: pd.DataFrame) -> pd.DataFrame:
    issuer = legs.groupby("ticker")["issuer_cik"].first() if "issuer_cik" in legs.columns else {}
    out = clusters.copy()
    out["issuer_cik"] = [str(issuer.get(t, "")) for t in out["ticker"]]
    out["owner_ciks"] = [
        sorted(
            set(
                legs[(legs["ticker"] == t) & (legs["filed_date"] >= f) & (legs["filed_date"] <= e)][
                    "reporting_owner_cik"
                ].astype(str)
            )
        )
        for t, f, e in zip(out["ticker"], out["first_leg_date"], out["event_date"], strict=True)
    ]
    return out


def select_for_brief_date(
    clusters: pd.DataFrame, *, asof: dt.date, acceptance_fn: AcceptanceFn
) -> pd.DataFrame:
    """Clusters whose event brief date is ``asof``; adds ``acceptance_et``.

    Only clusters completing on ``asof`` or the day before can map to ``asof``
    (pre-open acceptance -> same day, otherwise next calendar day), so the
    acceptance time is fetched for those alone.
    """
    if clusters.empty:
        return clusters.assign(acceptance_et=pd.Series(dtype=object))
    window = {asof, asof - dt.timedelta(days=1)}
    cand = clusters[clusters["event_date"].isin(window)].copy()
    keep: list[bool] = []
    accepted: list[dt.datetime | None] = []
    for row in cand.to_dict("records"):
        acc = acceptance_fn(
            str(row["completing_accession"]), str(row["issuer_cik"]), list(row["owner_ciks"])
        )
        accepted.append(acc)
        keep.append(ic.event_brief_date(row["event_date"], acc) == asof)
    cand["acceptance_et"] = accepted
    return cand[pd.Series(keep, index=cand.index)].reset_index(drop=True)


def apply_exclusions(
    events: pd.DataFrame,
    *,
    mcap_fn: McapFn,
    earnings_fn: EarningsFn,
    sic_fn: SicFn,
    exchange: str = DEFAULT_EXCHANGE,
) -> pd.DataFrame:
    """Stamp the facts each exclusion looks at, then the first reason in ``EXCLUSION_ORDER``.

    Every fact is stamped for every row (eligible or not) so the shadow arm shows
    what the gate saw; the inputs are as observed at detection and never revised.
    """
    out = events.copy()
    arrivals: list[dt.date] = []
    lags: list[int] = []
    caps: list[float | None] = []
    sics: list[int | None] = []
    earnings: list[dt.date | None] = []
    reasons: list[str] = []
    lo, hi = ic.EVENT_MCAP_RANGE
    for row in out.to_dict("records"):
        ticker = str(row["ticker"])
        arrival = ic.arrival_session(row["event_date"], row["acceptance_et"], exchange=exchange)
        lag = ic.filing_lag_bdays(row["completing_transaction_date"], row["event_date"])
        cap = mcap_fn(ticker)
        sic = sic_fn(ticker)
        nxt = earnings_fn(ticker)
        last_excluded_session = advance_trading_sessions(
            arrival, ic.EARNINGS_EXCLUSION_SESSIONS - 1, exchange
        )
        if lag > ic.LATE_FILING_BDAYS:
            reason = "late_filing"
        elif cap is None:
            reason = "mcap_unknown"
        elif cap < lo or cap > hi:
            reason = "mcap_out_of_bracket"
        elif sic is not None and sic in ic.EXCLUDED_SIC:
            reason = "sic_excluded"
        elif nxt is not None and nxt <= last_excluded_session:
            reason = "earnings_window"
        else:
            reason = ""
        arrivals.append(arrival)
        lags.append(lag)
        caps.append(cap)
        sics.append(sic)
        earnings.append(nxt)
        reasons.append(reason)
    out["arrival_session"] = arrivals
    out["filing_lag_bdays"] = lags
    out["market_cap"] = caps
    out["sic"] = sics
    out["next_earnings_date"] = earnings
    out["exclusion_reason"] = reasons
    out["eligible"] = [r == "" for r in reasons]
    return out


def _empty_frame() -> pd.DataFrame:
    df = pd.DataFrame(columns=list(EVENT_CANDIDATE_COLUMNS))
    df["verified"] = df["verified"].astype(bool)
    df["eligible"] = df["eligible"].astype(bool)
    return df


def _row(ev: Mapping[Any, Any], legs: pd.DataFrame, names: Mapping[str, str]) -> dict[str, Any]:
    ticker = str(ev["ticker"])
    title = ic.cluster_title(
        n_insiders=int(ev["n_insiders"]),
        cluster_usd=float(ev["cluster_usd"]),
        first_leg_date=ev["first_leg_date"],
        event_date=ev["event_date"],
    )
    buyers = ic.cluster_buyers(
        legs, ticker=ticker, first_leg_date=ev["first_leg_date"], event_date=ev["event_date"]
    )
    accepted_utc = ic.acceptance_to_utc_iso(ev["acceptance_et"])
    return {
        "theme": ic.SOURCE_INSIDER_CLUSTER,
        "ticker": ticker,
        "company_name": names.get(ticker) or ticker,
        "rationale": title,
        "llm_confidence": None,
        "market_cap": ev["market_cap"],
        "gates_passed": [],
        "gates_passed_str": "",
        "n_gates_passed": 0,
        "gates_failed": [],
        "gates_failed_str": "",
        "n_gates_failed": 0,
        "gates_unknown": [],
        "gates_unknown_str": "",
        "n_gates_unknown": 0,
        "verified": True,
        "gate_verdict_json": "{}",
        "source_event_url": ic.filing_index_url(
            str(ev["issuer_cik"]), str(ev["completing_accession"])
        ),
        "source_event_title": title,
        "source_event_published_at": accepted_utc or ev["event_date"].isoformat(),
        "theme_search_keywords": [],
        "source": ic.SOURCE_INSIDER_CLUSTER,
        "event_n_insiders": int(ev["n_insiders"]),
        "event_cluster_usd": float(ev["cluster_usd"]),
        "event_buyers_json": json.dumps(buyers),
        "event_first_leg_date": ev["first_leg_date"],
        "event_completing_accession": ev["completing_accession"],
        "event_acceptance_utc": accepted_utc,
        "event_arrival_session": ev["arrival_session"],
        "event_filing_lag_bdays": int(ev["filing_lag_bdays"]),
        "event_sic": ev["sic"],
        "event_next_earnings_date": ev["next_earnings_date"],
        "event_gate_version": ic.EVENT_GATE_VERSION,
        "eligible": bool(ev["eligible"]),
        "exclusion_reason": str(ev["exclusion_reason"]),
    }


def _default_acceptance_fn(client, cache_dir: Path) -> AcceptanceFn:
    def fetch(
        accession: str, issuer_cik: str, fallback_ciks: list[str | None]
    ) -> dt.datetime | None:
        nonlocal client
        if client is None:
            from alphalens_pipeline.data.alt_data.sec_edgar_client import get_default_sec_client

            client = get_default_sec_client()
        return ic.fetch_acceptance(
            accession, issuer_cik, client, cache_dir=cache_dir, fallback_ciks=fallback_ciks
        )

    return fetch


def _default_mcap_fn(asof: dt.date) -> McapFn:
    def fetch(ticker: str) -> float | None:
        from alphalens_pipeline.thematic.verification.mcap_filter import fetch_mcap

        return fetch_mcap(ticker, asof=asof)

    return fetch


def _default_earnings_fn(asof: dt.date) -> EarningsFn:
    def fetch(ticker: str) -> dt.date | None:
        from alphalens_pipeline.thematic.sources.earnings_calendar import fetch_next_earnings

        return fetch_next_earnings(ticker=ticker, asof=asof)

    return fetch


def _default_sic_fn() -> SicFn:
    from alphalens_pipeline.data.fundamentals.sic_index import get_sic

    return get_sic


def build_event_candidates(
    *,
    asof: dt.date,
    form4_root: Path = DEFAULT_FORM4_ROOT,
    acceptance_cache_dir: Path = DEFAULT_ACCEPTANCE_CACHE_DIR,
    client=None,
    acceptance_fn: AcceptanceFn | None = None,
    mcap_fn: McapFn | None = None,
    earnings_fn: EarningsFn | None = None,
    sic_fn: SicFn | None = None,
    company_names: Mapping[str, str] | None = None,
    exchange: str = DEFAULT_EXCHANGE,
) -> pd.DataFrame:
    """All clusters whose event belongs to brief date ``asof``, eligible or not.

    Deterministic for a given store state (sorted by ticker), so the six daily
    build slots rewrite the same parquet; rows only ever appear as the store
    catches up. Returns the typed empty frame when nothing qualifies.
    """
    legs = load_legs(form4_root=form4_root, asof=asof)
    clusters = ic.detect_clusters(legs, exchange=exchange)
    if clusters.empty:
        return _empty_frame()
    clusters = _attach_ciks(clusters, legs)
    selected = select_for_brief_date(
        clusters,
        asof=asof,
        acceptance_fn=acceptance_fn or _default_acceptance_fn(client, acceptance_cache_dir),
    )
    if selected.empty:
        return _empty_frame()
    facts = apply_exclusions(
        selected,
        mcap_fn=mcap_fn or _default_mcap_fn(asof),
        earnings_fn=earnings_fn or _default_earnings_fn(asof),
        sic_fn=sic_fn or _default_sic_fn(),
        exchange=exchange,
    )
    names = company_names if company_names is not None else load_company_names()
    rows = [_row(ev, legs, names) for ev in facts.to_dict("records")]
    df = pd.DataFrame(rows, columns=list(EVENT_CANDIDATE_COLUMNS))
    df = df.sort_values("ticker", kind="stable").reset_index(drop=True)
    df["verified"] = df["verified"].astype(bool)
    df["eligible"] = df["eligible"].astype(bool)
    return df


def write_event_candidates(df: pd.DataFrame, *, asof: dt.date, output_dir: Path) -> Path:
    """Write ``<output_dir>/<asof>.parquet`` atomically and return the path."""
    path = output_dir / f"{asof.isoformat()}.parquet"
    write_parquet_atomic(df, path, index=False)
    return path
