"""Regression: the market autouse permission fixture must not leak state.

Mirrors ``briefs/tests/test_conftest_isolation.py``. ``market/tests/conftest.py``
rebinds ``MarketStatusView.permission_classes`` / ``authentication_classes`` to
``AllowAny`` before each test; the original implementation never restored them
after ``yield``, permanently injecting an attribute the view never declared
(it inherits from DRF's ``APIView``). This pins that the fixture restores the
class to its import-time state on teardown.
"""

from __future__ import annotations

import pytest

from market import views as market_views

_VIEW_CLASS = market_views.MarketStatusView
_PERM_ATTRS = ("permission_classes", "authentication_classes")

# Captured at import time, before any autouse fixture mutates the class.
_ORIGINAL_OWNED = {
    attr: _VIEW_CLASS.__dict__[attr] for attr in _PERM_ATTRS if attr in _VIEW_CLASS.__dict__
}


def _reset_to_original():
    for attr in _PERM_ATTRS:
        if attr in _ORIGINAL_OWNED:
            setattr(_VIEW_CLASS, attr, _ORIGINAL_OWNED[attr])
        elif attr in _VIEW_CLASS.__dict__:
            delattr(_VIEW_CLASS, attr)


def _owned_now():
    return {
        attr: _VIEW_CLASS.__dict__[attr] for attr in _PERM_ATTRS if attr in _VIEW_CLASS.__dict__
    }


@pytest.mark.django_db
def test_force_permissive_fixture_restores_market_view(settings):
    """The fixture's full cycle leaves ``MarketStatusView`` byte-for-byte intact."""
    from market.tests.conftest import _force_permissive_dev_rest_framework

    _reset_to_original()
    before = _owned_now()
    assert before == _ORIGINAL_OWNED  # precondition: clean start

    gen = _force_permissive_dev_rest_framework.__wrapped__(settings)
    next(gen)  # setup: mutates the view class
    with pytest.raises(StopIteration):
        next(gen)  # teardown: must restore

    after = _owned_now()
    assert after == before, (
        "MarketStatusView was not restored to its pre-fixture state — the "
        "autouse fixture leaked permission/authentication class attributes"
    )

    _reset_to_original()
