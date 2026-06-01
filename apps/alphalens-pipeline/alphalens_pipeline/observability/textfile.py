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
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

# Keep in lock-step with the ``${ALPHALENS_TEXTFILE_DIR:-...}`` default
# in ``deploy/systemd/bin/alphalens-emit-job-metrics``. The two halves
# of the metric stream MUST land in the same directory or Prometheus
# loses half the signal. A unit test in
# ``apps/alphalens-research/tests/test_observability_textfile.py`` pins
# the contract.
DEFAULT_DIR = Path.home() / ".alphalens" / "metrics"
ENV_VAR = "ALPHALENS_TEXTFILE_DIR"


def _resolve_dir() -> Path:
    """Resolve the textfile directory, honoring the env-var override.

    Re-read on every call rather than caching: tests flip the env var
    inside ``setUp``/``tearDown`` and the production code path runs
    rarely enough that the lookup cost is irrelevant.
    """
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    # ``Path.home()`` evaluates ``$HOME``, which container-side defaults
    # of ``Path.home() / ".alphalens" / ...`` have tripped on before
    # (see ``feedback_pathhome_in_container_trap_2026_05_28`` memory).
    # This emitter runs BOTH from the host venv (cron ExecStopPost hooks)
    # AND inside the pipeline Docker container (the Phase 4 per-stage
    # thematic volume gauges): the thematic-build unit sets
    # ``HOME=/app/home`` and bind-mounts ``%h/.alphalens`` there, so
    # ``Path.home()/.alphalens/metrics`` resolves to the SAME host
    # directory node_exporter scrapes. We re-evaluate ``$HOME`` on every
    # call so a test that swaps it is honored.
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
            will surface this as a unit failure.
    """
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
        for metric_expr, value in metrics.items():
            tmp.write(f"{metric_expr} {value}\n")
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
