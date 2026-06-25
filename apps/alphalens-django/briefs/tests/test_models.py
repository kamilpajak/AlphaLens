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
