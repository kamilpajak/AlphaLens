from typing import Literal

from .composite_ranker import CompositeRanker
from .config import PRESCREENER_DEFAULTS
from .data_fetcher import BatchDataFetcher
from .fundamental_scorer import FundamentalScorer
from .integration import PrescreenerPipeline
from .technical_scorer import TechnicalScorer
from .universe import SP500_FALLBACK, get_sp500_tickers
from .volume_scorer import VolumeScorer

__all__ = [
    "PRESCREENER_DEFAULTS",
    "SP500_FALLBACK",
    "BatchDataFetcher",
    "CompositeRanker",
    "FundamentalScorer",
    "PrescreenerPipeline",
    "TechnicalScorer",
    "VolumeScorer",
    "get_sp500_tickers",
]

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "RESEARCH_ONLY"
__closed_reason__ = (
    "Layer 2a unvalidated — 45% fundamentals weight needs PIT historicals "
    "Polygon Starter does not provide. Manual ad-hoc only, no performance guarantee."
)
