"""Options-volume screeners — Pan-Poteshman 2006-inspired proxy signals.

Pre-registered as ``pc_abnormal_volume_retrospective_pre_2018_2026_05_05`` in
signal class ``options_volume_search_2026_05_05`` (see
``docs/research/preregistration/params_pc_abnormal_volume_retrospective_pre_2018_2026_05_05.json``).

Distinct feature space from ``alphalens.screeners.options_implied`` (which uses
IV-level features ivx30/ivp30/rv_30d). This screener uses options-VOLUME features
(optVolPut/optVolCall) cross-sectionally residualized against equity controls.
"""

from typing import Literal

from alphalens.screeners.options_volume.features import (
    FEATURE_COLUMNS,
    build_feature_frame,
)
from alphalens.screeners.options_volume.pc_abnormal_volume import (
    EQUITY_CONTROLS_FOR_RESIDUAL,
    MIN_ASOF_TICKERS,
    MIN_ROLLING_OBS,
    ROLLING_WINDOW_DAYS,
    compute_abnormal_pcr_series,
    compute_pcr,
    score_pc_abnormal_residual,
)

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"

__all__ = [
    "EQUITY_CONTROLS_FOR_RESIDUAL",
    "FEATURE_COLUMNS",
    "MIN_ASOF_TICKERS",
    "MIN_ROLLING_OBS",
    "ROLLING_WINDOW_DAYS",
    "build_feature_frame",
    "compute_abnormal_pcr_series",
    "compute_pcr",
    "score_pc_abnormal_residual",
]
