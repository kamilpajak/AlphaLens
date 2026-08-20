"""L3 golden-API test (test-strategy Phase 3): ingest the real golden brief
parquet → DRF, asserting the side-effect invariants the SPA depends on.

Uses the SAME real golden brief parquet the research-side replay test produces
(``apps/alphalens-research/tests/golden/fixtures/brief_day/golden/``) so the two
halves of the chain are pinned to one artifact: the brief shape the pipeline
emits is the shape Django ingests and serves.

Pins:
  * #6 ingest-drop seam — ``DayMeta.n_candidates == len(parquet rows)`` (the
    Path.home orphan-drop class silently deleted rows; assert the count, not a
    hardcoded number).
  * DRF envelope + nested-JSON drift — ``/v1/days`` paginated ``{data, meta}``;
    ``/v1/days/{date}`` bare object with ``candidates`` whose JSONFields
    (``gates_passed`` list, ``brief_trade_setup`` dict) survive the round-trip.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pandas as pd
import pytest
from rest_framework.test import APIClient

from briefs.ingest.parquet import rebuild_from_parquet

# Single source of truth: the golden produced by the research-side recorder.
# The asof is DISCOVERED from the directory rather than hardcoded — the golden is
# re-cut onto a new day whenever the brief contract changes (2026-05-24 -> 2026-08-19
# for the causal-support taxonomy), and a hardcoded date turns every honest
# re-baseline into a cross-app CI break.
_GOLDEN_DIR = (
    Path(__file__).resolve().parents[3]
    / "alphalens-research"
    / "tests"
    / "golden"
    / "fixtures"
    / "brief_day"
    / "golden"
)


def _discover_golden() -> tuple[dt.date, Path]:
    """The single ``<asof>.parquet`` in the golden dir, as ``(asof, path)``.

    Raises when the count is not exactly one: zero means the recorder has not run,
    and more than one means an old recording was left behind, which would make
    WHICH day this test pins depend on sort order.
    """
    found = sorted(_GOLDEN_DIR.glob("*.parquet"))
    if len(found) != 1:
        raise FileNotFoundError(
            f"expected exactly 1 golden brief parquet in {_GOLDEN_DIR}, found {len(found)}"
            f" ({[p.name for p in found]}) — run "
            "apps/alphalens-research/scripts/record_golden_brief.py (live capture) "
            "or remove the superseded recording"
        )
    return dt.date.fromisoformat(found[0].stem), found[0]


_ASOF, _GOLDEN_PARQUET = _discover_golden()


@pytest.fixture
def golden_briefs_dir(tmp_path: Path) -> Path:
    if not _GOLDEN_PARQUET.exists():
        raise FileNotFoundError(
            f"golden brief parquet missing: {_GOLDEN_PARQUET} — run "
            "apps/alphalens-research/scripts/record_golden_brief.py (live capture) "
            "or check the cross-app relative path"
        )
    shutil.copyfile(_GOLDEN_PARQUET, tmp_path / f"{_ASOF.isoformat()}.parquet")
    return tmp_path


@pytest.mark.django_db
class TestGoldenApi:
    def test_ingest_drop_invariant_n_candidates_equals_rows(self, golden_briefs_dir: Path):
        n_rows = len(pd.read_parquet(_GOLDEN_PARQUET))
        result = rebuild_from_parquet(briefs_dir=golden_briefs_dir)
        assert result.total_briefs == n_rows

        client = APIClient()
        resp = client.get(f"/v1/days/{_ASOF.isoformat()}")
        assert resp.status_code == 200
        body = resp.json()
        # The ingest-drop seam: every parquet row must survive to the API count.
        assert body["n_candidates"] == n_rows
        assert len(body["candidates"]) == n_rows

    def test_days_list_envelope(self, golden_briefs_dir: Path):
        rebuild_from_parquet(briefs_dir=golden_briefs_dir)
        resp = APIClient().get("/v1/days")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"data", "meta"}
        assert body["meta"]["total"] == 1
        assert body["data"][0]["date"] == _ASOF.isoformat()
        assert body["data"][0]["n_candidates"] == len(pd.read_parquet(_GOLDEN_PARQUET))

    def test_nested_json_fields_survive_to_api(self, golden_briefs_dir: Path):
        rebuild_from_parquet(briefs_dir=golden_briefs_dir)
        body = APIClient().get(f"/v1/days/{_ASOF.isoformat()}").json()
        candidate = body["candidates"][0]
        # gates_passed is a list[str] JSONField; brief_trade_setup is a dict
        # JSONField (the SPA renders both). A coercion regression would surface
        # here as a wrong type, not a 500.
        assert isinstance(candidate["gates_passed"], list)
        assert isinstance(candidate["brief_trade_setup"], dict)
        assert candidate["brief_trade_setup"]["schema_version"]
