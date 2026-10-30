"""Index-level market-state regime classifier (PR-1) — display-only, log-now.

Composes the PR-0 primitives into a discrete 4-state trend×volatility label
(+ ``unknown``) for a single index (SPY), per the design memo
(docs/research/market_state_signal_design_2026_07_05.md §1.3). The label is a
HEURISTIC, not an estimated regime; every threshold is FROZEN a-priori and
UNVALIDATED (usable history is <1y, so there is no honest in-sample fit — memo
§2). Nothing here feeds selection or ordering: it is a context label, held out
of the brief sort exactly like the expert panel (the PR-6 sort allowlist).

Design (memo §1.3):
- Trend axis {up, down, neutral}: price vs SMA200, SMA50 vs SMA200, SMA50 slope,
  with a ``DIST_FLAT_BAND`` deadband → neutral.
- Vol axis {low, high}: OR of a realized proxy (ATR% ≥ its own trailing quantile)
  and an implied proxy (VIX ≥ 25). The OR is a single pre-committed a-priori
  choice (crypto-origin), logged in the config version.
- neutral trend folds to up/down by ``sign(dist200)``; the (trend, vol) grid maps
  to the four named states. A missing/insufficient input → ``unknown`` (a
  first-class token, never silently mapped to a real state) — with ONE exception,
  below.

An OR does not always need both operands (#1559). With the realized leg at or
above ``ATR_HIGH_Q`` the vol axis is 'high' whatever the VIX is, so the label is
fully determined and blanking it would discard a decided answer. Only the other
branch — realized leg low, VIX absent — is genuinely undetermined, because a VIX
at or above ``VIX_HIGH`` would have flipped it. So ``vix`` is the one decision
input that is not unconditionally required.

Two partitions a forward study will want, both computable from columns already
stamped — no extra column carries them:

* **did the implied leg take part** — among rows the classifier DECIDED,
  ``market_state_vix`` is NaN exactly when it did not, whatever the reason
  (empty series, non-finite print, a refused fetch). Restrict to
  ``market_state != 'unknown'`` first: :func:`_unknown_result` NaNs the same
  column when the whole classification failed, so an unfiltered read mixes
  "decided without the implied leg" with "decided nothing at all";
* **did the implied leg DECIDE anything** — a present VIX is not a VIX that
  mattered. Where ``market_state_atr_pct_q >= ATR_HIGH_Q`` the realized leg
  settled the axis alone and the VIX is decoration.

``MARKET_STATE_CONFIG_VERSION`` is the sole poolability key for the label + all
telemetry; the deferred forward study partitions rows by it, never pools across
versions. The two-axis raw drivers are stamped alongside the bucket so the study
correlates the CONTINUOUS drivers, never only the label (the disagreement.py
discipline). This module is the pure classifier; the store/FRED I/O wrapper and
the broadcast ``enrich`` stamp are added in a later step of this PR.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from pathlib import Path
from typing import Any

import pandas as pd

from alphalens_pipeline.brief_contract.validation import warn_on_column_disagreement
from alphalens_pipeline.data.rs_history import (
    DEFAULT_RS_HISTORY_ROOT,
    _close,
    _newest_session_on_or_before,
    read_grouped_day,
)
from alphalens_pipeline.market.calendar import DEFAULT_EXCHANGE, n_sessions_before
from alphalens_pipeline.market.primitives import (
    atr_pct,
    bollinger_keltner_squeeze,
    normalized_slope,
    rolling_quantile_rank,
    sma,
)

logger = logging.getLogger(__name__)

# --- Frozen hyperparameters (memo §2.2 manifest). All UNVALIDATED literal priors;
# --- any change ⇒ bump MARKET_STATE_CONFIG_VERSION so old rows are not pooled.
SMA_FAST = 50
SMA_SLOW = 200
SLOPE_WIN = 20
SLOPE_EPS = 0.0
DIST_FLAT_BAND = 0.02
ATR_WIN = 14
ATR_QUANTILE_LOOKBACK = 252
ATR_HIGH_Q = 0.70
VIX_HIGH = 25.0
BB_WIN = 20
BB_K = 2.0
KC_WIN = 20
KC_MULT = 1.5

# Bumped v1 -> v1.1 on 2026-09-24 (#1524). No threshold changed; the VIX INPUT
# did. Every row stamped between 2026-07-06 and that date carries a VIX frozen at
# the 2026-07-01 print (16.59) because the FRED disk cache had no expiry, so
# `market_state_vix` and `market_state_vix_decile` are not comparable across the
# boundary. The rows are NOT pooled with v1.1 rows; see the dated note in
# docs/research/market_state_signal_design_2026_07_05.md.
#
# Bumped v1.1 -> v1.2 on 2026-09-25 (#1559). No threshold changed and no label
# changed: for every row where both legs were finite the mapping is identical,
# and all 8 rows ever stamped v1.1 had a present VIX. What changed is WHEN a
# label is emitted — v1.2 decides a high-realized-vol day that v1.1 blanked. A
# version string has to name exactly one emission rule, or it identifies nothing;
# that, not a threshold edit, is what forces the bump. Kept in step with
# docs/research/preregistration/ledger.json (pinned by a test — the v1.1 bump
# never reached the ledger because nothing checked it).
MARKET_STATE_CONFIG_VERSION = "mstate-v1.2-spy-sma50x200-atrq70-vix15_25-UNVALIDATED"

# The columns this signal stamps onto every (broadcast) row. ``market_state`` is
# the label; the rest are the raw continuous drivers + the poolability key.
MARKET_STATE_COLUMNS: tuple[str, ...] = (
    "market_state",
    "market_state_atr_pct",
    "market_state_atr_pct_q",
    "market_state_dist200",
    "market_state_vix",
    "market_state_vix_decile",
    "market_state_squeeze_on",
    "market_state_config_version",
)

_UNKNOWN = "unknown"

# (trend, vol) → named state (after the neutral fold).
_STATE_MAP = {
    ("up", "low"): "bull_quiet",
    ("up", "high"): "bull_volatile",
    ("down", "high"): "bear_volatile",
    ("down", "low"): "bear_quiet",
}


def _trend_axis(c: float, sma50: float, sma200: float, slope: float, dist200: float) -> str:
    """Trend axis {up, down, neutral} (memo §1.3). The ``DIST_FLAT_BAND`` deadband
    around SMA200 takes precedence → neutral; otherwise price + SMA50 must be on the
    same side of SMA200 with a same-sign SMA50 slope."""
    if abs(dist200) <= DIST_FLAT_BAND:
        return "neutral"
    if c > sma200 and sma50 > sma200 and slope > SLOPE_EPS:
        return "up"
    if c < sma200 and sma50 < sma200 and slope < -SLOPE_EPS:
        return "down"
    return "neutral"


def _vol_axis(atr_pct_q: float, vix: float) -> str | None:
    """Vol axis {low, high}: OR of a realized (ATR% quantile) and an implied (VIX)
    proxy — either elevation flips the state to 'high' (memo §1.3).

    Returns ``None`` for the one case the OR cannot settle: the realized leg is
    low and the VIX is unavailable, where a VIX at or above ``VIX_HIGH`` would
    have flipped the answer (#1559). A high realized leg settles it alone, so an
    absent VIX costs nothing there.

    The thresholds are frozen a-priori and are NOT touched by that split.
    """
    if atr_pct_q >= ATR_HIGH_Q:
        return "high"
    if math.isfinite(vix):
        return "high" if vix >= VIX_HIGH else "low"
    return None


def _unknown_result() -> dict[str, Any]:
    """The fully-unknown classification — used when inputs are missing or I/O fails."""
    return {
        "market_state": _UNKNOWN,
        "market_state_atr_pct": float("nan"),
        "market_state_atr_pct_q": float("nan"),
        "market_state_dist200": float("nan"),
        "market_state_vix": float("nan"),
        "market_state_vix_decile": float("nan"),
        "market_state_squeeze_on": None,
    }


def _last(series: pd.Series) -> float:
    """Last value as a float, or NaN when the series is empty."""
    if len(series) == 0:
        return float("nan")
    return float(series.iloc[-1])


def classify_state(
    *,
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    vix: pd.Series,
) -> dict[str, Any]:
    """Classify the market state at the LAST bar of the trailing input series.

    ``close``/``high``/``low`` are the trailing index (SPY) daily bars ending at
    the as-of date; ``vix`` is the trailing VIX series ending at the same date.
    The two series' last values are read INDEPENDENTLY (``.iloc[-1]`` on each),
    so the SPY session and the VIX print may fall on slightly different calendar
    dates — acceptable for a daily regime label; the caller (:func:`classify`)
    is responsible for the ``<= asof`` PIT constraint on both.
    Returns the label under ``market_state`` plus the raw driver telemetry, with
    NaN telemetry where a driver could not be computed. Every input except
    ``vix`` must be finite or the label is ``'unknown'``; an absent VIX blanks it
    only when the realized leg cannot settle the vol axis alone (see
    :func:`_vol_axis` and the module docstring). Pure — no I/O.
    """
    telemetry: dict[str, Any] = {
        "market_state_atr_pct": float("nan"),
        "market_state_atr_pct_q": float("nan"),
        "market_state_dist200": float("nan"),
        "market_state_vix": float("nan"),
        "market_state_vix_decile": float("nan"),
        "market_state_squeeze_on": None,
    }

    have_bars = len(close) > 0
    if have_bars:
        sma_fast = sma(close, SMA_FAST)
        sma_slow = sma(close, SMA_SLOW)
        slope_series = normalized_slope(sma_fast, window=SLOPE_WIN)
        atr_pct_series = atr_pct(high, low, close, window=ATR_WIN)
        atr_pct_q_series = rolling_quantile_rank(atr_pct_series, lookback=ATR_QUANTILE_LOOKBACK)
        squeeze_series = bollinger_keltner_squeeze(
            close, high, low, bb_window=BB_WIN, bb_k=BB_K, kc_window=KC_WIN, kc_mult=KC_MULT
        )

        c = _last(close)
        sma50_now = _last(sma_fast)
        sma200_now = _last(sma_slow)
        slope_now = _last(slope_series)
        atr_pct_now = _last(atr_pct_series)
        atr_pct_q_now = _last(atr_pct_q_series)
        dist200 = (
            (c - sma200_now) / sma200_now
            if math.isfinite(sma200_now) and sma200_now > 0.0
            else float("nan")
        )

        telemetry["market_state_atr_pct"] = atr_pct_now
        telemetry["market_state_atr_pct_q"] = atr_pct_q_now
        telemetry["market_state_dist200"] = dist200
        telemetry["market_state_squeeze_on"] = bool(squeeze_series.iloc[-1])
    else:
        c = sma50_now = sma200_now = slope_now = atr_pct_q_now = dist200 = float("nan")

    vix_now = _last(vix)
    if len(vix) > 0:
        telemetry["market_state_vix"] = vix_now
        telemetry["market_state_vix_decile"] = _last(
            rolling_quantile_rank(vix, lookback=ATR_QUANTILE_LOOKBACK)
        )

    # Any REQUIRED decision input missing/insufficient → unknown (first-class
    # token). `vix_now` is deliberately absent from this tuple: the vol axis
    # decides without it whenever the realized leg is elevated (#1559), so
    # demanding it here would blank labels the disjunction had already settled.
    decision_inputs = (c, sma50_now, sma200_now, slope_now, dist200, atr_pct_q_now)
    if not all(math.isfinite(x) for x in decision_inputs):
        return {"market_state": _UNKNOWN, **telemetry}

    vol = _vol_axis(atr_pct_q_now, vix_now)
    if vol is None:  # realized leg low and no VIX — genuinely undetermined
        return {"market_state": _UNKNOWN, **telemetry}

    trend = _trend_axis(c, sma50_now, sma200_now, slope_now, dist200)
    if trend == "neutral":
        trend = "up" if dist200 >= 0 else "down"

    return {"market_state": _STATE_MAP[(trend, vol)], **telemetry}


# --- I/O wrapper + broadcast enrich (mirrors disagreement.enrich) --------------

INDEX_TICKER = "SPY"
VIX_SERIES_ID = "VIXCLS"
# Trailing sessions to load: the ATR% quantile (252) is the binding window; the
# margin absorbs holidays/gaps. Fewer on disk → classify_state returns 'unknown'.
_HISTORY_SESSIONS = 300

# Explicit empty-frame dtypes (memo §3.1): object for the two string columns,
# float64 for the drivers, nullable boolean for the squeeze flag. Missing
# conventions: ``'unknown'`` label / ``NaN`` driver floats / ``pd.NA`` squeeze.
_EMPTY_COLUMN_DTYPES: dict[str, str] = {
    "market_state": "object",
    "market_state_atr_pct": "float64",
    "market_state_atr_pct_q": "float64",
    "market_state_dist200": "float64",
    "market_state_vix": "float64",
    "market_state_vix_decile": "float64",
    "market_state_squeeze_on": "boolean",
    "market_state_config_version": "object",
}


def _session_dates_on_or_before(grouped_root: Path, asof: dt.date, limit: int) -> list[dt.date]:
    """The newest ``limit`` stored session dates on or before ``asof`` (PIT)."""
    if not grouped_root.is_dir():
        return []
    cutoff = asof.isoformat()
    stems = sorted(
        p.stem for p in grouped_root.glob("*.parquet") if len(p.stem) == 10 and p.stem <= cutoff
    )
    dates: list[dt.date] = []
    for stem in stems[-limit:]:
        try:
            dates.append(dt.date.fromisoformat(stem))
        except ValueError:
            continue
    return dates


def _load_index_ohlc(
    grouped_root: Path, ticker: str, asof: dt.date, sessions: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Assemble the trailing (close, high, low) series for ``ticker`` from the
    whole-market grouped-daily store — disk-only, PIT, split-adjusted."""
    tkr = ticker.upper()
    index: list[pd.Timestamp] = []
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    for d in _session_dates_on_or_before(grouped_root, asof, sessions):
        path = grouped_root / f"{d.isoformat()}.parquet"
        try:
            df = pd.read_parquet(path, columns=["T", "h", "l", "c"])
        except (OSError, ValueError):
            continue
        sub = df[df["T"].astype(str).str.upper() == tkr]
        if sub.empty:
            continue
        row = sub.iloc[-1]
        try:
            c, h, low_v = float(row["c"]), float(row["h"]), float(row["l"])
        except (TypeError, ValueError):
            continue
        index.append(pd.Timestamp(d))
        closes.append(c)
        highs.append(h)
        lows.append(low_v)
    return (
        pd.Series(closes, index=index, dtype=float),
        pd.Series(highs, index=index, dtype=float),
        pd.Series(lows, index=index, dtype=float),
    )


def classify(
    asof: dt.date,
    *,
    grouped_root: Path | str | None = None,
    fred_client=None,
    ticker: str = INDEX_TICKER,
    sessions: int = _HISTORY_SESSIONS,
) -> dict[str, Any]:
    """Load the trailing index bars + VIX and classify the state at ``asof``.

    Disk-only for the index bars (grouped store); VIX via the canonical FRED
    client (injected in tests, ``FREDClient.from_env()`` in production). No raw
    HTTP — the store is local parquet and FRED routes through its one client.
    """
    root = Path(grouped_root) if grouped_root is not None else DEFAULT_RS_HISTORY_ROOT
    close, high, low = _load_index_ohlc(root, ticker, asof, sessions)
    if fred_client is None:
        from alphalens_pipeline.data.macro.fred_client import FREDClient

        fred_client = FREDClient.from_env()
    # `through=asof` is load-bearing, not a hint (#1524). The truncation below is
    # a PIT guard against a FUTURE print; it does nothing about a series that
    # STOPS before asof, which is how a frozen cache stamped one July VIX onto 79
    # brief dates. Asking the client to reach asof is what makes that loud.
    #
    # FREDStaleError is caught here and NOTHING WIDER (#1559). Its own docstring
    # invites a display caller to catch it: the request succeeded and FRED simply
    # has no data that recent, which is a fact about the data and says nothing
    # about this host. The other members of the taxonomy must stay loud:
    # FREDAuthError and a bare FREDError (a 4xx — which is what a REVOKED api key
    # looks like — or a 5xx surviving three retries) are indefinite faults, and
    # degrading them to a realized-leg-only label would keep the banner looking
    # healthy for weeks on one warning a day. They propagate to `enrich`, which
    # stamps 'unknown' with a traceback.
    #
    # A transient blip never reaches here: the client already retries 5xx three
    # times with exponential backoff.
    from alphalens_pipeline.data.macro.fred_client import FREDStaleError

    try:
        vix = fred_client.fetch_series(VIX_SERIES_ID, through=asof)
    except FREDStaleError:
        # Deliberately NOT falling back to `through=None`. The series the refusal
        # was raised over is the frozen-cache defect of #1524 in another dress;
        # absent is the honest reading, and the vol axis decides without it
        # whenever the realized leg is elevated.
        logger.warning(
            "market_state: FRED has no VIX reaching %s — classifying without the "
            "implied leg; the label survives only if the realized leg settles it",
            asof,
        )
        vix = pd.Series([], dtype=float)
    else:
        vix = vix[vix.index <= pd.Timestamp(asof)]  # PIT: never a future VIX print
    return classify_state(close=close, high=high, low=low, vix=vix)


def enrich(
    frame: pd.DataFrame,
    *,
    asof: dt.date,
    grouped_root: Path | str | None = None,
    fred_client=None,
    ticker: str = INDEX_TICKER,
    sessions: int = _HISTORY_SESSIONS,
) -> pd.DataFrame:
    """Return ``frame`` with the index-level market_state columns broadcast onto
    every row (the ``disagreement.enrich`` idiom).

    Market state is computed ONCE for ``asof`` and stamped identically on every
    candidate row; ``market_state_config_version`` is stamped unconditionally.
    With no rows, all columns are still added (zero length, stable dtypes) for a
    stable parquet schema.
    """
    out = frame.copy()
    if len(out) == 0:
        for col, dtype in _EMPTY_COLUMN_DTYPES.items():
            out[col] = pd.Series([], dtype=dtype)
        return out

    try:
        result = classify(
            asof,
            grouped_root=grouped_root,
            fred_client=fred_client,
            ticker=ticker,
            sessions=sessions,
        )
    except Exception:  # best-effort context enrichment
        # A store read / FRED fetch hiccup degrades to 'unknown' rather than
        # aborting the score stage (the buffett/oneil fail-soft precedent).
        logger.warning(
            "market_state: classify failed for %s — stamping 'unknown'",
            asof,
            exc_info=True,
        )
        result = _unknown_result()

    # Broadcast each column with its DECLARED dtype so a populated frame matches
    # the empty-frame schema exactly — in particular the nullable boolean squeeze
    # (a fail-soft ``None`` becomes ``<NA>``, not an object column).
    n = len(out)
    for col, dtype in _EMPTY_COLUMN_DTYPES.items():
        if col == "market_state_config_version":
            out[col] = MARKET_STATE_CONFIG_VERSION
        else:
            out[col] = pd.Series([result[col]] * n, index=out.index, dtype=dtype)
    return out


# --- Raw index 5-session returns: DISPLAY DATA, no version ---------------------
#
# WHY THIS FAMILY IS NOT PART OF THE CLASSIFIER ABOVE, AND MUST NOT BECOME ONE
#
# A four-state word cannot show a sector drawdown. The label watches SPY, and a
# window where SPY fell 1.08% is 'quiet' by its own frozen rule even while the
# Nasdaq drew down 6.2% — the label was not wrong, it was blind by construction
# (2026-10-13..27). These three numbers are the missing breadth: the FIVE-SESSION
# percent change of SPY, QQQ and IWM, per date, so a reader sees the dispersion
# the word folds away.
#
# They are DISPLAY DATA and carry NO ``config_version``, deliberately. A version
# string identifies exactly one emission rule; this family has none to identify.
# ``close(current) / close(5 sessions before current) - 1`` is raw arithmetic on
# a public close series: no threshold, no deadband, no fold, nothing fitted. So
# there is nothing to pool across and nothing a bump could orphan, and the window
# stays editable without partitioning anyone's panel. For the same reason the
# three names stay OUT of :data:`MARKET_STATE_COLUMNS` and
# :data:`MARKET_STATE_CONFIG_VERSION`'s poolability story: putting them in would
# claim they were produced by the classifier's frozen rule, which they are not.
#
# They are also NOT a feature set. Measured over 530 sessions the three series
# correlate 0.72-0.94 and ONE principal component explains 88.2% of their
# variance, so a study treating them as three inputs is fitting one. A study that
# wants a market feature derives it from the grouped-daily store itself, with its
# own pre-committed window — see
# docs/research/ml_label_registry_design_2026_09_16.md.
#
# Held out of selection and of the brief sort exactly like the label, and pinned
# there by ``tests/test_no_market_state_in_selection.py``.

#: Stored column name -> the ETF whose close series answers it. This mapping is
#: the WRITER: :func:`five_session_index_returns` iterates it, so these three
#: names are what reaches the parquet.
MARKET_SERIES_TICKERS: dict[str, str] = {
    "market_ret5_spy": "SPY",
    "market_ret5_qqq": "QQQ",
    "market_ret5_iwm": "IWM",
}

#: The DECLARATION, written out rather than derived from
#: :data:`MARKET_SERIES_TICKERS`. Spelling the names a second time is the point:
#: a declaration assembled from the writer's own keys cannot disagree with it, so
#: renaming one would rename a production column with nothing red (the inversion
#: ``brief_contract/columns.py`` documents for four of the enricher families).
#: Here the tuple only ever CHECKS a frame — :func:`enrich_index_series` reports
#: a disagreement through ``warn_on_column_disagreement`` and never repairs one.
MARKET_SERIES_COLUMNS: tuple[str, ...] = (
    "market_ret5_spy",
    "market_ret5_qqq",
    "market_ret5_iwm",
)

#: Sessions in the window. Six closes span five sessions of return.
MARKET_SERIES_WINDOW_SESSIONS = 5

#: How far the newest stored session may lag ``asof`` before the answer is NaN
#: rather than a number.
#:
#: NOT ``rs_history.LISTING_STATUS_MAX_LAG_SESSIONS`` (5), which this first copied.
#: That constant guards a slow boolean — was a ticker listed — where a week of lag
#: changes nothing; this is a 5-session RETURN, where a stale anchor silently
#: measures the wrong five sessions. And its premise no longer holds: it assumed a
#: healthy store sits ~4 sessions behind because free-tier Polygon publishes
#: session D around D+2. Since #1774 anchored the top-up to Polygon's Eastern day
#: boundary the store holds the session that just closed — measured over the seven
#: days to 2026-10-28, the 00:05 ET fire landed it every time with zero 403s, and
#: the store's newest session equalled the last closed session.
#:
#: So a healthy lag is ZERO sessions, and a weekend costs nothing: the pipeline
#: runs 7 days a week, and a Saturday or Sunday ``asof`` resolves to the Friday,
#: which the session calendar counts as no lag at all (verified).
#:
#: 1 leaves exactly one session of slack for one failed top-up night. Measured
#: 2026-10-30 against a store ending 2026-09-03, by stall depth: at 5 a THREE-day
#: stall still answers — which would publish three consecutive briefs carrying one
#: identical trio and then let the backfill bake those duplicates into the
#: published parquets. At 1, a two-session stall is already NaN.
#:
#: The bound is what stops a silent frozen trio. Measured on 2026-10-29 against a
#: Mac mirror whose store ended 2026-09-03: asked for 2026-10-14, 2026-10-27 and
#: 2026-10-28, the anchor rolled all three back to 2026-09-03 and WITHOUT this
#: bound every one of them answered the identical trio (+0.27% / -0.48% / -1.54%)
#: with no error and no warning — #1524's frozen-VIX defect in another dress.
MARKET_SERIES_MAX_LAG_SESSIONS = 1

_SERIES_DTYPE = "float64"


def five_session_index_returns(
    asof: dt.date,
    *,
    grouped_root: Path | str | None = None,
    window: int = MARKET_SERIES_WINDOW_SESSIONS,
    max_lag_sessions: int = MARKET_SERIES_MAX_LAG_SESSIONS,
    exchange: str = DEFAULT_EXCHANGE,
) -> dict[str, float]:
    """``{column: 5-session percent change}`` for the three broad-index ETFs.

    Disk-only (the grouped-daily store), PIT, split-adjusted. The anchor is the
    newest STORED session on or before ``asof`` — the thematic pipeline runs 7
    days a week, so a weekend brief date rolls back to the last real close — and
    the lookback is ``window`` sessions before THAT resolved anchor, taken from
    the exchange calendar.

    Both endpoints are read BY LABEL, never by position. Position is not session:
    the store can hold an unhealed gap, and ``iloc[-1 - window]`` over whatever
    happens to be on disk would silently measure a longer span and still return a
    plausible number. A missing endpoint is ``float('nan')`` — never a shorter
    window, never a 0.0 (the ``rs_percentile`` tri-state discipline, in the one
    missing convention a float64 column has).

    NaN for a given column when: the store holds no session on or before
    ``asof``; the newest one lags ``asof`` by more than ``max_lag_sessions``
    sessions (see that constant — this is the guard against stamping one frozen
    trio onto every later date); the lookback snapshot is off disk; or that ETF is
    absent from, or non-positively priced in, either endpoint.
    """
    nan = float("nan")
    blank = dict.fromkeys(MARKET_SERIES_TICKERS, nan)

    root = Path(grouped_root) if grouped_root is not None else DEFAULT_RS_HISTORY_ROOT
    current = _newest_session_on_or_before(root, asof)
    if current is None:
        logger.warning("market_ret5: no stored session on or before %s", asof)
        return blank
    if current < n_sessions_before(asof, max_lag_sessions, exchange):
        logger.warning(
            "market_ret5: newest stored session %s lags %s by more than %d sessions — "
            "returning NaN rather than a stale trio",
            current,
            asof,
            max_lag_sessions,
        )
        return blank

    lookback = n_sessions_before(current, window, exchange)
    now_map = read_grouped_day(root, current)
    then_map = read_grouped_day(root, lookback)
    if now_map is None or then_map is None:
        logger.warning(
            "market_ret5: an endpoint is off disk (anchor %s present=%s, lookback %s present=%s)",
            current,
            now_map is not None,
            lookback,
            then_map is not None,
        )
        return blank

    out: dict[str, float] = {}
    for column, ticker in MARKET_SERIES_TICKERS.items():
        now = _close(now_map.get(ticker.upper(), {}))
        then = _close(then_map.get(ticker.upper(), {}))
        out[column] = nan if now is None or then is None else now / then - 1.0
    return out


def enrich_index_series(
    frame: pd.DataFrame,
    *,
    asof: dt.date,
    grouped_root: Path | str | None = None,
    window: int = MARKET_SERIES_WINDOW_SESSIONS,
    max_lag_sessions: int = MARKET_SERIES_MAX_LAG_SESSIONS,
    exchange: str = DEFAULT_EXCHANGE,
) -> pd.DataFrame:
    """Return ``frame`` with the three index returns broadcast onto every row.

    Computed ONCE for ``asof`` and stamped identically on every candidate row
    (the ``disagreement.enrich`` idiom). No version column is stamped — see the
    section comment above for why this family has none.

    Each column is assigned with its DECLARED dtype on BOTH paths, populated and
    empty, so a quiet day's parquet and a normal day's carry the same schema. The
    populated path matters as much as the empty one: letting pandas infer an
    all-NaN fail-soft column would store ``object``.
    """
    out = frame.copy()
    if len(out) == 0:
        for column in MARKET_SERIES_TICKERS:
            out[column] = pd.Series([], dtype=_SERIES_DTYPE)
        return out

    try:
        values = five_session_index_returns(
            asof,
            grouped_root=grouped_root,
            window=window,
            max_lag_sessions=max_lag_sessions,
            exchange=exchange,
        )
    except Exception:  # best-effort context enrichment
        # A store read hiccup degrades to NaN rather than aborting the score
        # stage (the market_state / buffett / oneil fail-soft precedent).
        logger.warning(
            "market_ret5: five_session_index_returns failed for %s — stamping NaN",
            asof,
            exc_info=True,
        )
        values = dict.fromkeys(MARKET_SERIES_TICKERS, float("nan"))

    n = len(out)
    inherited = set(frame.columns)
    for column in MARKET_SERIES_TICKERS:
        out[column] = pd.Series([values[column]] * n, index=out.index, dtype=_SERIES_DTYPE)
    # The declaration is checked against the frame, never imposed on it.
    warn_on_column_disagreement(
        logger,
        writer="enrich_index_series",
        declaration="MARKET_SERIES_COLUMNS",
        produced=[c for c in out.columns if c not in inherited],
        declared=MARKET_SERIES_COLUMNS,
    )
    return out


__all__ = [
    "MARKET_SERIES_COLUMNS",
    "MARKET_SERIES_MAX_LAG_SESSIONS",
    "MARKET_SERIES_TICKERS",
    "MARKET_SERIES_WINDOW_SESSIONS",
    "MARKET_STATE_COLUMNS",
    "MARKET_STATE_CONFIG_VERSION",
    "classify",
    "classify_state",
    "enrich",
    "enrich_index_series",
    "five_session_index_returns",
]
