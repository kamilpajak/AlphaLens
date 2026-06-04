"""Per-package conftest: pin AllowAny perms before every market test.

Mirrors ``briefs/tests/conftest.py``. The auth_cf test package flips DRF
into IsAuthenticated via ``override_settings``; some combination of DRF
``api_settings`` caching and pytest-django teardown leaves the strict
config active across the test-file boundary, which surfaces here as
spurious 401s on ``/v1/market/status``.

This autouse fixture re-applies the dev permission set on every test
entry, regardless of what the previous test left behind.
"""

from __future__ import annotations

import pytest
from django.test import override_settings


@pytest.fixture(autouse=True)
def _force_permissive_dev_rest_framework(settings):
    from rest_framework.settings import api_settings

    permissive = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    }
    _PERM_ATTRS = ("permission_classes", "authentication_classes")
    with override_settings(REST_FRAMEWORK=permissive):
        # IMPORTANT: import market.views INSIDE the fixture, not at module
        # level. DRF's ``APIView`` resolves ``permission_classes`` /
        # ``authentication_classes`` at class-definition time; a top-level
        # import would see the un-mutated classes (briefs-side mirror tests
        # have the same invariant). Flag surfaced by zen review 2026-05-30.
        from market import views as market_views

        view = market_views.MarketStatusView
        # Capture pre-mutation ownership so teardown restores it exactly.
        # ``MarketStatusView`` inherits these attrs from DRF's ``APIView`` and
        # does not declare its own, so the sentinel lets teardown delete the
        # injected entry rather than leave a permanent override on the class.
        _MISSING = object()
        original = {attr: view.__dict__.get(attr, _MISSING) for attr in _PERM_ATTRS}
        try:
            view.permission_classes = api_settings.DEFAULT_PERMISSION_CLASSES
            view.authentication_classes = api_settings.DEFAULT_AUTHENTICATION_CLASSES
            yield
        finally:
            for attr, value in original.items():
                if value is _MISSING:
                    if attr in view.__dict__:
                        delattr(view, attr)
                else:
                    setattr(view, attr, value)
