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
        for member in cls:
            if member.value == sec_form:
                return member
        return None


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
