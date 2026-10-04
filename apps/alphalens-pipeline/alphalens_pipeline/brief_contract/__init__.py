"""The column contract of the brief store, assembled from its writers.

One job: say which columns a brief parquet under ``thematic_briefs/`` carries,
and say it by importing the declarations of the stages that compute those
columns rather than by keeping a second hand-written list beside them.

No column tuple is re-exported here on purpose. Reading the contract means
importing :mod:`alphalens_pipeline.brief_contract.columns`, which eagerly
imports twelve modules across four subpackages (``events``, ``experts``,
``market``, ``thematic``) and through them 111 ``alphalens_pipeline`` modules
in all. That costs a few tenths of a second of wall time: 0.43 s median over
eleven fresh interpreters on the dev laptop, measured 2026-10-03, against
0.01 s for a bare interpreter. An earlier measurement the same week read
0.59-0.67 s on the same tree, so the figure is machine- and cache-dependent
and is quoted as an order of magnitude, not a constant. Putting that import
behind ``import alphalens_pipeline.brief_contract`` would charge it to every
CLI invocation, including the EDGAR detector that fires every fifteen
minutes; :mod:`alphalens_pipeline.brief_contract.validation` is kept free of
it for the same reason.
"""

from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"
