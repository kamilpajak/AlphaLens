"""v7 options-implied screener — Phase A feature joiner.

Pre-registered as `v7_smd_options_implied_2026_05_02`.
- Phase A: feature joiner + sanity gates (`features.py`).
- Phase B: global Lasso fit + holdout reveal (TBD: `model.py`, `target.py`,
  `scripts/experiment_v7_options_implied.py`).
- Phase C: multi-phase audit via `alphalens audit v7_options_implied`.
"""

from typing import Literal

from alphalens_research.screeners.options_implied.cross_sectional_residual import (
    score_cross_sectional_residual,
)
from alphalens_research.screeners.options_implied.features import (
    EQUITY_CONTROLS,
    ETL_ANOMALY_BOUNDS,
    FEATURE_NAMES,
    OPTIONS_FEATURES,
    US_PRIMARY_EXCHANGES,
    build_feature_frame,
    multicollinearity_drop_recommendation,
    validate_phase_a_gates,
)
from alphalens_research.screeners.options_implied.literature_direct import score_literature_direct
from alphalens_research.screeners.options_implied.model import (
    LAMBDA_GRID_POINTS_DEFAULT,
    N_FOLDS_DEFAULT,
    GlobalLassoFit,
    fit_global_lasso,
    lasso_sign_alignment,
    predict_scores,
)
from alphalens_research.screeners.options_implied.sign_constrained import fit_sign_constrained_lasso
from alphalens_research.screeners.options_implied.target import (
    DEFAULT_HOLDING,
    DelistingEventsIndex,
    aligned_train,
    build_target_frame,
    forward_raw_return,
    load_delisting_events_index,
    split_train_holdout,
)

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"

__all__ = [
    "DEFAULT_HOLDING",
    "EQUITY_CONTROLS",
    "ETL_ANOMALY_BOUNDS",
    "FEATURE_NAMES",
    "LAMBDA_GRID_POINTS_DEFAULT",
    "N_FOLDS_DEFAULT",
    "OPTIONS_FEATURES",
    "US_PRIMARY_EXCHANGES",
    "DelistingEventsIndex",
    "GlobalLassoFit",
    "aligned_train",
    "build_feature_frame",
    "build_target_frame",
    "fit_global_lasso",
    "fit_sign_constrained_lasso",
    "forward_raw_return",
    "lasso_sign_alignment",
    "load_delisting_events_index",
    "multicollinearity_drop_recommendation",
    "predict_scores",
    "score_cross_sectional_residual",
    "score_literature_direct",
    "split_train_holdout",
    "validate_phase_a_gates",
]
