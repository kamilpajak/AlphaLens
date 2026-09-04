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

from pathlib import Path
from typing import Literal

__status__: Literal["ACTIVE", "CLOSED", "RESEARCH_ONLY", "ARCHIVED"] = "ACTIVE"

DEFAULT_EVENT_CANDIDATES_DIR = Path.home() / ".alphalens" / "event_candidates"
DEFAULT_ACCEPTANCE_CACHE_DIR = Path.home() / ".alphalens" / "edgar_acceptance"
