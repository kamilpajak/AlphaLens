"""Per-package conftest: force permissive dev DRF perms for every edge test.

Mirrors ``briefs/tests/conftest.py`` — the auth_cf suite flips REST_FRAMEWORK
into strict IsAuthenticated via override_settings, and DRF's view-class-level
permission caching can leak that across the test-file boundary, 401-ing the
``/v1/edge/*`` requests in this package. This autouse fixture re-asserts
AllowAny on the edge views on every test entry.
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
        from edge.api import views as edge_views

        view_classes = [edge_views.EdgeSummaryView, edge_views.EdgeOutcomesView]
        _MISSING = object()
        original = {
            cls: {attr: cls.__dict__.get(attr, _MISSING) for attr in _PERM_ATTRS}
            for cls in view_classes
        }
        try:
            for cls in view_classes:
                cls.permission_classes = api_settings.DEFAULT_PERMISSION_CLASSES
                cls.authentication_classes = api_settings.DEFAULT_AUTHENTICATION_CLASSES
            yield
        finally:
            for cls, owned in original.items():
                for attr, value in owned.items():
                    if value is _MISSING:
                        if attr in cls.__dict__:
                            delattr(cls, attr)
                    else:
                        setattr(cls, attr, value)
