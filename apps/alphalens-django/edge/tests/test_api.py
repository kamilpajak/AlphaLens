"""End-to-end DRF tests for ``/v1/edge/{summary,outcomes}``.

Ingest a fake parquet store, then exercise the read-only endpoints. Verifies the
N-gated summary shape (insufficient vs computed), the open-excluded-from-mean
invariant, the per-candidate outcomes shape + status filter, and that the
benchmark-excess is carried through at the return level.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from edge.api.summary import N_GATE_THRESHOLD
from edge.ingest.parquet import rebuild_from_parquet
from rest_framework.test import APIClient


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
    # "showing N of M" instead of silently dropping the oldest rows.
    from edge.api import views as edge_views

    _write_parquet(
        tmp_path,
        "2026-05-27",
        [_terminal(f"T{i}", excess=0.01, realized_r=0.5) for i in range(3)],
    )
    rebuild_from_parquet(tmp_path)

    # Under the cap: everything returned, not truncated.
    body = APIClient().get("/v1/edge/outcomes").json()
    assert len(body["data"]) == 3
    assert body["total"] == 3
    assert body["returned"] == 3
    assert body["truncated"] is False

    # Cap below the match count: rows are capped but `total` still reports the
    # full match count and `truncated` flags the drop.
    monkeypatch.setattr(edge_views, "_OUTCOMES_LIMIT", 2)
    capped = APIClient().get("/v1/edge/outcomes").json()
    assert len(capped["data"]) == 2
    assert capped["total"] == 3
    assert capped["returned"] == 2
    assert capped["truncated"] is True


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
