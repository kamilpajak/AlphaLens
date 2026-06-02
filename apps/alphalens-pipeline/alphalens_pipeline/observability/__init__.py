"""Prometheus textfile-collector emission helpers.

CLI commands import :func:`emit_domain_metrics` to publish
domain-specific counters (briefs written, candidates dispatched, AV
tickers cached, etc.) that node_exporter's
``--collector.textfile.directory`` scrapes on the VPS. The companion
bash hook ``deploy/systemd/bin/alphalens-emit-job-metrics`` publishes
the cron-health metrics (last_success_timestamp, last_duration,
last_exit_code) from systemd ExecStopPost.

The two halves agree on the textfile directory via the
``ALPHALENS_TEXTFILE_DIR`` env var; :data:`textfile.DEFAULT_DIR`
(``~/.alphalens/metrics/``) is the Python side of that contract but is the
dev/test default ONLY. In prod every emitter (host cron hooks AND the
pipeline Docker container) sets ``ALPHALENS_TEXTFILE_DIR=/var/lib/
node_exporter/textfile`` — the dir the live node_exporter actually scrapes.
"""

from alphalens_pipeline.observability.textfile import (
    DEFAULT_DIR,
    ENV_VAR,
    emit_domain_metrics,
)

__all__ = ["DEFAULT_DIR", "ENV_VAR", "emit_domain_metrics"]
