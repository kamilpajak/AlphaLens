"""The one way an engine module builds a refusal.

Every engine refusal is a :class:`~broker_contract.failure.ContractError`
carrying a ``Failure`` whose ``code`` names the failure mode and whose
``details["reason"]``, when present, names the rule — the project's rule of one
code per failure mode with a closed reason vocabulary. ``bars.py`` wrote that
once; this module is its second use, so the rule lives here and each module
keeps only its codes, its vocabulary and its error class.

A reason is checked only when it IS present: ``bars_empty`` ships with none,
and ``path_unclassified`` carries no vocabulary at all. A present reason
outside the vocabulary, or a present reason with no vocabulary to check it
against, is a programming error surfaced as ``ValueError`` — never a published
refusal.

ENGINE module: stdlib and ``broker_contract`` only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from broker_contract.failure import ContractError, Failure

__all__ = ["refuse"]


def refuse[E: ContractError](
    error: type[E],
    code: str,
    message: str,
    *,
    reasons: Mapping[str, str] | None = None,
    **details: Any,
) -> E:
    """Build ``error`` around a never-retryable ``Failure``.

    ``reasons`` is the closed vocabulary of the code; it is consulted only when
    ``details`` carries a ``reason``.
    """
    reason = details.get("reason")
    if reason is not None and (reasons is None or reason not in reasons):
        raise ValueError(f"unregistered {code} reason: {reason!r}")
    return error(Failure(code=code, message=message, retryable=False, details=details))
