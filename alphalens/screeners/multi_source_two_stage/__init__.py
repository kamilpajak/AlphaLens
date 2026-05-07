"""Multi-source two-stage regime-conditional screener (Exp 1).

Pre-registered as `multi_source_two_stage_2026_04_30` per
`docs/research/preregistration/params_multi_source_two_stage_2026_04_30.json`.

- Phase A: feature joiner + sanity validation (`features.py`).
- Phase B: per-regime Lasso with nested expanding-window CV (`model.py`),
  5d-forward excess-return target (`target.py`),
  experiment script (`scripts/experiment_multi_source_two_stage.py`).
- Phase C: multi-phase audit via `alphalens audit multi_source_two_stage`.
"""

from typing import Literal

from alphalens.screeners.multi_source_two_stage.features import (
    FEATURE_NAMES,
    REGIME_LABELS,
    assign_regime,
    build_feature_frame,
    train_quartile_thresholds,
)
from alphalens.screeners.multi_source_two_stage.model import (
    EMBARGO_DAYS_DEFAULT,
    GLOBAL_REGIME_LABEL,
    LAMBDA_GRID_POINTS_DEFAULT,
    N_FOLDS_DEFAULT,
    LightGBMFit,
    RegimeFit,
    fit_global,
    fit_lightgbm_mse_global,
    fit_two_stage,
    predict_scores,
    predict_scores_global,
    predict_scores_lightgbm,
)
from alphalens.screeners.multi_source_two_stage.target import (
    DEFAULT_HOLDING,
    aligned_train_targets,
    build_target_frame,
    forward_excess_return,
    split_train_holdout,
)

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"
__all__ = [
    "DEFAULT_HOLDING",
    "EMBARGO_DAYS_DEFAULT",
    "FEATURE_NAMES",
    "GLOBAL_REGIME_LABEL",
    "LAMBDA_GRID_POINTS_DEFAULT",
    "N_FOLDS_DEFAULT",
    "REGIME_LABELS",
    "LightGBMFit",
    "RegimeFit",
    "aligned_train_targets",
    "assign_regime",
    "build_feature_frame",
    "build_target_frame",
    "fit_global",
    "fit_lightgbm_mse_global",
    "fit_two_stage",
    "forward_excess_return",
    "predict_scores",
    "predict_scores_global",
    "predict_scores_lightgbm",
    "split_train_holdout",
    "train_quartile_thresholds",
]
