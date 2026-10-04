"""One way for a stage to check its frame against its own column declaration.

Every stage that publishes a column tuple faces the same question at runtime:
are the names I just wrote the names I say I write? Three stages ask it
(``score_candidates``, ``merge_event_candidates`` and
``_sort_and_dedup_for_brief``), so the question is asked once here instead of
three copies of the same format string drifting apart.

THE DECLARATION IS CHECKED AGAINST THE FRAME, NEVER IMPOSED ON IT
-----------------------------------------------------------------
:func:`warn_on_column_disagreement` only reads and reports. It deliberately
does not reindex, reorder or fill the frame to match the declaration. A stage
that projected its output through its own tuple would re-invent a column the
writer stopped producing as an all-null column under the declared name: the
store would keep its declared width while the data behind one field quietly
went empty, and the gate comparing the frame against the tuple would stay
green. Reporting instead of repairing keeps a broken writer visible both in
the operator's log and in the data itself.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

#: One message for all three call sites: writer, declaration name, then the
#: two lists an operator needs to act — what the tuple names and the frame
#: lacks, and what the frame carries and the tuple does not.
COLUMN_DISAGREEMENT_WARNING = "%s columns disagree with %s: missing=%s extra=%s"


def warn_on_column_disagreement(
    log: logging.Logger,
    *,
    writer: str,
    declaration: str,
    produced: Iterable[str],
    declared: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Log one warning when ``produced`` is not exactly ``declared``.

    ``produced`` is the set of names the stage actually wrote, in frame order;
    ``declared`` is the tuple it publishes. Both difference lists keep their
    source order so two runs over the same frame log the same line.

    ``produced`` is MATERIALISED on the first line because it is consumed
    twice below — once for the membership set, once for the ordered ``extra``
    list. A generator argument would be exhausted by the first pass and the
    warning would report a drift as half a drift, naming the missing columns
    with an empty ``extra`` list. Narrowing the annotation to ``Sequence[str]``
    was the alternative and was rejected: it documents the hazard without
    removing it, and a caller computing ``produced`` as a generator expression
    is a natural shape for this call. The copy is one list of column names.

    Returns the ``(missing, extra)`` pair so a caller can assert on it.
    """
    produced_columns = list(produced)
    declared_set = set(declared)
    produced_set = set(produced_columns)
    missing = [column for column in declared if column not in produced_set]
    extra = [column for column in produced_columns if column not in declared_set]
    if missing or extra:
        log.warning(COLUMN_DISAGREEMENT_WARNING, writer, declaration, missing, extra)
    return missing, extra


__all__ = ["COLUMN_DISAGREEMENT_WARNING", "warn_on_column_disagreement"]
