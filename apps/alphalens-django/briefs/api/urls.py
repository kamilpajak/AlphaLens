"""URL config for the ``/v1/*`` API.

DRF ``DefaultRouter(trailing_slash=False)`` handles the three viewsets whose
PK is a single URL segment (days, themes, tickers). The candidates endpoint
takes a compound ``{date}/{ticker}`` lookup that the standard router can't
express, so we wire it explicitly. ``/v1/stats`` is an APIView, also wired
manually.
"""

from __future__ import annotations

from django.urls import path
from rest_framework.routers import DefaultRouter

from briefs.api.views import (
    CandidateViewSet,
    DayViewSet,
    StatsView,
    ThemeViewSet,
    TickerViewSet,
)

router = DefaultRouter(trailing_slash=False)
router.register(r"v1/days", DayViewSet, basename="day")
router.register(r"v1/themes", ThemeViewSet, basename="theme")
router.register(r"v1/tickers", TickerViewSet, basename="ticker")

urlpatterns = [
    *router.urls,
    path(
        "v1/candidates/<str:date>/<str:ticker>",
        CandidateViewSet.as_view({"get": "retrieve_compound"}),
        name="candidate-detail",
    ),
    path("v1/stats", StatsView.as_view(), name="stats"),
]
