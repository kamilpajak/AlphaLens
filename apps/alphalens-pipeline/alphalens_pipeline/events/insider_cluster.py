"""Insider purchase clusters — the pure rules of the event lane (epic #1293).

Frozen spec: ``docs/research/preregistration/params_insider_cluster_forward_2026_09.json``
(a parity test pins every constant below to it). Promoted from the stage-1
research helpers (``alphalens_research.diagnostics.insider_cluster_retro``,
which now re-exports these names) so the VPS pipeline image — which carries no
research code — can run the live detection.

Pure functions over pandas frames: leg qualification, cluster detection (the
signal is the COMPLETION of the cluster — the filing of the second distinct
insider), the arrival rule keyed on the EDGAR acceptance time, the brief-date
mapping that makes the population monitor's ladder anchor coincide with the
event arrival, and the fact-shaped catalyst text. The only I/O here is the
cached acceptance-time fetch through an injected SEC client.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    session_on_or_after,
)

SOURCE_INSIDER_CLUSTER = "insider_cluster"
# Bump on ANY change to the leg / cluster / exclusion rules: cohorts are never
# pooled across gate versions (pre-registration section 5).
EVENT_GATE_VERSION = "insider_cluster_gate_v1"

# Frozen spec constants (mirrored in the params JSON; a test pins parity).
LEG_MIN_USD = 10_000.0
CLUSTER_MIN_USD = 100_000.0
CLUSTER_MIN_USD_CHECK = 250_000.0
CLUSTER_WINDOW_SESSIONS = 2
CLUSTER_MIN_INSIDERS = 2
DEDUP_SESSIONS = 20
LATE_FILING_BDAYS = 10
PRE_OPEN_CUTOFF_ET = dt.time(9, 0)
EVENT_MCAP_RANGE = (500_000_000, 10_000_000_000)  # == thematic DEFAULT_MCAP_RANGE (test-pinned)
EARNINGS_EXCLUSION_SESSIONS = 10  # arrival + 0..9
EXCLUDED_SIC = frozenset({6722, 6726, 6770})  # investment offices, unit trusts, blank checks
HORIZON_SESSIONS_PRIMARY = 19  # arrival + 19 = 20 sessions inclusive
HORIZON_SESSIONS_SECONDARY = 39
SPLIT_RATIO_LO, SPLIT_RATIO_HI = 0.55, 1.8
FEE_ROUND_TRIP = 0.0066  # Saxo LIVE schedule: 0.08%/side + 0.25% FX each way on a fixed ticket

ACCEPT_RE = re.compile(r"<ACCEPTANCE-DATETIME>\s*(\d{14})")
CLUSTER_COLUMNS = (
    "ticker",
    "event_date",
    "first_leg_date",
    "n_insiders",
    "cluster_usd",
    "completing_accession",
    "completing_transaction_date",
)
_ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)


def qualifying_legs(form4: pd.DataFrame, *, leg_min_usd: float = LEG_MIN_USD) -> pd.DataFrame:
    """Open-market purchase legs by officers/directors, priced, >= ``leg_min_usd``.

    Ten-percent owners count only if they are also an officer or director (the
    flags AS FILED). Amendments are dropped (the original filing already carried
    the leg).
    """
    df = form4
    mask = (
        (df["transaction_code"] == "P")
        & (df["acquired_disposed"] == "A")
        & (~df["is_amendment"].fillna(False).astype(bool))
        & (
            df["is_officer"].fillna(False).astype(bool)
            | df["is_director"].fillna(False).astype(bool)
        )
        & df["transaction_price_per_share"].notna()
        & df["transaction_shares"].notna()
    )
    out = df.loc[mask].copy()
    out["usd"] = out["transaction_shares"].astype(float) * out[
        "transaction_price_per_share"
    ].astype(float)
    out = out[out["usd"] >= leg_min_usd].copy()  # explicit copy after the filter
    out["filed_date"] = pd.to_datetime(out["filed_date"]).dt.date
    out["transaction_date"] = pd.to_datetime(out["transaction_date"]).dt.date
    return out.reset_index(drop=True)


def detect_clusters(
    legs: pd.DataFrame,
    *,
    window_sessions: int = CLUSTER_WINDOW_SESSIONS,
    min_insiders: int = CLUSTER_MIN_INSIDERS,
    min_usd: float = CLUSTER_MIN_USD,
    dedup_sessions: int = DEDUP_SESSIONS,
    exchange: str = DEFAULT_EXCHANGE,
) -> pd.DataFrame:
    """One row per cluster event: the first session at which the condition is observable.

    A cluster is >= ``min_insiders`` DISTINCT ``reporting_owner_cik`` whose legs
    were filed within ``window_sessions`` trading sessions of the anchor leg
    (inclusive: distance 0..``window_sessions``) and whose USD sum is
    >= ``min_usd``. ``event_date`` is the ``filed_date`` of the leg that completes
    the insider count — the k-th distinct CIK in (filed_date, accession_number)
    order, so same-day ties are deterministic. One event per ticker per
    ``dedup_sessions``; the first wins.
    """
    if legs.empty:
        return pd.DataFrame(columns=list(CLUSTER_COLUMNS))
    per = (
        legs.groupby(["ticker", "reporting_owner_cik", "filed_date"], as_index=False)
        .agg(
            usd=("usd", "sum"),
            accession_number=("accession_number", "first"),
            transaction_date=("transaction_date", "min"),
        )
        .sort_values(["ticker", "filed_date", "accession_number"], kind="stable")
    )
    rows = []
    for ticker, grp in per.groupby("ticker", sort=False):
        g = grp.reset_index(drop=True)
        last_event: dt.date | None = None
        for i in range(len(g)):
            d0 = g.filed_date[i]
            hi = advance_trading_sessions(
                session_on_or_after(d0, exchange), window_sessions, exchange
            )
            w = g[(g.filed_date >= d0) & (g.filed_date <= hi)]
            if w.reporting_owner_cik.nunique() < min_insiders or w.usd.sum() < min_usd:
                continue
            distinct = w.drop_duplicates("reporting_owner_cik")  # first appearance per CIK
            completing = distinct.iloc[min_insiders - 1]
            ev_date = completing.filed_date
            # first-wins dedup: a completion inside [last_event, last_event + dedup_sessions]
            # (inclusive) is the same episode; the re-anchoring of the same cluster at its
            # later legs lands here too.
            if last_event is not None and ev_date <= advance_trading_sessions(
                session_on_or_after(last_event, exchange), dedup_sessions, exchange
            ):
                continue
            last_event = ev_date
            rows.append(
                {
                    "ticker": ticker,
                    "event_date": ev_date,
                    "first_leg_date": d0,
                    "n_insiders": int(w.reporting_owner_cik.nunique()),
                    "cluster_usd": float(w.usd.sum()),
                    "completing_accession": completing.accession_number,
                    "completing_transaction_date": completing.transaction_date,
                }
            )
    return pd.DataFrame(rows, columns=list(CLUSTER_COLUMNS))


def cluster_buyers(
    legs: pd.DataFrame, *, ticker: str, first_leg_date: dt.date, event_date: dt.date
) -> list[dict]:
    """The distinct insiders behind one cluster, JSON-shaped (fact-only catalyst detail).

    One entry per ``reporting_owner_cik`` with qualifying legs filed inside
    ``[first_leg_date, event_date]``: name as reported on the filing, role from
    the filing flags, USD summed over the insider's legs, first filing date.
    Sorted by (filed_date, cik) so the output is deterministic.
    """
    w = legs[
        (legs["ticker"] == ticker)
        & (legs["filed_date"] >= first_leg_date)
        & (legs["filed_date"] <= event_date)
    ]
    out: list[dict] = []
    for cik, g in w.groupby("reporting_owner_cik", sort=False):
        officer = bool(g["is_officer"].fillna(False).astype(bool).any())
        director = bool(g["is_director"].fillna(False).astype(bool).any())
        role = (
            "officer_director" if officer and director else ("officer" if officer else "director")
        )
        name = (
            str(g["reporting_owner_name"].dropna().iloc[0])
            if "reporting_owner_name" in g.columns and g["reporting_owner_name"].notna().any()
            else None
        )
        out.append(
            {
                "cik": str(cik),
                "name": name,
                "role": role,
                "usd": float(g["usd"].sum()),
                "filed_date": min(g["filed_date"]).isoformat(),
            }
        )
    return sorted(out, key=lambda b: (b["filed_date"], b["cik"]))


def arrival_session(
    filed_date: dt.date, acceptance_et: dt.datetime | None, *, exchange: str = DEFAULT_EXCHANGE
) -> dt.date:
    """First session a follower can trade after the filing became public.

    Accepted before 09:00 ET on a session day -> that session's OPEN is
    obtainable; anything else (intraday, post-close, or unknown) -> the next
    session's OPEN. A filing accepted on a NON-session weekday (an exchange
    holiday such as Good Friday) is public before the next session's open
    whatever its acceptance time, so that session is the arrival.
    """
    base = session_on_or_after(filed_date, exchange)
    if base != filed_date:
        return base
    if (
        acceptance_et is not None
        and acceptance_et.date() == filed_date
        and acceptance_et.time() < PRE_OPEN_CUTOFF_ET
    ):
        return base
    return advance_trading_sessions(base, 1, exchange)


def event_brief_date(filed_date: dt.date, acceptance_et: dt.datetime | None) -> dt.date:
    """The brief date ``D`` an event row belongs to.

    Chosen so that ``session_on_or_after(D) == arrival_session(filed_date, acceptance)``:
    the population monitor anchors every ladder at ``session_on_or_after(brief_date)``,
    so the event anchor and the ladder anchor coincide by construction. Accepted
    before 09:00 ET on the filing date -> ``D = F``; otherwise ``D = F + 1``
    calendar day (a Friday after-close filing lands on the Saturday brief and
    arrives Monday; the Sunday and Monday briefs never claim it).
    """
    if (
        acceptance_et is not None
        and acceptance_et.date() == filed_date
        and acceptance_et.time() < PRE_OPEN_CUTOFF_ET
    ):
        return filed_date
    return filed_date + dt.timedelta(days=1)


def filing_lag_bdays(transaction_date: dt.date, filed_date: dt.date) -> int:
    """Business days between the transaction and its filing (weekends only, no holidays)."""
    return int(np.busday_count(transaction_date, filed_date))


def acceptance_to_utc_iso(acceptance_et: dt.datetime | None) -> str | None:
    """EDGAR acceptance (naive, America/New_York) -> ISO-8601 UTC string, or None."""
    if acceptance_et is None:
        return None
    return acceptance_et.replace(tzinfo=_ET).astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def filing_index_url(cik: str, accession: str) -> str:
    """The EDGAR filing-index page of one accession (the human-readable landing page)."""
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession.replace('-', '')}/{accession}-index.htm"
    )


def _usd_compact(usd: float) -> str:
    if usd >= 1_000_000:
        return f"${usd / 1_000_000:.1f}M"
    return f"${usd / 1_000:.0f}k"


def cluster_title(
    *, n_insiders: int, cluster_usd: float, first_leg_date: dt.date, event_date: dt.date
) -> str:
    """Fact-only catalyst headline for the brief prompt and the card (no judgment words)."""
    when = (
        f"on {event_date.isoformat()}"
        if first_leg_date == event_date
        else f"between {first_leg_date.isoformat()} and {event_date.isoformat()}"
    )
    return (
        f"Insider purchase cluster: {n_insiders} officers/directors bought "
        f"{_usd_compact(cluster_usd)} of stock on the open market {when} (SEC Form 4)"
    )


def accession_urls(accession: str, ciks: list[str | None]) -> list[str]:
    """Candidate full-submission URLs for a filing.

    EDGAR files a Form 4 under the issuer AND each reporting-owner CIK, but the
    Archives path is not always present under the issuer — try every CIK we know,
    in order, without duplicates.
    """
    acc = accession.replace("-", "")
    seen: set[str] = set()
    urls = []
    for cik in ciks:
        if cik and str(cik) not in seen:
            seen.add(str(cik))
            urls.append(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{accession}.txt")
    return urls


def fetch_acceptance(
    accession: str,
    cik: str,
    client,
    *,
    cache_dir: Path,
    fallback_ciks: list[str | None] | None = None,
) -> dt.datetime | None:
    """EDGAR ``<ACCEPTANCE-DATETIME>`` of a filing, cached per accession.

    The raw header text is persisted before parsing. Tries the issuer CIK path
    first, then the reporting-owner paths. A cached miss caused by a missing
    Archives key is retried once fallbacks are supplied. Unknown -> ``None``
    (the caller maps that to the conservative next-session arrival). ``client``
    is the canonical SEC client (``get_default_sec_client()``) or a test double
    exposing ``get_text(url)``.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{accession}.json"
    if cache.exists():
        payload = json.loads(cache.read_text())
        v = payload.get("acceptance")
        retryable = (
            v is None
            and bool(fallback_ciks)
            and "NoSuchKey" in (payload.get("error") or "")
            and not payload.get("fallback_tried")
        )
        if not retryable:
            return dt.datetime.strptime(v, "%Y%m%d%H%M%S") if v else None
    last_err = None
    for url in accession_urls(accession, [cik, *(fallback_ciks or [])]):
        try:
            text = client.get_text(url)[:4000]
        except Exception as exc:  # next candidate URL; unknown -> conservative arrival
            last_err = f"{type(exc).__name__}: {str(exc)[:200]}"
            continue
        m = ACCEPT_RE.search(text)
        cache.write_text(
            json.dumps({"acceptance": m.group(1) if m else None, "header": text[:600], "url": url})
        )
        return dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S") if m else None
    log.warning("acceptance fetch failed %s: %s", accession, last_err)
    cache.write_text(
        json.dumps({"acceptance": None, "error": last_err, "fallback_tried": bool(fallback_ciks)})
    )
    return None
