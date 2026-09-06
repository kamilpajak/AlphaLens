"""Field-existence tests for the Brief model.

These tests are DB-free: they introspect ``Brief._meta.get_fields()`` to check
that expected fields are declared on the model.  No database round-trip needed
— the model class is fully constructed at import time.
"""

from __future__ import annotations

import django.db.models as m

from briefs.models import Brief


def _field_by_name(name: str) -> m.Field:
    return Brief._meta.get_field(name)


def test_selection_score_field_exists() -> None:
    field = _field_by_name("selection_score")
    assert isinstance(field, m.FloatField)
    assert field.null is True
    assert field.blank is True


def test_atr_penalty_field_exists() -> None:
    field = _field_by_name("atr_penalty")
    assert isinstance(field, m.FloatField)
    assert field.null is True
    assert field.blank is True


def test_scorer_config_version_field_exists() -> None:
    field = _field_by_name("scorer_config_version")
    assert isinstance(field, m.CharField)
    assert field.max_length == 128
    assert field.blank is True
    assert field.default == ""


# Event-lane provenance (epic #1293, issue #1298): the eight columns the SPA
# card / day-list source filter read. Defaults mirror the parquet writer
# (alphalens_pipeline.events.merge): NOT-NULL text/bool with a safe default so
# the AddField migration is safe over a populated table; the event facts are
# nullable because they are None on every thematic row.
def test_source_field_exists() -> None:
    field = _field_by_name("source")
    assert isinstance(field, m.CharField)
    assert field.blank is True
    assert field.default == ""


def test_event_overlap_field_exists() -> None:
    field = _field_by_name("event_overlap")
    assert isinstance(field, m.BooleanField)
    assert field.default is False


def test_event_n_insiders_field_exists() -> None:
    field = _field_by_name("event_n_insiders")
    assert isinstance(field, m.IntegerField)
    assert field.null is True


def test_event_cluster_usd_field_exists() -> None:
    field = _field_by_name("event_cluster_usd")
    assert isinstance(field, m.FloatField)
    assert field.null is True


def test_event_buyers_field_exists() -> None:
    field = _field_by_name("event_buyers")
    assert isinstance(field, m.JSONField)
    assert field.null is True


def test_event_arrival_session_field_exists() -> None:
    field = _field_by_name("event_arrival_session")
    assert isinstance(field, m.DateField)
    assert field.null is True


def test_event_filing_lag_bdays_field_exists() -> None:
    field = _field_by_name("event_filing_lag_bdays")
    assert isinstance(field, m.IntegerField)
    assert field.null is True


def test_event_gate_version_field_exists() -> None:
    field = _field_by_name("event_gate_version")
    assert isinstance(field, m.CharField)
    assert field.blank is True
    assert field.default == ""
