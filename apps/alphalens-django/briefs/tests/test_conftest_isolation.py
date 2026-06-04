"""Regression: the autouse permission-class fixture must not leak state.

``briefs/tests/conftest.py`` (and its ``market`` mirror) rebinds DRF view-class
attributes (``permission_classes`` / ``authentication_classes``) to ``AllowAny``
before each test so the dev settings stay permissive even after an ``auth_cf``
test flips DRF into strict ``IsAuthenticated`` mode.

The original implementation rebound the class attributes but never restored
them after ``yield``. Because the briefs API views inherit those attributes
from DRF's base classes (they do not declare their own), the fixture injected
a brand-new entry into each subclass ``__dict__`` that survived teardown,
permanently shadowing whatever the active ``REST_FRAMEWORK`` settings would
otherwise resolve to. That made test ordering load-bearing.

This test drives the fixture's full setup/teardown cycle in isolation, from a
known-clean class state, and pins that every view class is returned to its
exact pre-fixture ``__dict__`` state once the fixture has torn down.
"""

from __future__ import annotations

import pytest
from django.conf import settings as dj_settings

from briefs.api import views as briefs_views

_VIEW_CLASSES = [
    briefs_views.DayViewSet,
    briefs_views.ThemeViewSet,
    briefs_views.CandidateViewSet,
    briefs_views.TickerViewSet,
    briefs_views.StatsView,
]

_PERM_ATTRS = ("permission_classes", "authentication_classes")

# Captured at import time, before any autouse fixture has mutated the classes:
# which permission/auth attrs each view class genuinely declares in its own
# ``__dict__`` (none, for the briefs views — they inherit from DRF bases).
_ORIGINAL_OWNED = {
    cls: {attr: cls.__dict__[attr] for attr in _PERM_ATTRS if attr in cls.__dict__}
    for cls in _VIEW_CLASSES
}


def _reset_to_original():
    """Strip any injected attrs so the classes match their import-time state."""
    for cls, owned in _ORIGINAL_OWNED.items():
        for attr in _PERM_ATTRS:
            if attr in owned:
                setattr(cls, attr, owned[attr])
            elif attr in cls.__dict__:
                delattr(cls, attr)


def _owned_now():
    return {
        cls: {attr: cls.__dict__[attr] for attr in _PERM_ATTRS if attr in cls.__dict__}
        for cls in _VIEW_CLASSES
    }


@pytest.mark.django_db
def test_force_permissive_fixture_restores_view_classes(settings):
    """Running the fixture's full cycle leaves view classes byte-for-byte intact.

    Starts from the import-time class state, drives
    ``_force_permissive_dev_rest_framework`` past its ``yield`` (setup, which
    mutates the classes) and then through teardown, and asserts each class
    ``__dict__`` matches the pre-fixture snapshot. A fixture that forgets to
    restore leaves an injected ``permission_classes`` /
    ``authentication_classes`` entry behind and fails here.
    """
    from briefs.tests.conftest import _force_permissive_dev_rest_framework

    _reset_to_original()
    before = _owned_now()
    assert before == _ORIGINAL_OWNED  # precondition: clean start

    # ``settings`` is the pytest-django fixture value; forward it to the raw
    # generator behind the fixture wrapper.
    gen = _force_permissive_dev_rest_framework.__wrapped__(settings)
    next(gen)  # setup: mutates the view classes
    with pytest.raises(StopIteration):
        next(gen)  # teardown: must restore

    after = _owned_now()
    assert after == before, (
        "view classes were not restored to their pre-fixture state — the "
        "autouse fixture leaked permission/authentication class attributes"
    )

    _reset_to_original()  # keep the rest of the session clean


@pytest.mark.django_db
def test_dev_rest_framework_default_is_allow_any():
    """Sanity: the dev REST_FRAMEWORK default permission is AllowAny.

    Anchors the precondition the briefs/market fixtures rely on — if dev ever
    stops defaulting to AllowAny the whole leak-prevention story changes.
    """
    perms = dj_settings.REST_FRAMEWORK.get("DEFAULT_PERMISSION_CLASSES", [])
    assert "rest_framework.permissions.AllowAny" in perms
