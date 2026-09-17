"""The primary selection label ``sel_ar_h`` (ML label registry, memo §5.1, §8 step 1).

``docs/research/ml_label_registry_design_2026_09_16.md`` is the contract; this module
only computes and stores what it defines.

- Anchor: the official OPEN of ``ladder_arrival_session(brief_date)``, the first session
  a reader of the brief can trade (D4). A date whose list was final only after that
  open is excluded (``set_final_after_open``), never shifted.
- Benchmark: IWM times the raw OLS beta over the 250 daily returns ending at the close
  before the anchor, intercept not carried (D2). Stock and benchmark compound as two
  wealth paths from the open; ``sel_ar_h`` is their difference after ``h`` sessions,
  the anchor session counted as the first.
- Population: thematic briefed names plus every LLM proposal of ``proposal_shadow``,
  one row per (brief date, ticker), with stage columns beside the label (D3). The
  event lane keeps its registered outcome (``event_car.py``) and is not labelled here.
- Horizons 1 / 3 / 5 / 10 / 20 / 40, the non-overlapping increments 1-10 / 11-20 /
  21-40 and the pre-specified secondary ``sel_car_mean_20`` (D1). ``sel_zar_h`` is the
  label over the pre-window residual volatility: stored, not an owner decision.
- Missing is a coded outcome, one status per horizon, never a dropped row.

Prices come from the split-adjusted grouped-daily history (``rs_history``), where the
memo's measurements were taken. That store never re-fetches a session already on disk,
so a label stamped under one version stays consistent with it: rows whose every
horizon is terminal are never recomputed. Labels live in their own store, are not on
the wire and are not read by ``/edge``. Nothing here logs a label value.
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
import math
import os
import statistics
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from alphalens_pipeline.data.rs_history import DEFAULT_RS_HISTORY_ROOT
from alphalens_pipeline.events.insider_cluster import (
    SOURCE_INSIDER_CLUSTER,
    SPLIT_RATIO_HI,
    SPLIT_RATIO_LO,
)
from alphalens_pipeline.feedback.ladder_config import ladder_arrival_session
from alphalens_pipeline.feedback.market_beta import BetaEstimate, estimate_beta
from alphalens_pipeline.paper.calendar import (
    DEFAULT_EXCHANGE,
    advance_trading_sessions,
    n_sessions_before,
    previous_trading_day,
)
from alphalens_pipeline.thematic.mapping.proposal_shadow import DEFAULT_SHADOW_DIR
from alphalens_pipeline.thematic.publication import BRIEF_PUBLISHED_AT, published_before_open

logger = logging.getLogger(__name__)

# Poolability key. Bump on ANY change to: the anchor rule, the horizons, the benchmark
# ticker, BETA_WINDOW_SESSIONS, MIN_BETA_OBS, the split bounds, RESID_WINDOW_SESSIONS,
# MIN_RESID_OBS, the population rule or the status codes and their order.
SEL_LABEL_VERSION = "sel-label-v1"

HORIZONS: tuple[int, ...] = (1, 3, 5, 10, 20, 40)
MAX_HORIZON = max(HORIZONS)
INCREMENTS: tuple[tuple[int, int], ...] = ((1, 10), (11, 20), (21, 40))
PATH_MEAN_HORIZON = 20
BENCHMARK_TICKER = "IWM"
BETA_WINDOW_SESSIONS = 250  # daily returns ending at the close before the anchor
MIN_BETA_OBS = 120  # fewer usable return pairs -> beta = 1 (recent listings; a stated bias)
RESID_WINDOW_SESSIONS = 60  # daily residuals behind sel_zar_h
MIN_RESID_OBS = 30  # fewer residuals -> no scaled label

DEFAULT_LABELS_DIR = Path.home() / ".alphalens" / "selection_labels"
DEFAULT_BRIEFS_DIR = Path.home() / ".alphalens" / "thematic_briefs"

STATUS_OK = "ok"
STATUS_SET_FINAL_AFTER_OPEN = "set_final_after_open"
STATUS_PUBLICATION_UNKNOWN = "publication_unknown"
STATUS_IMMATURE = "immature"
STATUS_GROUPED_SESSION_MISSING = "grouped_session_missing"
STATUS_NO_OPEN = "no_open"
STATUS_BENCHMARK_MISSING = "benchmark_missing"
STATUS_NO_CLOSE_AT_HORIZON = "no_close_at_horizon"
STATUS_SPLIT_GUARD = "split_guard"
# A row with any of these is recomputed on the next pass; every other status is final.
NON_TERMINAL_STATUSES = frozenset(
    {STATUS_PUBLICATION_UNKNOWN, STATUS_IMMATURE, STATUS_GROUPED_SESSION_MISSING}
)

POPULATION_BRIEFED_OR_PROPOSED = "briefed_or_llm_proposed"
LANE_THEMATIC = "thematic"
SHADOW_SOURCE_LLM = "llm"

# (open, close) per ticker for one session; ``None`` for the session = no file on disk.
SessionBars = Mapping[str, tuple[float | None, float | None]]
PriceBook = Mapping[dt.date, SessionBars | None]


def ar_key(h: int) -> str:
    return f"sel_ar_{h}"


def zar_key(h: int) -> str:
    return f"sel_zar_{h}"


def status_key(h: int) -> str:
    return f"sel_label_status_{h}"


def increment_key(lo: int, hi: int) -> str:
    return f"sel_ar_inc_{lo}_{hi}"


PATH_MEAN_KEY = f"sel_car_mean_{PATH_MEAN_HORIZON}"
VALUE_KEYS: tuple[str, ...] = (
    *(ar_key(h) for h in HORIZONS),
    *(zar_key(h) for h in HORIZONS),
    *(increment_key(lo, hi) for lo, hi in INCREMENTS),
    PATH_MEAN_KEY,
)
STAGE_COLUMNS: tuple[str, ...] = (
    "lane",
    "population",
    "briefed_any_theme",
    "themes_briefed",
    "themes_proposed",
    "shadow_available",
    "event_overlap",
    "mapper_config_version",
    BRIEF_PUBLISHED_AT,
)
PRE_COLUMNS: tuple[str, ...] = (
    "beta_ols",
    "beta_source",
    "beta_n_obs",
    "beta_n_zero",
    "beta_n_split_dropped",
    "sigma_resid_pre",
)


_STRING = pa.string()
_FLOAT = pa.float64()
_INT = pa.int64()
_BOOL = pa.bool_()
# One fixed schema for every label file. Without it a date whose rows carry no value
# (all excluded) writes null-typed columns, and the store no longer reads as one dataset.
BRIEF_PUBLISHED_AT_TYPE = pa.timestamp("us", tz="UTC")
SEL_LABEL_SCHEMA = pa.schema(
    [
        ("brief_date", pa.date32()),
        ("ticker", _STRING),
        ("lane", _STRING),
        ("population", _STRING),
        ("briefed_any_theme", _BOOL),
        ("themes_briefed", pa.list_(_STRING)),
        ("themes_proposed", pa.list_(_STRING)),
        ("shadow_available", _BOOL),
        ("event_overlap", _BOOL),
        ("mapper_config_version", _STRING),
        (BRIEF_PUBLISHED_AT, BRIEF_PUBLISHED_AT_TYPE),
        ("anchor_session", pa.date32()),
        ("published_before_open", _BOOL),
        ("beta_ols", _FLOAT),
        ("beta_source", _STRING),
        ("beta_n_obs", _INT),
        ("beta_n_zero", _INT),
        ("beta_n_split_dropped", _INT),
        ("sigma_resid_pre", _FLOAT),
        *((key, _FLOAT) for key in VALUE_KEYS),
        *((status_key(h), _STRING) for h in HORIZONS),
        ("sel_label_version", _STRING),
        ("computed_at", _STRING),
    ]
)
SEL_LABEL_COLUMNS: tuple[str, ...] = tuple(SEL_LABEL_SCHEMA.names)


@dataclass(frozen=True)
class PreWindow:
    """Everything the label takes from before the anchor; fixed once estimated."""

    beta: BetaEstimate
    n_split_dropped: int
    sigma_resid_pre: float | None


@dataclass(frozen=True)
class SelectionLabel:
    anchor_session: dt.date
    pre: PreWindow | None
    values: dict[str, float | None]
    statuses: dict[str, str]


def _positive(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0.0:
        return None
    return out


def _bar(book: PriceBook, session: dt.date, ticker: str) -> tuple[float | None, float | None]:
    bars = book.get(session)
    if not bars:
        return None, None
    o, c = bars.get(ticker, (None, None))
    return _positive(o), _positive(c)


def _out_of_bounds(prev: float, cur: float) -> bool:
    ratio = cur / prev
    return ratio < SPLIT_RATIO_LO or ratio > SPLIT_RATIO_HI


def pre_window_sessions(anchor: dt.date, exchange: str = DEFAULT_EXCHANGE) -> list[dt.date]:
    """The ``BETA_WINDOW_SESSIONS + 1`` closes ending at the session before ``anchor``."""
    return [n_sessions_before(anchor, k, exchange) for k in range(BETA_WINDOW_SESSIONS + 1, 0, -1)]


def window_sessions(anchor: dt.date, n: int, exchange: str = DEFAULT_EXCHANGE) -> list[dt.date]:
    return [advance_trading_sessions(anchor, k, exchange) for k in range(n)]


def estimate_pre_window(
    book: PriceBook, ticker: str, anchor: dt.date, exchange: str = DEFAULT_EXCHANGE
) -> PreWindow:
    """Raw OLS beta vs IWM and the residual sd, from closes strictly before ``anchor``.

    The store is split-adjusted as of each session's fetch, so a split after a fetch
    leaves one unadjusted step. A close whose ratio to the previous close is outside
    the split bounds is removed (both returns touching it drop) and counted.
    """
    sessions = pre_window_sessions(anchor, exchange)
    stock: list[float | None] = [_bar(book, s, ticker)[1] for s in sessions]
    market: list[float | None] = [_bar(book, s, BENCHMARK_TICKER)[1] for s in sessions]
    cleaned = list(stock)
    n_dropped = 0
    for i in range(1, len(stock)):
        prev, cur = stock[i - 1], stock[i]
        if prev is not None and cur is not None and _out_of_bounds(prev, cur):
            cleaned[i] = None
            n_dropped += 1
    beta = estimate_beta(cleaned, market, min_observations=MIN_BETA_OBS)
    return PreWindow(beta, n_dropped, _residual_sd(cleaned, market, beta.beta))


def _residual_sd(
    stock: Sequence[float | None], market: Sequence[float | None], beta: float
) -> float | None:
    residuals: list[float] = []
    start = len(stock) - RESID_WINDOW_SESSIONS
    for i in range(max(start, 1), len(stock)):
        s0, s1, m0, m1 = stock[i - 1], stock[i], market[i - 1], market[i]
        if s0 is None or s1 is None or m0 is None or m1 is None:
            continue
        residuals.append((s1 / s0 - 1.0) - beta * (m1 / m0 - 1.0))
    if len(residuals) < MIN_RESID_OBS:
        return None
    sd = statistics.stdev(residuals)
    return sd if sd > 0.0 else None


def _uniform(status: str, anchor: dt.date, pre: PreWindow | None) -> SelectionLabel:
    return SelectionLabel(anchor, pre, dict.fromkeys(VALUE_KEYS), dict.fromkeys(VALUE_KEYS, status))


def _horizon_status(
    h: int,
    *,
    sessions: Sequence[dt.date],
    file_present: Sequence[bool],
    stock_open: float | None,
    stock_close: Sequence[float | None],
    iwm_open: float | None,
    iwm_close: Sequence[float | None],
    last_closed_session: dt.date,
    newest_session: dt.date | None,
) -> str:
    end = sessions[h - 1]
    if end > last_closed_session or newest_session is None or end > newest_session:
        return STATUS_IMMATURE
    if not all(file_present[:h]):
        return STATUS_GROUPED_SESSION_MISSING
    if stock_open is None:
        return STATUS_NO_OPEN
    if iwm_open is None or any(c is None for c in iwm_close[:h]):
        return STATUS_BENCHMARK_MISSING
    closes = stock_close[:h]
    if any(c is None for c in closes):
        return STATUS_NO_CLOSE_AT_HORIZON
    path: list[float] = [stock_open, *(c for c in closes if c is not None)]
    if any(_out_of_bounds(a, b) for a, b in itertools.pairwise(path)):
        return STATUS_SPLIT_GUARD
    return STATUS_OK


def compute_selection_label(
    book: PriceBook,
    ticker: str,
    *,
    brief_date: dt.date,
    published_before_open: bool | None,
    last_closed_session: dt.date,
    newest_session: dt.date | None,
    pre: PreWindow | None = None,
    exchange: str = DEFAULT_EXCHANGE,
) -> SelectionLabel:
    """The label of one (brief date, ticker). ``pre`` reuses an already estimated beta.

    Status order, first match wins: publication after the open, publication unknown,
    immature, a session file missing, no open, benchmark missing, no close, split guard.
    """
    anchor = ladder_arrival_session(brief_date, exchange)
    if published_before_open is False:
        return _uniform(STATUS_SET_FINAL_AFTER_OPEN, anchor, None)
    if published_before_open is None:
        return _uniform(STATUS_PUBLICATION_UNKNOWN, anchor, None)
    ticker = ticker.upper()
    if pre is None:
        pre = estimate_pre_window(book, ticker, anchor, exchange)

    sessions = window_sessions(anchor, MAX_HORIZON, exchange)
    file_present = [book.get(s) is not None for s in sessions]
    stock_open, _ = _bar(book, anchor, ticker)
    iwm_open, _ = _bar(book, anchor, BENCHMARK_TICKER)
    stock_close = [_bar(book, s, ticker)[1] for s in sessions]
    iwm_close = [_bar(book, s, BENCHMARK_TICKER)[1] for s in sessions]

    statuses: dict[str, str] = {}
    values: dict[str, float | None] = dict.fromkeys(VALUE_KEYS)
    path = _abnormal_path(stock_open, stock_close, iwm_open, iwm_close, pre.beta.beta)
    for h in HORIZONS:
        status = _horizon_status(
            h,
            sessions=sessions,
            file_present=file_present,
            stock_open=stock_open,
            stock_close=stock_close,
            iwm_open=iwm_open,
            iwm_close=iwm_close,
            last_closed_session=last_closed_session,
            newest_session=newest_session,
        )
        statuses[ar_key(h)] = statuses[zar_key(h)] = status
        if status == STATUS_OK:
            values[ar_key(h)] = path[h - 1]
            sigma = pre.sigma_resid_pre
            if sigma is not None:
                values[zar_key(h)] = path[h - 1] / (sigma * math.sqrt(h))

    for lo, hi in INCREMENTS:
        key = increment_key(lo, hi)
        statuses[key] = statuses[ar_key(hi)]
        if statuses[key] == STATUS_OK:
            before = path[lo - 2] if lo > 1 else 0.0
            values[key] = path[hi - 1] - before
    statuses[PATH_MEAN_KEY] = statuses[ar_key(PATH_MEAN_HORIZON)]
    if statuses[PATH_MEAN_KEY] == STATUS_OK:
        values[PATH_MEAN_KEY] = sum(path[:PATH_MEAN_HORIZON]) / PATH_MEAN_HORIZON
    return SelectionLabel(anchor, pre, values, statuses)


def _abnormal_path(
    stock_open: float | None,
    stock_close: Sequence[float | None],
    iwm_open: float | None,
    iwm_close: Sequence[float | None],
    beta: float,
) -> list[float]:
    """``W_stock - W_bench`` after each session, as far as every price is present."""
    path: list[float] = []
    if stock_open is None or iwm_open is None:
        return path
    w_bench, prev_iwm = 1.0, iwm_open
    for sc, ic in zip(stock_close, iwm_close, strict=True):
        if sc is None or ic is None:
            break
        w_bench *= 1.0 + beta * (ic / prev_iwm - 1.0)
        prev_iwm = ic
        path.append(sc / stock_open - w_bench)
    return path


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------


def _column(df: pd.DataFrame, name: str, default: Any) -> pd.Series:
    return df[name] if name in df.columns else pd.Series([default] * len(df), index=df.index)


def _first_present(values: Iterable[Any]) -> Any:
    for v in values:
        if v is not None and not (isinstance(v, float) and math.isnan(v)) and v is not pd.NaT:
            return v
    return None


def build_population(brief: pd.DataFrame | None, shadow: pd.DataFrame | None) -> pd.DataFrame:
    """One row per ticker: thematic briefed names and LLM proposals, with stage columns."""
    themes_briefed: dict[str, set[str]] = {}
    themes_proposed: dict[str, set[str]] = {}
    overlap: dict[str, bool] = {}
    config: dict[str, Any] = {}
    published_at = None
    if brief is not None and len(brief):
        b = brief[
            _column(brief, "source", LANE_THEMATIC).fillna(LANE_THEMATIC) != SOURCE_INSIDER_CLUSTER
        ]
        published_at = _first_present(_column(brief, BRIEF_PUBLISHED_AT, None))
        for rec in b.to_dict("records"):
            ticker = str(rec.get("ticker") or "").upper()
            if not ticker:
                continue
            themes_briefed.setdefault(ticker, set()).add(str(rec.get("theme") or ""))
            overlap[ticker] = overlap.get(ticker, False) or rec.get("event_overlap") is True
            config.setdefault(ticker, rec.get("mapper_config_version"))
    shadow_available = shadow is not None
    if shadow is not None and len(shadow):
        s = shadow[_column(shadow, "source", None) == SHADOW_SOURCE_LLM]
        for rec in s.to_dict("records"):
            ticker = str(rec.get("ticker") or "").upper()
            if not ticker:
                continue
            themes_proposed.setdefault(ticker, set()).add(str(rec.get("theme") or ""))
            if config.get(ticker) is None:
                config[ticker] = rec.get("mapper_config_version")
    rows = [
        {
            "ticker": ticker,
            "lane": LANE_THEMATIC,
            "population": POPULATION_BRIEFED_OR_PROPOSED,
            "briefed_any_theme": ticker in themes_briefed,
            "themes_briefed": sorted(themes_briefed.get(ticker, ())),
            "themes_proposed": sorted(themes_proposed.get(ticker, ())),
            "shadow_available": shadow_available,
            "event_overlap": overlap.get(ticker, False),
            "mapper_config_version": config.get(ticker),
            BRIEF_PUBLISHED_AT: published_at,
        }
        for ticker in sorted(set(themes_briefed) | set(themes_proposed))
    ]
    return pd.DataFrame(rows, columns=["ticker", *STAGE_COLUMNS])


# ---------------------------------------------------------------------------
# Store pass
# ---------------------------------------------------------------------------


@dataclass
class SelectionLabelReport:
    dates_written: int = 0
    dates_failed: int = 0
    rows_stamped: int = 0
    status_counts_h20: dict[str, int] = field(default_factory=dict)


class _SessionReader:
    """Reads only the tickers a pass needs from each session file, once per run."""

    def __init__(self, root: Path):
        self._root = root
        self._bars: dict[dt.date, dict[str, tuple[float | None, float | None]] | None] = {}
        self._read: dict[dt.date, set[str]] = {}

    def book(
        self, sessions: Iterable[dt.date], tickers: set[str]
    ) -> dict[dt.date, SessionBars | None]:
        return {s: self._session(s, tickers) for s in sessions}

    def _session(self, session: dt.date, tickers: set[str]) -> SessionBars | None:
        if session in self._bars and self._bars[session] is None:
            return None
        missing = tickers - self._read.get(session, set())
        if missing:
            path = self._root / f"{session.isoformat()}.parquet"
            if not path.exists():
                self._bars[session] = None
                return None
            try:
                df = pd.read_parquet(
                    path, columns=["T", "o", "c"], filters=[("T", "in", sorted(missing))]
                )
            except (OSError, ValueError) as exc:
                logger.warning("selection-label: unreadable session file %s (%s)", path, exc)
                self._bars[session] = None
                return None
            bars = self._bars.setdefault(session, {})
            assert bars is not None
            for t, o, c in zip(df["T"], df["o"], df["c"], strict=True):
                bars[str(t).upper()] = (o, c)
            self._read.setdefault(session, set()).update(missing)
        return self._bars.get(session) or {}


def _date_stem(path: Path) -> dt.date | None:
    try:
        return dt.date.fromisoformat(path.stem)
    except ValueError:
        return None


def _dated_files(directory: Path) -> dict[dt.date, Path]:
    if not directory.exists():
        return {}
    out: dict[dt.date, Path] = {}
    for path in directory.glob("*.parquet"):
        d = _date_stem(path)
        if d is not None and path.is_file():
            out[d] = path
    return out


def _normalise(value: Any) -> Any:
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        return value.tolist()
    if value is pd.NaT or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def _is_non_terminal(row: Mapping[str, Any]) -> bool:
    if row.get("sel_label_version") != SEL_LABEL_VERSION:
        return True
    return any(row.get(status_key(h)) in NON_TERMINAL_STATUSES for h in HORIZONS) or any(
        row.get(status_key(h)) is None for h in HORIZONS
    )


def _reusable_pre(row: Mapping[str, Any] | None) -> PreWindow | None:
    if row is None or row.get("sel_label_version") != SEL_LABEL_VERSION:
        return None
    if row.get("beta_source") is None or row.get("beta_ols") is None:
        return None
    return PreWindow(
        BetaEstimate(
            float(row["beta_ols"]),
            str(row["beta_source"]),
            int(row["beta_n_obs"]),
            int(row["beta_n_zero"]),
        ),
        int(row["beta_n_split_dropped"]),
        None if row.get("sigma_resid_pre") is None else float(row["sigma_resid_pre"]),
    )


def _label_record(
    result: SelectionLabel, published: bool | None, now: dt.datetime
) -> dict[str, Any]:
    pre = result.pre
    rec: dict[str, Any] = {
        "anchor_session": result.anchor_session,
        "published_before_open": published,
        "beta_ols": pre.beta.beta if pre else None,
        "beta_source": pre.beta.source if pre else None,
        "beta_n_obs": pre.beta.n_observations if pre else None,
        "beta_n_zero": pre.beta.n_zero_returns if pre else None,
        "beta_n_split_dropped": pre.n_split_dropped if pre else None,
        "sigma_resid_pre": pre.sigma_resid_pre if pre else None,
        **result.values,
        **{status_key(h): result.statuses[ar_key(h)] for h in HORIZONS},
        "sel_label_version": SEL_LABEL_VERSION,
        "computed_at": now.isoformat(),
    }
    return rec


def _coerce(value: Any, kind: pa.DataType) -> Any:
    """A stored Python value in the type its schema field expects (parquet reads ints with
    nulls back as floats and timestamps as ``pd.Timestamp``)."""
    value = _normalise(value)
    if value is None:
        return None
    if kind == _INT:
        return int(value)
    if kind == _FLOAT:
        return float(value)
    if kind == _BOOL:
        return bool(value)
    if kind == BRIEF_PUBLISHED_AT_TYPE:
        stamp = pd.Timestamp(value)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        return stamp.to_pydatetime()
    return value


def _write_labels_atomic(rows: Sequence[Mapping[str, Any]], path: Path) -> None:
    """Write ``rows`` under ``SEL_LABEL_SCHEMA`` via a temp file in the same directory."""
    table = pa.Table.from_pylist(
        [{f.name: _coerce(row.get(f.name), f.type) for f in SEL_LABEL_SCHEMA} for row in rows],
        schema=SEL_LABEL_SCHEMA,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        pq.write_table(table, tmp)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _stamp_date(
    brief_date: dt.date,
    *,
    brief_path: Path | None,
    shadow_path: Path | None,
    labels_dir: Path,
    reader: _SessionReader,
    now: dt.datetime,
    last_closed_session: dt.date,
    newest_session: dt.date | None,
    counts: Counter[str],
    exchange: str,
) -> tuple[bool, int]:
    brief = pd.read_parquet(brief_path) if brief_path is not None else None
    shadow = pd.read_parquet(shadow_path) if shadow_path is not None else None
    population = build_population(brief, shadow)
    out_path = labels_dir / f"{brief_date.isoformat()}.parquet"
    existing: dict[str, dict[str, Any]] = {}
    if out_path.exists():
        try:
            for rec in pd.read_parquet(out_path).to_dict("records"):
                existing[str(rec["ticker"])] = {str(k): _normalise(v) for k, v in rec.items()}
        except (OSError, ValueError) as exc:
            logger.warning(
                "selection-label: unreadable label file %s (%s); rebuilding", out_path, exc
            )
    if population.empty and not existing:
        return False, 0

    published_at = _first_present(population[BRIEF_PUBLISHED_AT]) if len(population) else None
    published = published_before_open(brief_date, published_at, exchange)
    pop_records = {
        r["ticker"]: {k: _normalise(v) for k, v in r.items()} for r in population.to_dict("records")
    }
    todo = [t for t in pop_records if t not in existing or _is_non_terminal(existing[t])]

    rows = dict(existing)
    changed = False
    n_stamped = 0
    for ticker, stage in pop_records.items():
        base = rows.get(ticker, {"brief_date": brief_date, "ticker": ticker})
        merged = {**base, **stage}
        if merged != base:
            changed = True
        rows[ticker] = merged

    if todo:
        book = _book_for(todo, rows, brief_date, reader, published, exchange)
        for ticker in todo:
            result = compute_selection_label(
                book,
                ticker,
                brief_date=brief_date,
                published_before_open=published,
                last_closed_session=last_closed_session,
                newest_session=newest_session,
                pre=_reusable_pre(existing.get(ticker)),
                exchange=exchange,
            )
            record = _label_record(result, published, now)
            old = existing.get(ticker)
            if old is not None and _same_label(old, record):
                continue  # still immature / unknown with nothing new: keep the stored row
            rows[ticker] = {**rows[ticker], **record}
            counts[result.statuses[ar_key(PATH_MEAN_HORIZON)]] += 1
            n_stamped += 1
            changed = True

    if not changed:
        return False, 0
    _write_labels_atomic([rows[t] for t in sorted(rows)], out_path)
    return True, n_stamped


def _same_label(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    """True when a recomputation reproduced the stored row (``computed_at`` aside)."""
    return all(
        _normalise(old.get(k)) == _normalise(v) for k, v in new.items() if k != "computed_at"
    )


def _book_for(
    todo: Sequence[str],
    rows: Mapping[str, Mapping[str, Any]],
    brief_date: dt.date,
    reader: _SessionReader,
    published: bool | None,
    exchange: str,
) -> PriceBook:
    if published is not True:
        return {}
    anchor = ladder_arrival_session(brief_date, exchange)
    tickers = {t.upper() for t in todo} | {BENCHMARK_TICKER}
    sessions = window_sessions(anchor, MAX_HORIZON, exchange)
    if any(_reusable_pre(rows.get(t)) is None for t in todo):
        sessions = pre_window_sessions(anchor, exchange) + sessions
    return reader.book(sessions, tickers)


def enrich_selection_labels(
    *,
    briefs_dir: Path = DEFAULT_BRIEFS_DIR,
    shadow_dir: Path = DEFAULT_SHADOW_DIR,
    labels_dir: Path = DEFAULT_LABELS_DIR,
    grouped_root: Path = DEFAULT_RS_HISTORY_ROOT,
    now: dt.datetime | None = None,
    exchange: str = DEFAULT_EXCHANGE,
    deadline: Any = None,
) -> SelectionLabelReport:
    """Stamp every brief date newest-first. Never raises for one bad date.

    A deadline trip stops before the next date, so a label file is never half-written.
    The report carries counts only.
    """
    now = now or dt.datetime.now(dt.UTC)
    last_closed_session = previous_trading_day(now.date(), exchange)
    briefs, shadows = _dated_files(Path(briefs_dir)), _dated_files(Path(shadow_dir))
    grouped = _dated_files(Path(grouped_root))
    newest_session = max(grouped) if grouped else None
    reader = _SessionReader(Path(grouped_root))
    report = SelectionLabelReport()
    counts: Counter[str] = Counter()
    for brief_date in sorted(set(briefs) | set(shadows), reverse=True):
        if deadline is not None and deadline.should_stop():
            break
        try:
            written, n = _stamp_date(
                brief_date,
                brief_path=briefs.get(brief_date),
                shadow_path=shadows.get(brief_date),
                labels_dir=Path(labels_dir),
                reader=reader,
                now=now,
                last_closed_session=last_closed_session,
                newest_session=newest_session,
                counts=counts,
                exchange=exchange,
            )
        except Exception:  # one bad date must not stop the pass
            logger.exception("selection-label: failed on %s; continuing", brief_date)
            report.dates_failed += 1
            continue
        report.dates_written += int(written)
        report.rows_stamped += n
    report.status_counts_h20 = dict(counts)
    return report


__all__ = [
    "HORIZONS",
    "NON_TERMINAL_STATUSES",
    "SEL_LABEL_COLUMNS",
    "SEL_LABEL_VERSION",
    "PreWindow",
    "SelectionLabel",
    "SelectionLabelReport",
    "build_population",
    "compute_selection_label",
    "enrich_selection_labels",
    "estimate_pre_window",
]
