"""DRF tests for ``/v1/edge/chart/<brief_date>/<ticker>`` (PR-1 backend).

Ingest a fake parquet store carrying the ``chart_payload_json`` column, then
exercise the read-only endpoint. Verifies the stored-payload shape, the
graceful NO_DATA / NO_STRUCTURE shapes, the 404 paths (missing row / bad date),
case-insensitive ticker resolution, and the auth gate.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd
import pytest
from django.test import override_settings
from edge.ingest.parquet import rebuild_from_parquet
from rest_framework.test import APIClient

_BRIEF_DATE = "2026-05-27"

_OK_PAYLOAD = {
    "status": "OK",
    "bars": [
        {
            "time": "2026-05-27",
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "volume": 1000.0,
        }
    ],
    "price_lines": {"entry": 100.0, "tp": [110.0], "stop": 95.0},
    "markers": [
        {
            "time": "2026-05-27",
            "kind": "ENTRY",
            "level_id": "E1",
            "price": 100.0,
            "label": "E1",
            "ambiguous": False,
        }
    ],
    "ambiguous_bars": 0,
    "intrabar_rule": "sl_first",
    "rth_only": True,
}

_NO_STRUCTURE_PAYLOAD = {
    "status": "NO_STRUCTURE",
    "bars": [],
    "price_lines": {"entry": None, "tp": [], "stop": None},
    "markers": [],
    "ambiguous_bars": 0,
    "intrabar_rule": "sl_first",
    "rth_only": True,
}

# Strict DRF config to exercise the IsAuthenticated default (the edge conftest
# forces AllowAny on the dev views; this mirrors auth_cf's STRICT block).
_STRICT_REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "auth_cf.authentication.CloudflareAccessAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}


def _row(
    ticker: str,
    *,
    chart_payload_json: str | None,
    classification="TP_FULL",
    terminal=True,
    holding_days_elapsed=4,
    open_r=None,
    realized_r=1.5,
) -> dict:
    row: dict = {
        "brief_date": dt.date.fromisoformat(_BRIEF_DATE),
        "ticker": ticker,
        "plannable": True,
        "terminal": terminal,
        "ladder_classification": classification,
        "holding_days_elapsed": holding_days_elapsed,
        "open_r": open_r,
        "realized_r": realized_r,
    }
    if chart_payload_json is not None:
        row["chart_payload_json"] = chart_payload_json
    return row


def _write_and_ingest(directory: Path, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_parquet(directory / f"{_BRIEF_DATE}.parquet", index=False)
    rebuild_from_parquet(directory)


@pytest.mark.django_db
def test_chart_endpoint_returns_stored_payload_shape(tmp_path: Path) -> None:
    _write_and_ingest(tmp_path, [_row("CRUS", chart_payload_json=json.dumps(_OK_PAYLOAD))])

    resp = APIClient().get(f"/v1/edge/chart/{_BRIEF_DATE}/CRUS")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert body["ticker"] == "CRUS"
    assert body["ladder_classification"] == "TP_FULL"
    assert body["ambiguous_bars"] == 0
    assert body["intrabar_rule"] == "sl_first"
    assert body["rth_only"] is True
    assert isinstance(body["bars"], list) and body["bars"]
    assert set(body["price_lines"]) == {"entry", "tp", "stop"}
    marker = body["markers"][0]
    assert set(marker) == {"time", "kind", "level_id", "price", "label", "ambiguous"}
    # Lifecycle fields so the frontend can style Open vs Closed + "Day N".
    assert body["terminal"] is True
    assert body["holding_days_elapsed"] == 4
    assert body["open_r"] is None
    assert body["realized_r"] == 1.5


@pytest.mark.django_db
def test_chart_endpoint_open_position_lifecycle_fields(tmp_path: Path) -> None:
    """An ONGOING (non-terminal) position carries terminal=False, a populated
    open_r, a null realized_r, and a holding-day count for the "Day N" label."""
    _write_and_ingest(
        tmp_path,
        [
            _row(
                "CRUS",
                chart_payload_json=json.dumps(_OK_PAYLOAD),
                classification="",
                terminal=False,
                holding_days_elapsed=2,
                open_r=0.4,
                realized_r=None,
            )
        ],
    )
    body = APIClient().get(f"/v1/edge/chart/{_BRIEF_DATE}/CRUS").json()
    assert body["terminal"] is False
    assert body["open_r"] == 0.4
    assert body["realized_r"] is None
    assert body["holding_days_elapsed"] == 2


@pytest.mark.django_db
def test_chart_endpoint_no_data_and_no_structure_shapes(tmp_path: Path) -> None:
    _write_and_ingest(
        tmp_path,
        [
            # Blank column (older row) -> graceful NO_DATA, never 500.
            _row("EMPTY", chart_payload_json=""),
            _row("NOSTRUCT", chart_payload_json=json.dumps(_NO_STRUCTURE_PAYLOAD)),
        ],
    )
    client = APIClient()

    no_data = client.get(f"/v1/edge/chart/{_BRIEF_DATE}/EMPTY").json()
    assert no_data["status"] == "NO_DATA"
    assert no_data["bars"] == []
    assert no_data["markers"] == []

    no_struct = client.get(f"/v1/edge/chart/{_BRIEF_DATE}/NOSTRUCT").json()
    assert no_struct["status"] == "NO_STRUCTURE"
    assert no_struct["bars"] == []
    assert no_struct["price_lines"] == {"entry": None, "tp": [], "stop": None}


@pytest.mark.django_db
def test_chart_endpoint_corrupt_payload_degrades_to_no_data(tmp_path: Path) -> None:
    """A stored payload that fails serialisation (e.g. a bar with a non-numeric
    OHLC value) must NOT surface a 400/500 — the endpoint degrades to a stable
    NO_DATA response in the same shape (zen MEDIUM, PR #496)."""
    corrupt = {
        "status": "OK",
        "bars": [
            {
                "time": "2026-05-01",
                "open": 1.0,
                "high": "not-a-number",  # breaks FloatField.to_representation
                "low": 0.9,
                "close": 1.0,
                "volume": 1.0,
            }
        ],
        "price_lines": {"entry": 1.0, "tp": [1.1], "stop": 0.9},
        "markers": [],
        "ambiguous_bars": 0,
        "intrabar_rule": "sl_first",
        "rth_only": True,
    }
    _write_and_ingest(tmp_path, [_row("CRUS", chart_payload_json=json.dumps(corrupt))])

    resp = APIClient().get(f"/v1/edge/chart/{_BRIEF_DATE}/CRUS")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "NO_DATA"
    assert body["bars"] == []
    assert body["markers"] == []
    assert body["ticker"] == "CRUS"  # outcome identity still populated
    # Lifecycle fields survive the fallback path (terminal from the row).
    assert body["terminal"] is True
    assert "holding_days_elapsed" in body
    assert "open_r" in body
    assert "realized_r" in body


@pytest.mark.django_db
def test_chart_endpoint_404_on_missing_row_and_bad_date(tmp_path: Path) -> None:
    _write_and_ingest(tmp_path, [_row("CRUS", chart_payload_json=json.dumps(_OK_PAYLOAD))])
    client = APIClient()

    # Unknown ticker -> 404.
    assert client.get(f"/v1/edge/chart/{_BRIEF_DATE}/UNKNOWN").status_code == 404
    # Non-ISO date segment -> 404 (no such resource, not 400).
    assert client.get("/v1/edge/chart/not-a-date/CRUS").status_code == 404
    # Lower-case ticker URL still resolves (upper-cased before lookup).
    assert client.get(f"/v1/edge/chart/{_BRIEF_DATE}/crus").status_code == 200


@pytest.mark.django_db
def test_chart_endpoint_is_auth_gated(tmp_path: Path) -> None:
    """Unauthenticated request is denied like the rest of ``/v1/edge/*``.

    The endpoint declares NO explicit ``permission_classes``, so in production it
    inherits the project-wide ``IsAuthenticated`` default. The edge-package autouse
    conftest forces AllowAny on every view for the test run, so this test pins the
    strict permission onto the chart view class for the duration of the request
    (mirroring how the conftest itself patches the dev views) and asserts the
    unauthenticated request is rejected with 401.
    """
    from rest_framework.permissions import IsAuthenticated

    from edge.api.chart import EdgeChartView

    _write_and_ingest(tmp_path, [_row("CRUS", chart_payload_json=json.dumps(_OK_PAYLOAD))])

    # Production guarantee: the view declares no OWN permission_classes, so it
    # inherits the project-wide IsAuthenticated default (base.py). (The edge
    # conftest sets it on the dev views via class assignment, but the source class
    # must not carry its own.)
    assert "permission_classes" not in EdgeChartView.__dict__

    _MISSING = object()
    original = EdgeChartView.__dict__.get("permission_classes", _MISSING)
    EdgeChartView.permission_classes = [IsAuthenticated]
    try:
        with override_settings(REST_FRAMEWORK=_STRICT_REST_FRAMEWORK):
            resp = APIClient().get(f"/v1/edge/chart/{_BRIEF_DATE}/CRUS")
    finally:
        if original is _MISSING:
            del EdgeChartView.permission_classes
        else:
            EdgeChartView.permission_classes = original
    assert resp.status_code == 401
