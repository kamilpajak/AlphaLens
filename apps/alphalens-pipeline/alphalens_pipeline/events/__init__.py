"""Event-sourced candidate lane (epic #1293).

A second candidate SOURCE beside the thematic pipeline: structured-filing
triggers (today: insider purchase clusters from the Form-4 store) detected
daily, written to ``~/.alphalens/event_candidates/<date>.parquet`` and merged
into the day's candidates by ``alphalens thematic score`` ONLY when the
``ALPHALENS_EVENT_LANE=1`` flag is set. The thematic lane is untouched.

Pre-registration (frozen constants, exclusions, outcome, floor, verdict):
``docs/research/insider_cluster_forward_prereg_2026_09.md`` and
``docs/research/preregistration/params_insider_cluster_forward_2026_09.json``.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"

# The accrual switch: `thematic score` merges event rows only when this is "1".
EVENT_LANE_ENV = "ALPHALENS_EVENT_LANE"
DEFAULT_EVENT_CANDIDATES_DIR = Path.home() / ".alphalens" / "event_candidates"
DEFAULT_ACCEPTANCE_CACHE_DIR = Path.home() / ".alphalens" / "edgar_acceptance"


def event_lane_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True only for the literal ``"1"`` (any other value keeps the lane OFF).

    The single definition of "the lane is on" — the day script, ``thematic score``
    and the merge all read it from here.
    """
    env = os.environ if environ is None else environ
    return env.get(EVENT_LANE_ENV) == "1"
