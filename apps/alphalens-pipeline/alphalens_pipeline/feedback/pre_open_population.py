"""The brief population the population-ladder store measures, on the dates it must not trust.

Until #1482 the thematic build rewrote a date's brief up to six times a day, including after
the arrival open. On the dates in ``publication.PUBLISHED_AFTER_OPEN_HISTORY`` the stored
list is therefore not the one a reader could have acted on, and ``/edge`` measures the stored
one. #1494 recovered the list that existed at the open for 14 of those dates; #1496 applied
it to the ML label store; this seam applies it to ``/edge``.

Every pass that loads a brief AND writes into ``~/.alphalens/population_ladders`` calls this
instead of ``load_brief``. That is not a detail: ``_replay_one_date`` rebuilds a date's
parquet wholesale from the candidate list it is given, so a pass left on the raw loader would
write the stored population straight back over the recovered one.

What the swap does on a recovered date:

* the population is exactly the recovered names, in the order the run printed them;
* a name the stored brief still carries is passed through untouched — its theme, its score,
  its scorer config and its own frozen setup;
* a name the later run dropped is rebuilt from the committed record in
  ``thematic.pre_open_setup`` and carries no theme and no scorer config, because the journal
  recorded names, not themes, and not which configuration ranked them;
* a name the stored brief added after the open is absent.

A date the recovery could not answer is passed through unchanged, which keeps the three
unanswered dates (2026-05-28, 2026-06-09, 2026-05-19) exactly as they are today.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from alphalens_pipeline.paper.brief_loader import CandidateBrief, load_brief
from alphalens_pipeline.thematic.pre_open_brief import pre_open_names
from alphalens_pipeline.thematic.pre_open_setup import PRE_OPEN_SETUPS, pre_open_setup

logger = logging.getLogger(__name__)

# The candidate lane, not a provenance marker. A rebuilt name was a thematic candidate like
# every other name on the card, and the event-lane cohorts filter on this field.
LANE_THEMATIC = "thematic"

SetupRecord = Mapping[dt.date, Mapping[str, Mapping[str, Any]]]


def _rebuilt_candidate(
    brief_date: dt.date, ticker: str, setup: dict[str, Any] | None
) -> CandidateBrief:
    """A candidate for a recovered name the stored brief no longer carries.

    ``verified`` is True because the name WAS on the published list at the open; that is the
    whole claim the recovery makes. ``trade_setup`` may be ``None`` when the record cannot
    serve the name — the ladder then takes the non-plannable path it already gives a
    setup-less candidate, which is what the recompute was pre-registered to do.
    """
    return CandidateBrief(
        brief_date=brief_date,
        ticker=ticker,
        theme="",
        verified=True,
        suggested_size_pct=None if setup is None else setup.get("suggested_size_pct"),
        trade_setup=setup,
        layer4_weighted_score=None,
        scorer_config_version="",
        technical_pct_off_52w_high=None,
        source=LANE_THEMATIC,
        event_overlap=False,
        brief_published_at=None,
    )


def load_brief_for_population(
    brief_date: dt.date,
    briefs_dir: Path,
    *,
    setups: SetupRecord | None = None,
) -> list[CandidateBrief]:
    """``load_brief``, with the recovered pre-open population on the dates that have one.

    ``setups`` overrides the committed record; it exists so a test can ask what happens to a
    recovered name the record cannot serve.
    """
    candidates = load_brief(brief_date, briefs_dir)
    recovered = pre_open_names(brief_date)
    if recovered is None:
        return candidates

    stored = {c.ticker.upper(): c for c in candidates}
    record = PRE_OPEN_SETUPS if setups is None else setups
    rebuilt = 0
    out: list[CandidateBrief] = []
    for ticker in recovered:
        kept = stored.get(ticker.upper())
        if kept is not None:
            out.append(kept)
            continue
        if setups is None:
            setup = pre_open_setup(brief_date, ticker)
        else:
            found = record.get(brief_date, {}).get(ticker.upper())
            setup = None if found is None else dict(found)
        out.append(_rebuilt_candidate(brief_date, ticker.upper(), setup))
        rebuilt += 1

    logger.info(
        "pre-open population %s: %d recovered names, %d rebuilt, %d of the stored %d dropped.",
        brief_date.isoformat(),
        len(out),
        rebuilt,
        len(stored) - (len(out) - rebuilt),
        len(stored),
    )
    return out


__all__ = ["load_brief_for_population"]
