"""Atomic Prometheus textfile writer for the Django maintenance commands.

The ADR 0011 mirror of ``alphalens_pipeline.observability.textfile`` — the slim
Django image installs only ``alphalens-django``, so the pipeline's writer cannot
be imported, and the mirror command (``rebuild_ladder_outcomes_cache``) is the
one process that holds the facts the /edge staleness rules need (#1436): the
ingest watermark it read, how many dates it refused, and the newest brief date
Postgres holds after its own writes. Same file name (``alphalens_domain_<job>.prom``),
same line format (``<expr> <value>``), same tmp-in-the-same-dir + ``os.replace``
so node_exporter only ever sees a whole file, same ``0o644`` (the collector reads
as ``nobody``).

Three deliberate differences from the pipeline module:

- **No home-directory fallback.** ``ALPHALENS_TEXTFILE_DIR`` unset → one WARNING and
  nothing written. Inside the container ``Path.home()`` is ``/home/django`` (no such
  directory), and a default that "succeeds" into an unscraped path is the #377 /
  #1366 failure: emit works, Prometheus never sees the series.
- **No ``mkdir``.** A missing directory inside the container means the compose bind
  mount is missing; creating it would land the file on the container's own disk.
  ``OSError`` propagates instead.
- **An unwritable directory raises.** The command runs the emit AFTER the ingest, so
  a metrics failure never loses data; letting it fail the run makes the broken
  channel visible (``AlphalensJobFailed`` on the unit) rather than silent.

If a second Django command ever needs this, the shared home is
``apps/alphalens-feedback`` (primitives consumed by both the pipeline and the
Django API) — extract on second use, not before.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

# Same variable the bash hook and the pipeline writer honour; prod sets it to the
# directory node_exporter scrapes (/var/lib/node_exporter/textfile).
ENV_VAR = "ALPHALENS_TEXTFILE_DIR"


def resolve_dir() -> Path | None:
    """The textfile directory named by the environment, or None when unset."""
    value = os.environ.get(ENV_VAR)
    return Path(value) if value else None


def emit_domain_metrics(job: str, metrics: Mapping[str, float | int]) -> Path | None:
    """Atomically write ``alphalens_domain_<job>.prom`` and return its path.

    ``metrics`` maps a full exposition-format expression (name plus optional
    ``{labels}``) to a number; the caller formats labels, this module knows
    nothing about escaping. Returns ``None`` (after a warning) when
    ``ALPHALENS_TEXTFILE_DIR`` is unset. Raises ``OSError`` when the directory
    does not exist or cannot be written.
    """
    out_dir = resolve_dir()
    if out_dir is None:
        logger.warning("%s: %s unset - metrics not written (#1436)", job, ENV_VAR)
        return None

    target = out_dir / f"alphalens_domain_{job}.prom"
    with tempfile.NamedTemporaryFile(
        mode="w", dir=out_dir, delete=False, suffix=".tmp", encoding="utf-8"
    ) as tmp:
        for expression, value in metrics.items():
            tmp.write(f"{expression} {value}\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, target)
    # Gauges only, meant to be world-readable by the collector (see the pipeline
    # module for the CodeQL note).
    os.chmod(target, 0o644)  # NOSONAR lgtm[py/overly-permissive-file]
    return target
