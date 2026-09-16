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

Identical to the pipeline module on purpose: a value that is not a finite number
is DROPPED, counted in ``alphalens_textfile_invalid_samples{textfile="<job>"}`` and
logged once, never written and never raised (#1462 — see the pipeline module for
why raising is the worse failure). The two copies cannot share code, so
``apps/alphalens-research/tests/test_textfile_writer_parity.py`` loads this file by
path and runs one case table through both.

If a second Django command ever needs this, the shared home is
``apps/alphalens-feedback`` (primitives consumed by both the pipeline and the
Django API) — extract on second use, not before.
"""

from __future__ import annotations

import logging
import math
import numbers
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

# Same variable the bash hook and the pipeline writer honour; prod sets it to the
# directory node_exporter scrapes (/var/lib/node_exporter/textfile).
ENV_VAR = "ALPHALENS_TEXTFILE_DIR"

# Mirror of alphalens_pipeline.observability.textfile (#1462); keep the two in step.
INVALID_SAMPLES_METRIC = "alphalens_textfile_invalid_samples"
_LATCH_CAP = 512
_LATCHED: set[tuple[str, str]] = set()
_LATCH_LOCK = threading.Lock()


def _render_value(value: object) -> str | None:
    """The sample text for ``value``, or ``None`` when it is not a valid sample."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    try:
        if isinstance(value, numbers.Integral):
            return str(int(value))
        number = float(value)
    except Exception:  # noqa: BLE001 - a caller's __int__ / __float__ may raise anything
        return None
    return repr(number) if math.isfinite(number) else None


def _describe(value: object) -> str:
    try:
        return repr(value)
    except Exception:  # noqa: BLE001 - a hostile __repr__ must not break the emit
        return f"<unrepresentable {type(value).__name__}>"


def _report_refusals(job: str, refused: Mapping[str, object], accepted: set[str]) -> None:
    for expression, value in refused.items():
        key = (job, expression)
        with _LATCH_LOCK:
            if key in _LATCHED:
                continue
            if len(_LATCHED) < _LATCH_CAP:
                _LATCHED.add(key)
        logger.error(
            "%s: dropped %s = %s (%s): not a finite number, sample not written (#1462)",
            job,
            expression,
            _describe(value),
            type(value).__name__,
        )
    recovered: list[str] = []
    with _LATCH_LOCK:
        for expression in accepted:
            key = (job, expression)
            if key in _LATCHED:
                _LATCHED.discard(key)
                recovered.append(expression)
    for expression in recovered:
        logger.info("%s: %s is a valid sample again", job, expression)


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
    does not exist or cannot be written. Never raises for a value: one that is
    not a finite number is dropped, counted and logged (see the module docstring).
    """
    lines: list[str] = []
    refused: dict[str, object] = {}
    accepted: set[str] = set()
    for expression, value in metrics.items():
        rendered = _render_value(value)
        if rendered is None:
            refused[expression] = value
        else:
            accepted.add(expression)
            lines.append(f"{expression} {rendered}\n")
    if refused:
        lines.append(f'{INVALID_SAMPLES_METRIC}{{textfile="{job}"}} {len(refused)}\n')
    _report_refusals(job, refused, accepted)

    out_dir = resolve_dir()
    if out_dir is None:
        logger.warning("%s: %s unset - metrics not written (#1436)", job, ENV_VAR)
        return None

    target = out_dir / f"alphalens_domain_{job}.prom"
    with tempfile.NamedTemporaryFile(
        mode="w", dir=out_dir, delete=False, suffix=".tmp", encoding="utf-8"
    ) as tmp:
        tmp.writelines(lines)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, target)
    # Gauges only, meant to be world-readable by the collector (see the pipeline
    # module for the CodeQL note).
    os.chmod(target, 0o644)  # NOSONAR lgtm[py/overly-permissive-file]
    return target
