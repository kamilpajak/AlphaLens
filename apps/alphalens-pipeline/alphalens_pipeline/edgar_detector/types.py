from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class FormType(Enum):
    FORM_8K = "8-K"
    FORM_4 = "4"
    FORM_13D = "SC 13D"
    FORM_13G = "SC 13G"
    FORM_13D_A = "SC 13D/A"
    FORM_13G_A = "SC 13G/A"

    @classmethod
    def from_sec_string(cls, sec_form: str) -> FormType | None:
        """Map an EDGAR form-type string to a member; ``None`` when unknown.

        Exact match only (no prefix / case folding). Accepts both the legacy
        beneficial-ownership spellings (``SC 13D``) and the names EDGAR emits
        since the SEC's Dec-2024 structured-data rule (``SCHEDULE 13D``) —
        see :data:`_SEC_FORM_ALIASES`.
        """
        canonical = _SEC_FORM_ALIASES.get(sec_form, sec_form)
        for member in cls:
            if member.value == canonical:
                return member
        return None


# EDGAR renamed Schedule 13D / 13G filings when the beneficial-ownership
# reports moved to structured XML (SEC Release 33-11253; the new spelling is
# the only one present in the full index from 2025 Q1). The detector must
# accept both spellings or it silently drops every activist filing (#1263).
_SEC_FORM_ALIASES: dict[str, str] = {
    "SCHEDULE 13D": "SC 13D",
    "SCHEDULE 13D/A": "SC 13D/A",
    "SCHEDULE 13G": "SC 13G",
    "SCHEDULE 13G/A": "SC 13G/A",
}


@dataclass(eq=False)
class Event:
    ticker: str
    form_type: FormType
    accession_number: str
    filed_at: datetime
    url: str
    raw_data: dict[str, Any] = field(default_factory=dict)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Event):
            return NotImplemented
        return self.accession_number == other.accession_number

    def __hash__(self) -> int:
        return hash(self.accession_number)
