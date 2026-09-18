"""The brief list that existed at the arrival open, on the dates the stored one does not show.

Until #1482 the thematic build rewrote a date's brief up to six times a day, including
after the arrival open, so on the dates in ``publication.PUBLISHED_AFTER_OPEN_HISTORY``
the stored list is not the one a reader could have acted on. #1494 recovered the names
from the build journal; the record, the method and its limits are in
``docs/research/pre_open_brief_recovery_2026_09_18.md`` and the CSV beside it.

Only the dates the journal answered EXACTLY are here: 14 dates, 155 names, in the order
the run printed them. Three affected dates are deliberately absent — 2026-05-28 recovered
only 17 of its 18 names (the score-stage printer stops at 25 rows), and 2026-06-09 and
2026-05-19 had no brief at all before the open. A consumer must treat a date that answers
``None`` as it did before: the stored list, with its publication status unchanged.

The names live in code rather than in a data file because the pipeline must not read
``docs/``, and the list is frozen — a research test keeps it equal to the committed CSV.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from types import MappingProxyType

PRE_OPEN_BRIEF_NAMES: Mapping[dt.date, tuple[str, ...]] = MappingProxyType(
    {
        dt.date(2026, 5, 31): (
            "SONO",
            "DLB",
            "BAH",
            "MRCY",
            "ETSY",
            "CHWY",
            "W",
            "PKE",
            "OSIS",
            "PSN",
            "LUNR",
            "RDW",
            "SPCE",
            "OWL",
            "TPG",
            "STEP",
        ),
        dt.date(2026, 6, 1): (
            "S",
            "SONO",
            "CRSR",
            "DLB",
            "PSN",
            "MRCY",
            "ETSY",
            "HXL",
            "OWL",
            "TPG",
        ),
        dt.date(2026, 6, 2): (
            "S",
            "PATH",
            "AI",
            "PEGA",
            "SONO",
            "CRUS",
            "QRVO",
            "PSN",
            "KBR",
            "BAH",
            "CHWY",
            "ETSY",
            "W",
            "MAN",
            "RHI",
            "NSP",
            "MRCY",
        ),
        dt.date(2026, 6, 3): (
            "VRNS",
            "PATH",
            "S",
            "APPF",
            "SONO",
            "CRSR",
            "AVAV",
            "BAH",
            "CHWY",
            "ETSY",
            "W",
            "FDS",
            "RHI",
            "NSP",
        ),
        dt.date(2026, 6, 4): (
            "CRL",
            "FDS",
            "GME",
            "IRDM",
        ),
        dt.date(2026, 6, 7): (
            "CTKB",
            "GPRE",
            "ESTC",
            "DFIN",
            "MC",
            "PJT",
            "MORN",
            "HLI",
            "VIRT",
            "AVAV",
            "KBR",
            "VCYT",
        ),
        dt.date(2026, 6, 8): (
            "MORN",
            "BFH",
            "NSP",
            "KFY",
            "KBR",
            "AVAV",
            "OSK",
            "DFIN",
            "MC",
            "CRUS",
        ),
        dt.date(2026, 6, 10): (
            "ESTC",
            "FCN",
            "HLI",
            "LAZ",
            "CRUS",
        ),
        dt.date(2026, 6, 11): (
            "KFY",
            "CRL",
            "RGEN",
            "FCN",
            "HLI",
            "PJT",
            "DFIN",
            "RDW",
            "NVAX",
            "PCVX",
            "SONO",
        ),
        dt.date(2026, 6, 14): (
            "WK",
            "BL",
            "FDS",
            "CORZ",
            "LAC",
            "CMP",
            "MTRN",
            "VKTX",
            "ALT",
            "HIMS",
            "CNS",
            "LNC",
            "DFIN",
            "RDW",
            "VSAT",
            "IRDM",
        ),
        dt.date(2026, 6, 15): (
            "PSN",
            "FDS",
            "QRVO",
            "TTD",
            "MGNI",
            "DV",
            "RYTM",
            "VKTX",
            "HIMS",
            "RITM",
            "AMC",
            "MARA",
            "OPEN",
            "DFIN",
            "COMP",
            "SNAP",
        ),
        dt.date(2026, 8, 2): (
            "ESTC",
            "WT",
            "MORN",
            "MTCH",
            "LYFT",
            "SNAP",
            "ACLS",
            "UCTT",
            "KTOS",
            "MRCY",
        ),
        dt.date(2026, 8, 18): (
            "TTD",
            "ETSY",
        ),
        dt.date(2026, 8, 19): (
            "IRDM",
            "RDW",
            "PL",
            "SMG",
            "IIPR",
            "MXL",
            "AMBA",
            "GO",
            "OLLI",
            "MARA",
            "RIOT",
            "HIVE",
        ),
    }
)


def pre_open_names(brief_date: dt.date) -> tuple[str, ...] | None:
    """The names the brief for ``brief_date`` held at the arrival open, or ``None``."""
    return PRE_OPEN_BRIEF_NAMES.get(brief_date)


__all__ = ["PRE_OPEN_BRIEF_NAMES", "pre_open_names"]
