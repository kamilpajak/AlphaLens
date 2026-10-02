"""Read-only architecture-audit measurements over this repository.

Every number the architecture audit publishes is produced here, so the report
can name the command that computed it and the owner can re-run it after the
code moves. Nothing in this package writes to the repository or to
``~/.alphalens``.

``graph`` holds the import graph. It deliberately keeps three distinctions the
audit's numbers depend on: runtime versus ``TYPE_CHECKING``-only imports,
top-level versus function-scope imports, and observed edges versus the implicit
parent-package edges that are a modelling choice rather than an observation.
"""

from __future__ import annotations

__all__ = ["graph"]
