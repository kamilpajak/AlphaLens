"""Per-package conftest: force the dev DRF settings before every briefs test.

The auth_cf test package flips ``REST_FRAMEWORK`` into a strict
IsAuthenticated configuration via ``override_settings`` context managers.
Some combination of DRF's ``api_settings`` caching and pytest-django's
fixture teardown leaves stale permission-class resolution active across
the test-file boundary, which then causes /v1/* requests in this package
to 401 even though dev settings declare ``AllowAny``.

This autouse fixture is defensive: it re-applies the dev permission set
on every test entry, regardless of what the previous test left behind.
"""

from __future__ import annotations

import pytest
from django.test import override_settings


@pytest.fixture(autouse=True)
def _force_permissive_dev_rest_framework(settings):
    """Re-assert AllowAny perms by mutating both DRF cache and view classes.

    Why not "just override_settings"?

    DRF reads ``permission_classes`` and ``authentication_classes`` at
    view-class definition time. The legacy approach of changing settings
    mid-run does invalidate ``api_settings._cached_attrs`` via the
    ``setting_changed`` signal, but views that were instantiated against
    a different settings snapshot (e.g. an auth_cf test that flipped to
    IsAuthenticated) retain the resolved classes on the class itself.

    pytest-django's interaction with override_settings makes the mutation
    survive into the next test module — empirically observed by printing
    ``StatsView.permission_classes`` at the start of the failing test.

    Defensive fix: walk every DRF view in the briefs API module and rebind
    its class attrs to whatever ``api_settings`` currently resolves to.
    """
    from rest_framework.settings import api_settings

    permissive = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    }
    with override_settings(REST_FRAMEWORK=permissive):
        from briefs.api import views as briefs_views

        view_classes = [
            briefs_views.DayViewSet,
            briefs_views.ThemeViewSet,
            briefs_views.CandidateViewSet,
            briefs_views.TickerViewSet,
            briefs_views.StatsView,
        ]
        for cls in view_classes:
            cls.permission_classes = api_settings.DEFAULT_PERMISSION_CLASSES
            cls.authentication_classes = api_settings.DEFAULT_AUTHENTICATION_CLASSES
        yield
