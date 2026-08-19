"""End-to-end DRF tests for ``/v1/edge/{summary,outcomes}``.

Ingest a fake parquet store, then exercise the read-only endpoints. Verifies the
N-gated summary shape (insufficient vs computed), the open-excluded-from-mean
invariant, the per-candidate outcomes shape + status filter, and that the
benchmark-excess is carried through at the return level.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd
import pytest
from rest_framework.test import APIClient

from edge.api.summary import N_GATE_THRESHOLD
from edge.ingest.parquet import rebuild_from_parquet


def _write_parquet(directory: Path, iso_date: str, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_parquet(directory / f"{iso_date}.parquet", index=False)


def _terminal(
    ticker: str,
    *,
    excess: float,
    realized_r: float,
    classification="TP_FULL",
    theme: str | None = None,
) -> dict:
    return {
        "brief_date": dt.date(2026, 5, 27),
        "ticker": ticker,
        "theme": theme,
        "plannable": True,
        "terminal": True,
        "matured_at": dt.date(2026, 6, 2),
        "ladder_classification": classification,
        "captured_tp_count": 1,
        "touched_tp_count": 3,
        "realized_r": realized_r,
        "open_r": None,
        "forward_return": excess + 0.02,
        "benchmark_window_return": 0.02,
        "market_excess_return": excess,
        "holding_days_elapsed": 11,
        "realized_risk_pct": 0.01,
        "realized_return_pct_of_book": 0.002,
        "tiers_filled_count": 2.0,
    }


def _ongoing(ticker: str, *, open_r: float, theme: str | None = None) -> dict:
    return {
        "brief_date": dt.date(2026, 5, 27),
        "ticker": ticker,
        "theme": theme,
        "plannable": True,
        "terminal": False,
        "matured_at": None,
        "ladder_classification": "OPEN",
        "realized_r": None,
        "open_r": open_r,
        "forward_return": 0.01,
        "market_excess_return": None,
    }


@pytest.mark.django_db
def test_summary_gated_when_below_threshold(tmp_path: Path):
    _write_parquet(
        tmp_path,
        "2026-05-27",
        [_terminal(f"T{i}", excess=0.01, realized_r=0.5) for i in range(5)]
        + [_ongoing("OP1", open_r=0.3)],
    )
    rebuild_from_parquet(tmp_path)

    resp = APIClient().get("/v1/edge/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["edge"]["status"] == "insufficient"
    assert body["edge"]["n_matured"] == 5
    assert body["edge"]["threshold"] == N_GATE_THRESHOLD
    assert body["edge"]["market_excess_mean"] is None
    # Open is descriptive only — never folded into the (hidden) mean.
    assert body["open_positions"]["n_open"] == 1
    # Deployment is N-independent.
    assert body["deployment"]["n_terminal"] == 5
    assert body["deployment"]["fill_rate"] is not None


@pytest.mark.django_db
def test_summary_computed_when_at_threshold(tmp_path: Path):
    _write_parquet(
        tmp_path,
        "2026-05-27",
        [_terminal(f"T{i}", excess=0.03, realized_r=0.5) for i in range(N_GATE_THRESHOLD)],
    )
    rebuild_from_parquet(tmp_path)

    body = APIClient().get("/v1/edge/summary").json()
    assert body["edge"]["status"] in ("early", "ok")
    assert body["edge"]["market_excess_mean"] == pytest.approx(0.03)
    assert body["edge"]["market_excess_quantiles"]["p50"] is not None
    assert body["benchmark"] == "SPY"


@pytest.mark.django_db
def test_outcomes_shape_and_theme_from_record(tmp_path: Path):
    # Theme is carried ON the ladder-outcome parquet (stamped at the brief by the
    # population monitor), NOT re-joined from the briefs cache. A row whose theme
    # has churned out of the latest brief still renders its theme; an unstamped
    # (older) row renders None.
    _write_parquet(
        tmp_path,
        "2026-05-27",
        [
            _terminal("AMPL", excess=0.04, realized_r=1.2, theme="ai-infra"),
            _ongoing("BLBD", open_r=0.16, theme="ev"),
            _ongoing("MRCY", open_r=0.07),  # no theme stamped (older row) → None
        ],
    )
    rebuild_from_parquet(tmp_path)

    body = APIClient().get("/v1/edge/outcomes").json()
    rows = {r["ticker"]: r for r in body["data"]}
    assert set(rows) == {"AMPL", "BLBD", "MRCY"}
    ampl = rows["AMPL"]
    assert ampl["terminal"] is True
    assert ampl["market_excess_return"] == pytest.approx(0.04)
    assert ampl["realized_r"] == pytest.approx(1.2)
    assert ampl["theme"] == "ai-infra"
    assert rows["BLBD"]["theme"] == "ev"
    assert rows["BLBD"]["open_r"] == pytest.approx(0.16)
    assert rows["MRCY"]["theme"] is None


@pytest.mark.django_db
def test_outcomes_expose_tp_capture_counts(tmp_path: Path):
    # A partial-entry TP_FULL row: all three TP levels TOUCHED but only one SOLD.
    # The outcomes row must carry both counts so the SPA can flag that TP_FULL /
    # the three green arrows overstate what was captured.
    _write_parquet(tmp_path, "2026-05-27", [_terminal("AMPL", excess=0.04, realized_r=0.2)])
    rebuild_from_parquet(tmp_path)

    row = APIClient().get("/v1/edge/outcomes").json()["data"][0]
    assert row["captured_tp_count"] == 1
    assert row["touched_tp_count"] == 3


@pytest.mark.django_db
def test_outcomes_scorer_config_version(tmp_path: Path):
    # scorer_config_version is stamped at the brief by the population monitor and
    # carried on the outcome record — no re-join required.  A row with a version
    # string must appear verbatim in the response; an unstamped row renders None.
    _write_parquet(
        tmp_path,
        "2026-05-27",
        [
            {
                **_terminal("AMPL", excess=0.04, realized_r=1.2),
                "scorer_config_version": "scorer-v1-test",
            },
            {**_terminal("BLBD", excess=0.02, realized_r=0.5)},  # no version → None
        ],
    )
    rebuild_from_parquet(tmp_path)

    body = APIClient().get("/v1/edge/outcomes").json()
    rows = {r["ticker"]: r for r in body["data"]}
    assert rows["AMPL"]["scorer_config_version"] == "scorer-v1-test"
    assert rows["BLBD"]["scorer_config_version"] is None


@pytest.mark.django_db
def test_outcomes_status_filter(tmp_path: Path):
    _write_parquet(
        tmp_path,
        "2026-05-27",
        [_terminal("AMPL", excess=0.04, realized_r=1.2), _ongoing("BLBD", open_r=0.16)],
    )
    rebuild_from_parquet(tmp_path)

    terminal = APIClient().get("/v1/edge/outcomes?status=terminal").json()["data"]
    assert {r["ticker"] for r in terminal} == {"AMPL"}
    ongoing = APIClient().get("/v1/edge/outcomes?status=ongoing").json()["data"]
    assert {r["ticker"] for r in ongoing} == {"BLBD"}


@pytest.mark.django_db
def test_outcomes_reports_true_total_and_truncation(tmp_path: Path, monkeypatch):
    # The listing is capped at `_OUTCOMES_LIMIT`; the response must carry the TRUE
    # matching total + a truncation flag so the SPA can render an honest
    # "showing N of M" instead of silently dropping rows. Distinct matured_at per
    # row so the test also pins WHICH rows survive the cap (most recently active).
    from edge.api import views as edge_views

    _write_parquet(
        tmp_path,
        "2026-05-27",
        [
            {
                **_terminal(f"T{i}", excess=0.01, realized_r=0.5),
                "matured_at": dt.date(2026, 6, 2 + i),
            }
            for i in range(3)
        ],
    )
    rebuild_from_parquet(tmp_path)

    # Under the cap: everything returned, not truncated.
    body = APIClient().get("/v1/edge/outcomes").json()
    assert len(body["data"]) == 3
    assert body["total"] == 3
    assert body["returned"] == 3
    assert body["truncated"] is False

    # Cap below the match count: rows are capped but `total` still reports the
    # full match count and `truncated` flags the drop. The survivors are the two
    # most recently matured rows; the least recently active row is the victim.
    monkeypatch.setattr(edge_views, "_OUTCOMES_LIMIT", 2)
    capped = APIClient().get("/v1/edge/outcomes").json()
    assert {r["ticker"] for r in capped["data"]} == {"T1", "T2"}
    assert capped["total"] == 3
    assert capped["returned"] == 2
    assert capped["truncated"] is True


@pytest.mark.django_db
def test_outcomes_cap_retains_recently_matured_time_stop(tmp_path: Path, monkeypatch):
    # Production regression (2026-08-18): a TIME_STOP necessarily carries an OLD
    # brief_date (the position aged to its TTL), so capping "newest by brief_date"
    # structurally evicted every TIME_STOP while fresher ongoing rows survived.
    # The cap must evict by RECENCY OF ACTIVITY (matured_at for terminal rows,
    # brief_date for ongoing), never by brief age alone.
    from edge.api import views as edge_views

    _write_parquet(
        tmp_path,
        "2026-05-01",
        [
            {
                **_terminal("TSTP", excess=-0.05, realized_r=-0.2, classification="TIME_STOP"),
                "matured_at": dt.date(2026, 7, 20),
            }
        ],
    )
    _write_parquet(tmp_path, "2026-06-01", [_ongoing("OLD1", open_r=0.1)])
    _write_parquet(tmp_path, "2026-06-10", [_ongoing("MID2", open_r=0.1)])
    _write_parquet(tmp_path, "2026-06-20", [_ongoing("NEW3", open_r=0.1)])
    rebuild_from_parquet(tmp_path)

    monkeypatch.setattr(edge_views, "_OUTCOMES_LIMIT", 3)
    body = APIClient().get("/v1/edge/outcomes").json()
    tickers = {r["ticker"] for r in body["data"]}
    assert "TSTP" in tickers  # recently matured — must survive despite oldest brief
    assert tickers == {"TSTP", "NEW3", "MID2"}  # victim = least recently active


@pytest.mark.django_db
def test_outcomes_ordered_by_recency(tmp_path: Path):
    # Rows come back ordered by coalesce(matured_at, brief_date) descending —
    # terminal rows sort at their maturity date, ongoing rows at their brief date —
    # tiebroken by (-brief_date, ticker) for determinism.
    _write_parquet(
        tmp_path,
        "2026-05-01",
        [{**_terminal("TERM_A", excess=0.01, realized_r=0.5), "matured_at": dt.date(2026, 7, 20)}],
    )
    _write_parquet(
        tmp_path,
        "2026-06-01",
        [{**_terminal("TERM_C", excess=0.01, realized_r=0.5), "matured_at": dt.date(2026, 6, 15)}],
    )
    _write_parquet(tmp_path, "2026-06-10", [_ongoing("ONGO_D", open_r=0.1)])
    _write_parquet(
        tmp_path,
        "2026-06-20",
        [_ongoing("ONGO_B2", open_r=0.1), _ongoing("ONGO_B1", open_r=0.1)],
    )
    rebuild_from_parquet(tmp_path)

    body = APIClient().get("/v1/edge/outcomes").json()
    assert [r["ticker"] for r in body["data"]] == [
        "TERM_A",  # recency 07-20 (matured)
        "ONGO_B1",  # recency 06-20, ticker tiebreak
        "ONGO_B2",  # recency 06-20
        "TERM_C",  # recency 06-15 (matured)
        "ONGO_D",  # recency 06-10
    ]


@pytest.mark.django_db
def test_outcomes_facets_reflect_window_population_before_filters_and_cap(
    tmp_path: Path, monkeypatch
):
    # `facets` describe the WHOLE window+plannable population — computed BEFORE
    # the status/classification filters and BEFORE the per-page cap — so the SPA
    # chips carry server truth even when the listing itself is filtered/capped.
    from edge.api import views as edge_views

    _write_parquet(
        tmp_path,
        "2026-05-27",
        [
            _terminal("AMPL", excess=0.04, realized_r=1.2),
            _terminal("BBAI", excess=0.02, realized_r=0.8),
            _terminal("RGTI", excess=-0.03, realized_r=-1.0, classification="SL_HIT"),
            _ongoing("BLBD", open_r=0.16),
            _ongoing("RKLB", open_r=0.05),
        ],
    )
    rebuild_from_parquet(tmp_path)

    monkeypatch.setattr(edge_views, "_OUTCOMES_LIMIT", 2)
    body = APIClient().get("/v1/edge/outcomes").json()
    assert body["returned"] == 2  # cap applied to the listing...
    assert body["facets"]["status"] == {"terminal": 3, "ongoing": 2}  # ...not the facets
    assert body["facets"]["classification"] == {
        "terminal": {"TP_FULL": 2, "SL_HIT": 1},
        "ongoing": {"OPEN": 2},
    }

    # The status filter narrows the listing but must NOT change the facets.
    filtered = APIClient().get("/v1/edge/outcomes?status=terminal").json()
    assert filtered["facets"] == body["facets"]


@pytest.mark.django_db
def test_outcomes_facets_drop_empty_classification_bucket(tmp_path: Path):
    # Not-yet-priced rows carry an empty ladder_classification; the "" bucket is
    # dropped from facets.classification but the row still counts in facets.status.
    _write_parquet(
        tmp_path,
        "2026-05-27",
        [
            {**_ongoing("PEND", open_r=0.0), "ladder_classification": ""},
            _terminal("AMPL", excess=0.04, realized_r=1.2),
        ],
    )
    rebuild_from_parquet(tmp_path)

    facets = APIClient().get("/v1/edge/outcomes").json()["facets"]
    assert "" not in facets["classification"]["terminal"]
    assert "" not in facets["classification"]["ongoing"]
    assert facets["classification"] == {"terminal": {"TP_FULL": 1}, "ongoing": {}}
    assert facets["status"] == {"terminal": 1, "ongoing": 1}


@pytest.mark.django_db
def test_outcomes_facets_split_by_row_terminal_flag_not_class_name(tmp_path: Path):
    # The per-view split follows the ACTUAL per-row `terminal` flag, never the
    # nominal semantics of the class name: a NO_FILL whose 7-day entry window is
    # still open is an ONGOING row, while a lapsed NO_FILL is TERMINAL. The same
    # class must therefore be able to appear in BOTH view maps with disjoint
    # counts — the SPA no longer guesses a class's view from its name.
    _write_parquet(
        tmp_path,
        "2026-05-27",
        [
            _terminal("NFA", excess=0.0, realized_r=0.0, classification="NO_FILL"),
            _terminal("NFB", excess=0.0, realized_r=0.0, classification="NO_FILL"),
            {**_ongoing("NFC", open_r=0.0), "ladder_classification": "NO_FILL"},
            _ongoing("BLBD", open_r=0.16),
        ],
    )
    rebuild_from_parquet(tmp_path)

    facets = APIClient().get("/v1/edge/outcomes").json()["facets"]
    assert facets["classification"]["terminal"]["NO_FILL"] == 2
    assert facets["classification"]["ongoing"]["NO_FILL"] == 1
    # No cross-view leakage of the classes that exist in only one view.
    assert "OPEN" not in facets["classification"]["terminal"]
    assert facets["classification"]["ongoing"]["OPEN"] == 1


@pytest.mark.django_db
def test_outcomes_facets_terminal_view_sums_to_status_terminal(tmp_path: Path):
    # Amendment (b): the monitor promises terminal ⇒ real classification, so the
    # dropped ""-class bucket may only ever swallow ONGOING (not-yet-priced)
    # rows. Pinned here as an API contract: the terminal view map carries no ""
    # key and its values sum EXACTLY to facets.status.terminal — the invariant
    # the SPA's "terminal chips sum to ALL" display relies on.
    _write_parquet(
        tmp_path,
        "2026-05-27",
        [
            _terminal("AMPL", excess=0.04, realized_r=1.2),
            _terminal("RGTI", excess=-0.03, realized_r=-1.0, classification="SL_HIT"),
            _terminal("IONQ", excess=-0.01, realized_r=-0.2, classification="TIME_STOP"),
            _terminal("NFA", excess=0.0, realized_r=0.0, classification="NO_FILL"),
            {**_ongoing("PEND", open_r=0.0), "ladder_classification": ""},
            _ongoing("BLBD", open_r=0.16),
        ],
    )
    rebuild_from_parquet(tmp_path)

    facets = APIClient().get("/v1/edge/outcomes").json()["facets"]
    assert "" not in facets["classification"]["terminal"]
    assert sum(facets["classification"]["terminal"].values()) == facets["status"]["terminal"] == 4
    # The ongoing view undercounts status.ongoing by exactly the blank-class rows.
    assert sum(facets["classification"]["ongoing"].values()) == 1
    assert facets["status"]["ongoing"] == 2


@pytest.mark.django_db
def test_outcomes_classification_param_filters_rows_server_side(tmp_path: Path):
    _write_parquet(
        tmp_path,
        "2026-05-27",
        [
            _terminal("AMPL", excess=0.04, realized_r=1.2),
            _terminal("RGTI", excess=-0.03, realized_r=-1.0, classification="SL_HIT"),
            _terminal("IONQ", excess=-0.01, realized_r=-0.2, classification="TIME_STOP"),
        ],
    )
    rebuild_from_parquet(tmp_path)

    body = APIClient().get("/v1/edge/outcomes?classification=SL_HIT,TIME_STOP").json()
    assert {r["ticker"] for r in body["data"]} == {"RGTI", "IONQ"}
    assert body["total"] == 2
    # Facets are pre-filter — all three classes stay visible.
    assert body["facets"]["classification"] == {
        "terminal": {"TP_FULL": 1, "SL_HIT": 1, "TIME_STOP": 1},
        "ongoing": {},
    }


@pytest.mark.django_db
def test_outcomes_classification_composes_with_status(tmp_path: Path):
    _write_parquet(
        tmp_path,
        "2026-05-27",
        [_terminal("AMPL", excess=0.04, realized_r=1.2), _ongoing("BLBD", open_r=0.16)],
    )
    rebuild_from_parquet(tmp_path)

    none = APIClient().get("/v1/edge/outcomes?status=ongoing&classification=TP_FULL").json()
    assert none["data"] == []
    assert none["total"] == 0

    one = APIClient().get("/v1/edge/outcomes?status=terminal&classification=TP_FULL").json()
    assert [r["ticker"] for r in one["data"]] == ["AMPL"]


@pytest.mark.django_db
def test_outcomes_classification_unknown_value_matches_nothing(tmp_path: Path):
    # Unknown values simply match nothing — no validation, no 400 (solo project).
    _write_parquet(tmp_path, "2026-05-27", [_terminal("AMPL", excess=0.04, realized_r=1.2)])
    rebuild_from_parquet(tmp_path)

    resp = APIClient().get("/v1/edge/outcomes?classification=BOGUS")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["total"] == 0
    assert body["truncated"] is False
    assert body["facets"]["classification"] == {"terminal": {"TP_FULL": 1}, "ongoing": {}}


@pytest.mark.django_db
def test_outcomes_facets_respect_window(tmp_path: Path):
    # Facets count only in-window rows — the window floor is applied before the
    # facet aggregation, same as before the listing.
    _write_parquet(
        tmp_path,
        "2026-05-01",
        [{**_terminal("OLD", excess=0.01, realized_r=0.5, classification="SL_HIT")}],
    )
    _write_parquet(tmp_path, "2026-06-01", [_ongoing("NEW", open_r=0.1)])
    rebuild_from_parquet(tmp_path)

    facets = APIClient().get("/v1/edge/outcomes?window=10").json()["facets"]
    assert facets["status"] == {"terminal": 0, "ongoing": 1}
    assert facets["classification"] == {"terminal": {}, "ongoing": {"OPEN": 1}}


@pytest.mark.django_db
def test_outcomes_window_anchored_to_latest_brief_date(tmp_path: Path):
    # `?window=N` counts back from the LATEST brief_date in the cache, not from
    # today — the window must stay stable regardless of when the API is hit
    # relative to the nightly rebuild.
    _write_parquet(tmp_path, "2026-05-01", [_ongoing("OLD", open_r=0.1)])
    _write_parquet(tmp_path, "2026-06-01", [_ongoing("NEW", open_r=0.1)])
    rebuild_from_parquet(tmp_path)

    # Floor = 2026-06-01 - 10d = 2026-05-22: only the newer row qualifies. Were
    # the window anchored to today, the floor would exclude BOTH rows.
    body = APIClient().get("/v1/edge/outcomes?window=10").json()
    assert {r["ticker"] for r in body["data"]} == {"NEW"}
    assert body["total"] == 1


@pytest.mark.django_db
def test_excess_telemetry_endpoint_shape(tmp_path: Path):
    # Enough terminal rows to clear the N-gate so ``trend`` is populated.
    rows = [
        _terminal(f"T{i}", excess=0.01 * ((i % 5) - 2), realized_r=0.5)
        for i in range(N_GATE_THRESHOLD)
    ]
    _write_parquet(tmp_path, "2026-05-27", rows)
    rebuild_from_parquet(tmp_path)

    resp = APIClient().get("/v1/edge/excess-telemetry")
    assert resp.status_code == 200
    body = resp.json()
    assert body["benchmark"] == "SPY"
    assert body["status"] == "ok"
    assert body["n_total"] == N_GATE_THRESHOLD
    assert body["points"] and {"date", "excess", "ticker", "episode_repeat"} <= set(
        body["points"][0]
    )
    assert body["trend"] and {"date", "mean", "lo", "hi"} <= set(body["trend"][0])


@pytest.mark.django_db
def test_summary_exposes_enriched_at_from_watermark(tmp_path: Path, monkeypatch):
    # DEFAULT_LADDER_OUTCOMES_DIR is import-frozen (edge/ingest/parquet.py:49-52),
    # so monkeypatch.setenv would be a no-op here — patch the module attribute
    # directly, and the view must re-read it off the module at call time.
    monkeypatch.setattr("edge.ingest.parquet.DEFAULT_LADDER_OUTCOMES_DIR", tmp_path)
    ts = time.time()
    (tmp_path / ".ingest_watermark.json").write_text(json.dumps({"completed_at": ts}))

    resp = APIClient().get("/v1/edge/summary")
    assert resp.status_code == 200
    assert resp.json()["enriched_at"] is not None  # ISO-8601 string


@pytest.mark.django_db
def test_summary_enriched_at_null_without_watermark(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("edge.ingest.parquet.DEFAULT_LADDER_OUTCOMES_DIR", tmp_path)

    resp = APIClient().get("/v1/edge/summary")
    assert resp.status_code == 200
    assert resp.json()["enriched_at"] is None
