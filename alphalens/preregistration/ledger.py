"""Strategy pre-registration ledger.

Persistent record of every strategy hypothesis tested in AlphaLens. Each
entry is a frozen claim — hypothesis, scorer path, parameters, periods,
success criteria — registered BEFORE the multi-phase audit runs, then
completed exactly once with the verdict and headline numbers.

Storage: a single ``ledger.json`` file under the ledger root (default
``docs/research/preregistration/``). The flat JSON is intentionally
git-trackable so the commit history doubles as an audit trail of the
pre-registration discipline.

Use ``count_in_class()`` / ``bonferroni_threshold()`` to derive the
t-stat threshold hypotheses in a signal class must clear given the
number already on record (Bonferroni at n=count, floored at n=1).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from alphalens.backtest.multiple_testing import bonferroni_critical_tstat

VALID_STATUSES = frozenset({"registered", "running", "completed", "abandoned"})
VALID_VERDICTS = frozenset({"PASS", "MID", "FAIL"})

LEDGER_FILENAME = "ledger.json"


@dataclass
class Registration:
    """A single pre-registered hypothesis.

    All fields except ``status`` and ``outcome`` are frozen at registration
    time and must not be edited afterwards (re-running with different
    params requires a new id).
    """

    id: str
    signal_class: str
    hypothesis: str
    scorer_path: str
    params_frozen: dict[str, Any]
    periods: dict[str, str]
    success_criteria: dict[str, Any]
    registered_at: date
    status: str = "registered"
    outcome: dict[str, Any] | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Registration.id must be non-empty.")
        if not self.signal_class:
            raise ValueError("Registration.signal_class must be non-empty.")
        if self.status not in VALID_STATUSES:
            raise ValueError(
                f"Registration.status must be one of {sorted(VALID_STATUSES)}, got {self.status!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["registered_at"] = self.registered_at.isoformat()
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Registration:
        data = dict(payload)
        data["registered_at"] = date.fromisoformat(data["registered_at"])
        return cls(**data)


@dataclass
class Ledger:
    """File-backed collection of Registration entries.

    Loads existing ``ledger.json`` from ``root`` on construction. Mutations
    write through immediately — no buffered state. Concurrent writers are
    not supported (the ledger is a solo research artifact, not a service).
    """

    root: Path
    _entries: dict[str, Registration] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self._load()

    @property
    def path(self) -> Path:
        return self.root / LEDGER_FILENAME

    def _load(self) -> None:
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        for entry in payload.get("entries", []):
            reg = Registration.from_dict(entry)
            self._entries[reg.id] = reg

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": [self._entries[k].to_dict() for k in sorted(self._entries)],
        }
        # Write to a sibling tmp path then atomic-rename so a Ctrl+C mid-write
        # never leaves a half-written ledger.json behind.
        tmp_path = self.path.with_suffix(".tmp.json")
        tmp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self.path)

    def add(self, reg: Registration) -> None:
        if reg.id in self._entries:
            raise ValueError(
                f"Registration id {reg.id!r} already exists. Choose a new id "
                "(re-running with different params requires a new pre-registration)."
            )
        self._entries[reg.id] = reg
        self._save()

    def get(self, id: str) -> Registration:
        if id not in self._entries:
            raise KeyError(f"No registration with id {id!r}.")
        return self._entries[id]

    def list(self, signal_class: str | None = None) -> list[Registration]:
        entries = [self._entries[k] for k in sorted(self._entries)]
        if signal_class is not None:
            entries = [r for r in entries if r.signal_class == signal_class]
        return entries

    def count_in_class(self, signal_class: str) -> int:
        return sum(1 for r in self._entries.values() if r.signal_class == signal_class)

    def bonferroni_threshold(self, signal_class: str, alpha: float = 0.05) -> float:
        """Required two-tailed |t| for hypotheses in ``signal_class`` as of now.

        Counts every entry currently in the class. The natural workflow is
        ``add(reg)`` first, then ``bonferroni_threshold(reg.signal_class)`` —
        the threshold then applies to the just-added hypothesis as the n-th
        test. Floors at n=1 so an empty class returns the unadjusted z=1.96
        rather than dividing by zero.
        """
        n = max(1, self.count_in_class(signal_class))
        return bonferroni_critical_tstat(n_tests=n, alpha=alpha)

    def complete(
        self,
        id: str,
        *,
        verdict: str,
        mean_alpha_t: float,
        mean_excess_net: float,
        audit_path: str,
        completed_at: date,
        notes: str = "",
    ) -> None:
        if verdict not in VALID_VERDICTS:
            raise ValueError(f"verdict must be one of {sorted(VALID_VERDICTS)}, got {verdict!r}")
        reg = self.get(id)
        if reg.status == "completed":
            raise ValueError(
                f"Registration {id!r} is already completed. Re-running with "
                "different params requires a new pre-registration id."
            )
        reg.status = "completed"
        reg.outcome = {
            "verdict": verdict,
            "mean_alpha_t": mean_alpha_t,
            "mean_excess_net": mean_excess_net,
            "audit_path": audit_path,
            "completed_at": completed_at.isoformat(),
            "notes": notes,
        }
        self._save()

    def abandon(self, id: str, reason: str) -> None:
        reg = self.get(id)
        if reg.status == "completed":
            raise ValueError(f"Registration {id!r} is already completed; cannot abandon.")
        reg.status = "abandoned"
        reg.notes = (reg.notes + "\n" if reg.notes else "") + f"abandoned: {reason}"
        self._save()
