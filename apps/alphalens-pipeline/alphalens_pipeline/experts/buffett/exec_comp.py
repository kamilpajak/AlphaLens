"""DEF 14A executive-compensation reader — SEC pay-versus-performance (#507 PR-7b).

The SEC pay-versus-performance rule (effective for fiscal years ending on/after
2022-12-16) discloses CEO ("PEO") and average-other-NEO compensation as structured
XBRL under the ``ecd`` taxonomy. Those tags are ABSENT from the per-company
``companyfacts`` JSON but PRESENT in the cross-sectional **frames** API
(``/api/xbrl/frames/ecd/<concept>/USD/CY<year>.json`` — one concept across all
filers for a calendar year). This module reads them through the canonical
:class:`SecEdgarClient`, computes the CEO-to-NEO pay ratio in Python (never the
LLM), and reports a coverage enum so missing data is an honest ``None`` + reason,
never a fabricated zero.

POINT-IN-TIME — frame rows carry no ``filed`` timestamp, only an ``accn``
(accession). True PIT (``accepted <= asof``) is therefore resolved by joining the
``accn`` to its acceptance datetime in the issuer's submissions JSON (the exact-
accn mechanism). A row whose proxy was accepted after ``asof`` is excluded.

Non-calendar fiscal-year filers can't be mapped onto a ``CY`` frame soundly, so
they are reported ``UNKNOWN_NON_CALENDAR_FY`` (never mislabeled "not disclosed").
The reader is additive + opt-in (consumed only by the Buffett lens); it fails
soft — a fetch error degrades to ``NOT_DISCLOSED`` with all-``None`` numerics.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from alphalens_pipeline.data.alt_data.sec_edgar_client import SecEdgarClient

logger = logging.getLogger(__name__)

# ecd pay-versus-performance concepts (USD), confirmed live in the frames API.
_PEO_TOTAL = "PeoTotalCompAmt"
_PEO_CAP = "PeoActuallyPaidCompAmt"
_NEO_TOTAL = "NonPeoNeoAvgTotalCompAmt"
_NEO_CAP = "NonPeoNeoAvgCompActuallyPaidAmt"
_CONCEPTS = (_PEO_TOTAL, _PEO_CAP, _NEO_TOTAL, _NEO_CAP)

_TAXONOMY = "ecd"
_UNIT = "USD"

# First fiscal year with PvP data (FY2022, filed in 2023 proxies → CY2022 frame).
_ECD_FIRST_DATA_YEAR = 2022
# Scan the two most-recent calendar years before asof; the PIT filter drops any
# whose proxy isn't yet accepted, so the freshest eligible year wins.
_SCAN_WINDOW_YEARS = 2

_DEFAULT_FRAME_CACHE_DIR = Path.home() / ".alphalens" / "sec_frames"


class ExecCompCoverage(StrEnum):
    """Why exec-comp is or isn't available — so a ``None`` is never read as a bug."""

    PRESENT = "present"
    PRE_2023_NOT_REQUIRED = "pre_2023_not_required"  # asof predates the PvP rule
    NOT_DISCLOSED = "not_disclosed"  # calendar filer, required window, absent / fetch failed
    UNKNOWN_NON_CALENDAR_FY = "unknown_non_calendar_fy"  # off-Dec FYE, CY-frame mapping unsound


@dataclass(frozen=True)
class ExecCompFacts:
    """One filer's pay-versus-performance facts. Numerics ``None`` unless PRESENT."""

    cik: str
    coverage: ExecCompCoverage
    fiscal_year: int | None = None
    accn: str | None = None
    accepted: dt.datetime | None = None
    peo_total_comp: float | None = None
    peo_actually_paid: float | None = None
    neo_avg_total_comp: float | None = None
    neo_avg_actually_paid: float | None = None
    peo_to_neo_ratio: float | None = None


def _missing(cik: str, coverage: ExecCompCoverage) -> ExecCompFacts:
    return ExecCompFacts(cik=cik, coverage=coverage)


def _scan_years(asof: dt.date) -> list[int]:
    """Calendar years to scan, newest-first, bounded to >= the first PvP data year."""
    window = [asof.year - offset for offset in range(1, _SCAN_WINDOW_YEARS + 1)]
    return sorted((y for y in window if y >= _ECD_FIRST_DATA_YEAR), reverse=True)


def _parse_accepted(value: str | None) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _row_accepted(accepts: list, dates: list, i: int) -> dt.datetime | None:
    """Acceptance datetime for submission row ``i``.

    ``acceptanceDateTime`` is primary; the date-only ``filingDate`` (treated as
    end-of-day) is the fallback. A non-ISO ``filingDate`` resolves to ``None`` —
    conservative fail-soft (no look-ahead), acceptable because SEC submissions use
    ISO dates.
    """
    accepted = _parse_accepted(accepts[i] if i < len(accepts) else None)
    if accepted is not None:
        return accepted
    if i >= len(dates):
        return None
    day = _parse_accepted(dates[i])  # date-only ISO parses to midnight
    if day is not None:
        return day
    try:
        return dt.datetime.combine(dt.date.fromisoformat(dates[i]), dt.time.max)
    except ValueError:
        return None


class _AcceptedResolver:
    """``accn -> acceptance datetime`` over the recent block + overflow shards.

    Overflow shards are walked lazily only when an accn is not already indexed
    from the recent block; an unreadable shard is logged and skipped (fail-soft).
    """

    def __init__(self, subs: dict, client: SecEdgarClient) -> None:
        self._client = client
        self._index: dict[str, dt.datetime] = {}
        self._ingest(subs)
        self._shards = subs.get("filings", {}).get("files") or []

    def _ingest(self, block: dict) -> None:
        recent = block.get("filings", {}).get("recent", block.get("recent", block))
        accns = recent.get("accessionNumber") or []
        accepts = recent.get("acceptanceDateTime") or []
        dates = recent.get("filingDate") or []
        for i, accn in enumerate(accns):
            accepted = _row_accepted(accepts, dates, i)
            if accepted is not None:
                self._index.setdefault(accn, accepted)

    def resolve(self, accn: str) -> dt.datetime | None:
        if accn in self._index:
            return self._index[accn]
        for shard in self._shards:
            name = shard.get("name") if isinstance(shard, dict) else None
            if not name:
                continue
            try:
                self._ingest(self._client.fetch_submissions_overflow(name))
            except Exception as exc:  # fail-soft: an unreadable shard is not fatal
                logger.warning("exec_comp: overflow shard %s failed: %s", name, exc)
                continue
            if accn in self._index:
                return self._index[accn]
        return None


def _build_accepted_resolver(subs: dict, client: SecEdgarClient):
    """Return an ``accn -> acceptance datetime`` resolver callable (see
    :class:`_AcceptedResolver`)."""
    return _AcceptedResolver(subs, client).resolve


def _load_frame(client: SecEdgarClient, concept: str, year: int, cache_dir: Path) -> dict:
    """Fetch one ecd frame, disk-cached under ``cache_dir`` (across-process layer)."""
    path = cache_dir / f"{_TAXONOMY}_{concept}_{_UNIT}_CY{year}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            pass  # corrupt cache file → refetch
    data = client.fetch_xbrl_frame(_TAXONOMY, concept, _UNIT, f"CY{year}")
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError as exc:
        logger.warning("exec_comp: could not write frame cache %s: %s", path, exc)
    return data


def _eligible_rows(frame: dict, cik_int: int, resolve, asof: dt.date) -> list[dict]:
    """Rows for ``cik`` whose proxy was accepted on/before ``asof`` (PIT filter)."""
    out: list[dict] = []
    for row in frame.get("data", []):
        try:
            if int(row.get("cik", -1)) != cik_int:
                continue
        except (TypeError, ValueError):
            continue
        accepted = resolve(row.get("accn", ""))
        if accepted is None or accepted.date() > asof:
            continue  # exclude unverifiable or look-ahead rows
        out.append(row)
    return out


@dataclass(frozen=True)
class _YearScan:
    """Per-year accumulation across the four ecd concepts (PIT-filtered rows)."""

    # Mapping (not dict): frozen guards attribute rebinding only — the annotation
    # signals these are read-only after construction (consumers never mutate them).
    values: Mapping[str, float | None]
    present: Mapping[str, bool]
    ambiguous: bool  # >1 eligible row for some concept (e.g. mid-year CEO change)
    accn: str | None
    accepted: dt.datetime | None


def _is_non_calendar_fye(subs: dict) -> bool:
    """True for an off-December fiscal-year-end (CY-frame mapping is unsound)."""
    fye = str(subs.get("fiscalYearEnd") or "1231")
    return len(fye) >= 2 and fye[:2].isdigit() and int(fye[:2]) != 12


def _scan_year_concepts(
    client: SecEdgarClient,
    cik_int: int,
    resolve,
    asof: dt.date,
    year: int,
    cache_dir: Path,
) -> _YearScan:
    """Fold the four ecd concept frames for one ``year`` into a :class:`_YearScan`.

    A single eligible row yields its value; >1 row marks the year ``ambiguous`` (we
    refuse to pick a CEO); 0 rows leaves the value ``None``. ``accn``/``accepted``
    come from the first concept (in ``_CONCEPTS`` order) that has any eligible row.
    """
    values: dict[str, float | None] = {}
    present: dict[str, bool] = {}
    ambiguous = False
    accn: str | None = None
    accepted: dt.datetime | None = None
    for concept in _CONCEPTS:
        frame = _load_frame(client, concept, year, cache_dir)
        rows = _eligible_rows(frame, cik_int, resolve, asof)
        present[concept] = len(rows) >= 1
        if len(rows) == 1:
            values[concept] = _as_float(rows[0].get("val"))
        else:
            if len(rows) > 1:  # mid-year CEO change etc. — don't pick one
                ambiguous = True
            values[concept] = None
        if accn is None and rows:
            accn = rows[0].get("accn")
            accepted = resolve(accn or "")
    return _YearScan(values, present, ambiguous, accn, accepted)


def _facts_for_year(cik: str, year: int, scan: _YearScan) -> ExecCompFacts | None:
    """Build PRESENT facts for ``year`` if it disclosed BOTH total concepts, else None."""
    # Year qualifies once it disclosed BOTH total concepts (an eligible row
    # existed — single or ambiguous-multi).
    if not (scan.present.get(_PEO_TOTAL) and scan.present.get(_NEO_TOTAL)):
        return None
    ratio = (
        None if scan.ambiguous else _ratio(scan.values.get(_PEO_TOTAL), scan.values.get(_NEO_TOTAL))
    )
    return ExecCompFacts(
        cik=cik,
        coverage=ExecCompCoverage.PRESENT,
        fiscal_year=year,
        accn=scan.accn,
        accepted=scan.accepted,
        peo_total_comp=scan.values.get(_PEO_TOTAL),
        peo_actually_paid=scan.values.get(_PEO_CAP),
        neo_avg_total_comp=scan.values.get(_NEO_TOTAL),
        neo_avg_actually_paid=scan.values.get(_NEO_CAP),
        peo_to_neo_ratio=ratio,
    )


def exec_comp_as_of(
    cik: str,
    asof: dt.date,
    *,
    client: SecEdgarClient,
    frame_cache_dir: Path | None = None,
) -> ExecCompFacts:
    """Resolve one filer's PvP exec-comp as of ``asof`` (exact-accn PIT, fail-soft).

    Returns an :class:`ExecCompFacts` whose ``coverage`` explains any missing data.
    All numerics are ``None`` unless ``coverage == PRESENT``; the ratio is computed
    in Python. Never raises — a fetch error degrades to ``NOT_DISCLOSED``.
    """
    cik = str(cik)
    cache_dir = frame_cache_dir or _DEFAULT_FRAME_CACHE_DIR
    try:
        cik_int = int(cik)
    except (TypeError, ValueError):
        return _missing(cik, ExecCompCoverage.NOT_DISCLOSED)

    years = _scan_years(asof)
    if not years:
        return _missing(cik, ExecCompCoverage.PRE_2023_NOT_REQUIRED)

    try:
        subs = client.fetch_submissions(cik)
        if _is_non_calendar_fye(subs):
            return _missing(cik, ExecCompCoverage.UNKNOWN_NON_CALENDAR_FY)

        resolve = _build_accepted_resolver(subs, client)
        for year in years:
            scan = _scan_year_concepts(client, cik_int, resolve, asof, year, cache_dir)
            facts = _facts_for_year(cik, year, scan)
            if facts is not None:
                return facts
        return _missing(cik, ExecCompCoverage.NOT_DISCLOSED)
    except Exception as exc:  # fail-soft: never break the panel build
        logger.warning("exec_comp: failed for cik %s: %s", cik, exc, exc_info=True)
        return _missing(cik, ExecCompCoverage.NOT_DISCLOSED)


def _as_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ratio(peo_total: float | None, neo_total: float | None) -> float | None:
    if peo_total is None or neo_total is None or neo_total <= 0:
        return None
    return peo_total / neo_total


__all__ = ["ExecCompCoverage", "ExecCompFacts", "exec_comp_as_of"]
