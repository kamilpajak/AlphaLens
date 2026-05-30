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
    with override_settings(REST_FRAMEWORK=permissive):
        from market import views as market_views

        market_views.MarketStatusView.permission_classes = api_settings.DEFAULT_PERMISSION_CLASSES
        market_views.MarketStatusView.authentication_classes = (
            api_settings.DEFAULT_AUTHENTICATION_CLASSES
        )
        yield
