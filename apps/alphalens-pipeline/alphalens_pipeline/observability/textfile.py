"""Atomic Prometheus textfile-collector writer for AlphaLens CLI jobs.

This module is the Python half of the cron-observability stack (epic
PR-2). Each CLI command success-path calls :func:`emit_domain_metrics`
exactly once with a small dict of metric names → numeric values; the
emitter writes a ``.prom`` file that node_exporter's textfile collector
picks up on its next scrape (~15s cadence).

The textfile pattern is the canonical Prometheus answer for cron-style
batch jobs (see node_exporter README §Textfile Collector). It avoids
the operational footgun of Pushgateway (stale metrics from gone-away
jobs that linger forever) because the metrics dir is owned by the
host's filesystem, scraped in-place, and overwritten on the next run.

**Atomic write** is load-bearing here. node_exporter polls the directory
on its own schedule; without ``os.replace``, a half-written file is
visible to the scrape and either parses partially or is skipped with
an error. ``os.replace`` is atomic on POSIX (it's a single ``rename(2)``
under the hood) so the exporter only ever sees the previous fully-
written file or the new fully-written one.

**Why labels go in the metric KEY, not as a separate dict.** The
textfile collector reads raw Prometheus exposition format; passing
labels as a dict would force this module to know about label-value
escaping rules (quote-stripping, backslash-escaping). It is simpler
and harder to misuse if callers write the full PromQL expression
themselves::

    emit_domain_metrics(
        job="thematic-build",
        metrics={
            'alphalens_thematic_briefs_total{model="pro"}': 12,
            'alphalens_thematic_briefs_total{model="flash"}': 7,
        },
    )

The bash hook (``alphalens-emit-job-metrics``) uses the same approach.

**A bad value is dropped and reported, never written and never raised (#1462).**
Only a finite real number is a valid sample. A ``bool`` or ``str`` renders as a
token node_exporter cannot parse, and it then drops the WHOLE file; ``nan`` /
``inf`` parse, and then silently defeat every ``!= 0`` / ``> N`` alert rule. So a
refused value is left out of the file, the rest of the file is still written,
one ``alphalens_textfile_invalid_samples{textfile="<job>"} <count>`` line is
appended (only when something was refused, so a healthy file is unchanged), and
the ``AlphalensTextfileInvalidSample`` rule pages on it.

Raising instead would be the worse failure: the broker daemon calls this on
every tick and catches only ``OSError`` there, so a ``ValueError`` from a
metrics defect would stop the protective loop. The conversions below are
guarded for the same reason — nothing a caller passes can make this raise,
except the filesystem.

The refusal is logged at ERROR once per (job, expression) and then latched
until that expression writes cleanly again: the daemon emits every ~45 s and
the price stream every ~15 s, so a per-call ERROR would flood the journal.

The Django mirror (``alphalens-django/edge/ingest/textfile.py``) cannot import
this module (ADR 0011) and duplicates the same rules;
``tests/test_textfile_writer_parity.py`` runs one case table through both.
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

# Keep in lock-step with the ``${ALPHALENS_TEXTFILE_DIR:-...}`` default
# in ``deploy/systemd/bin/alphalens-emit-job-metrics``. The two halves
# of the metric stream MUST land in the same directory or Prometheus
# loses half the signal. A unit test in
# ``apps/alphalens-research/tests/test_observability_textfile.py`` pins
# the contract.
DEFAULT_DIR = Path.home() / ".alphalens" / "metrics"
ENV_VAR = "ALPHALENS_TEXTFILE_DIR"

# Written only when a value was refused; read by AlphalensTextfileInvalidSample.
# The label is ``textfile``, not ``job``: callers already put their own ``job``
# label on the samples in this file.
INVALID_SAMPLES_METRIC = "alphalens_textfile_invalid_samples"

# Log latch: (job, expression) pairs whose refusal has already been logged.
# Per process, so a restart starts clean. The lock makes check-then-add atomic
# across the price-stream thread and the daemon's main thread. Past the cap a
# new pair is not latched and logs on every refusal: degraded, never silenced.
_LATCH_CAP = 512
_LATCHED: set[tuple[str, str]] = set()
_LATCH_LOCK = threading.Lock()

# Anything a conversion of a caller's value can raise is a refusal, never a raise.
_CONVERSION_ERRORS = (ArithmeticError, ValueError, TypeError)


def _render_value(value: object) -> str | None:
    """The sample text for ``value``, or ``None`` when it is not a valid sample.

    ``bool`` is checked first because it IS an ``Integral``. ``numpy.bool_`` is
    not a ``numbers.Real``, so it falls out of the next check. Integral values
    render through ``int`` (numpy integers, ``IntEnum`` members). Other reals go
    through ``float`` and ``repr``: a Python float's ``repr`` is the shortest
    exact text, where ``repr`` of a numpy scalar is ``np.float64(1.5)``.
    """
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    try:
        if isinstance(value, numbers.Integral):
            return str(int(value))
        number = float(value)
    except _CONVERSION_ERRORS:
        return None
    return repr(number) if math.isfinite(number) else None


def _describe(value: object) -> str:
    """``repr`` for the log line, which must not raise either."""
    try:
        return repr(value)
    except Exception:  # a hostile __repr__ must not break the emit
        return f"<unrepresentable {type(value).__name__}>"


def _report_refusals(job: str, refused: Mapping[str, object], accepted: set[str]) -> None:
    """Log each refused expression once, and one INFO when it writes cleanly again."""
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


def _resolve_dir() -> Path:
    """Resolve the textfile directory, honoring the env-var override.

    Re-read on every call rather than caching: tests flip the env var
    inside ``setUp``/``tearDown`` and the production code path runs
    rarely enough that the lookup cost is irrelevant.
    """
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    # PROD ALWAYS sets ``ALPHALENS_TEXTFILE_DIR`` (the branch above), so this
    # ``Path.home()`` fallback is the dev/test default ONLY — it is NOT the
    # scraped dir in prod. The live VPS node_exporter scrapes
    # ``/var/lib/node_exporter/textfile`` (--collector.textfile.directory), and
    # BOTH halves of the metric stream route there via the env var: the host
    # cron ExecStopPost hooks read it from ``/etc/alphalens/env``, and the
    # pipeline Docker container gets an explicit
    # ``-e ALPHALENS_TEXTFILE_DIR=/var/lib/node_exporter/textfile`` + identity
    # bind mount in ``alphalens-thematic-build.service`` (so the Phase-4 stage
    # gauges + the VIX freshness gauge land on the scraped dir, not the
    # unscraped ``~/.alphalens/metrics`` bind mount). Do NOT assume
    # ``Path.home()/.alphalens/metrics`` is scraped — it is not. We re-evaluate
    # the env on every call so a test that swaps it is honored.
    return Path.home() / ".alphalens" / "metrics"


def emit_domain_metrics(job: str, metrics: Mapping[str, float | int]) -> Path:
    """Atomically write a Prometheus textfile for ``job``.

    Args:
        job: Short identifier matching the bash hook's first arg
            (``edgar-detect``, ``literature-scan-weekly``, etc.). Used
            as the filename suffix so domain metrics land in
            ``alphalens_domain_<job>.prom`` and the bash hook's
            cron-health metrics land in ``alphalens_job_<job>.prom``
            — separate files so the two emitters never race each other
            on the same path.
        metrics: Mapping from full PromQL metric expression (name +
            optional ``{labels}``) to numeric value. Caller-side
            formatting keeps this module ignorant of label escaping.
            Empty mapping is valid (writes an empty file) — emit a
            zero-row file if the job legitimately had nothing to
            report so the textfile still appears under the
            collector's directory.

    Returns:
        The final file path on disk (post-rename), useful for tests
        and for the success log line.

    Raises:
        OSError: if the textfile dir is unwriteable or a partial-write
            cleanup fails. The systemd unit's set -e + ExecStopPost
            will surface this as a unit failure. Never raised for a
            VALUE: a value that is not a finite number is dropped,
            counted in ``alphalens_textfile_invalid_samples`` and logged
            (see the module docstring).
    """
    # Validate everything BEFORE the tempfile exists, so a refusal can never
    # leave a ``.tmp`` behind in the scraped directory.
    lines: list[str] = []
    refused: dict[str, object] = {}
    accepted: set[str] = set()
    for metric_expr, value in metrics.items():
        rendered = _render_value(value)
        if rendered is None:
            refused[metric_expr] = value
        else:
            accepted.add(metric_expr)
            lines.append(f"{metric_expr} {rendered}\n")
    if refused:
        lines.append(f'{INVALID_SAMPLES_METRIC}{{textfile="{job}"}} {len(refused)}\n')
    _report_refusals(job, refused, accepted)

    out_dir = _resolve_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"alphalens_domain_{job}.prom"

    # Tempfile in the SAME directory so ``os.replace`` is a single
    # rename(2) (atomic on POSIX). A tempfile under ``/tmp`` would force
    # a cross-filesystem copy + delete + sync round-trip; that is
    # neither atomic nor safe to interleave with a poll.
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=out_dir,
        delete=False,
        suffix=".tmp",
        encoding="utf-8",
    ) as tmp:
        tmp.writelines(lines)
        tmp_path = Path(tmp.name)

    os.replace(tmp_path, target)

    # node_exporter's container runs as ``nobody`` (UID 65534); the
    # textfile collector reads scrape files as that user. ``tempfile``
    # defaults to 0o600 (owner-only) which makes node_exporter see
    # the file but fail to open it, silently dropping the series.
    # Promote to 0o644 (group + world readable) so any container user
    # — including ``nobody`` — can scrape. The file still lives under
    # the operator's home dir; the chmod only widens read access. The
    # companion bash hook (``alphalens-emit-job-metrics``) writes via
    # ``>`` which honors the systemd-user umask (typically 022 →
    # 0o644), so the bash side already does the right thing. Caught
    # during VPS cutover 2026-05-30 — node_exporter saw the bash
    # ``alphalens_job_*.prom`` files but not the Python
    # ``alphalens_domain_*.prom`` files until we manually chmod'd.
    #
    # 0o644 is the canonical mode for Prometheus textfile-collector
    # scrape files; CodeQL's "py/overly-permissive-file" rule is a
    # false positive for this specific use case (the file contains
    # only counters + gauges, no secrets). The contents are designed
    # to be world-readable.
    os.chmod(target, 0o644)  # NOSONAR lgtm[py/overly-permissive-file]
    return target
