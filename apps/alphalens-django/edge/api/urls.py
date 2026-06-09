"""URL config for the ``/v1/edge/*`` API (market-behavior edge dashboard).

Two flat APIView endpoints (no router): the summary aggregate and the
per-candidate outcomes listing. ``/v1/edge/*`` is lexically distinct from
``/v1/feedback/*`` (the user-action ledger) by design — they are different
surfaces (memo §4).
"""

from __future__ import annotations

from django.urls import path

from edge.api.chart import EdgeChartView
from edge.api.views import EdgeOutcomesView, EdgeSummaryView

urlpatterns = [
    path("v1/edge/summary", EdgeSummaryView.as_view(), name="edge-summary"),
    path("v1/edge/outcomes", EdgeOutcomesView.as_view(), name="edge-outcomes"),
    path(
        "v1/edge/chart/<str:brief_date>/<str:ticker>",
        EdgeChartView.as_view(),
        name="edge-chart",
    ),
]
