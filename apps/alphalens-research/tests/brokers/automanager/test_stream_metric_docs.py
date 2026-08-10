"""Doc/code consistency for the stream-liveness Prometheus gauge.

The runbook (``deploy/systemd/README.md`` §8.5) and ``.env.example`` tell the
operator to hand-sync the ``AlphalensBrokerStreamStale`` rule against the gauge
the daemon emits. If those docs name a gauge the code does not emit, the alert
targets a non-existent metric and silently never fires — defeating the memo's
"latency regression is OBSERVABLE, not silent" requirement.

This pins the documented gauge name to the base metric the code actually emits
(``stream_last_message_metric()`` in ``control_loop.py``), so a rename of the
gauge cannot drift the runbook out of sync unnoticed. The ``{job=...}`` label is
per-instance since ADR 0016 D5, but the bare gauge name below it is fixed
regardless of job, which is all the runbook needs to name.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from alphalens_pipeline.brokers.automanager.control_loop import (
    stream_last_message_metric,
)

# tests/brokers/automanager/ is two levels deeper than tests/, so the repo root
# is parents[5].
WORKSPACE_ROOT = Path(__file__).resolve().parents[5]
README = WORKSPACE_ROOT / "deploy" / "systemd" / "README.md"
ENV_EXAMPLE = WORKSPACE_ROOT / ".env.example"

# The emitted metric carries a ``{job="..."}`` label selector; the bare gauge
# name the operator writes into the alert rule is everything before the brace.
# The job value used here (a placeholder) is irrelevant to the base-name check.
_GAUGE_BASE = stream_last_message_metric("broker-manager-sim").split("{", 1)[0]


class TestStreamMetricDocsMatchCode(unittest.TestCase):
    def test_gauge_base_name_is_the_full_age_seconds_form(self) -> None:
        # Guard against a truncated constant creeping back in.
        self.assertEqual(
            _GAUGE_BASE,
            "alphalens_broker_manager_stream_last_message_age_seconds",
        )

    def test_readme_names_the_emitted_gauge(self) -> None:
        text = README.read_text(encoding="utf-8")
        self.assertIn(
            _GAUGE_BASE,
            text,
            "README §8.5 must name the gauge the code emits so the hand-synced "
            "AlphalensBrokerStreamStale rule targets a real metric.",
        )

    def test_env_example_names_the_emitted_gauge(self) -> None:
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn(_GAUGE_BASE, text)

    def test_docs_do_not_name_a_truncated_gauge(self) -> None:
        # ``broker_manager_stream_last_message`` without the ``_age_seconds``
        # suffix (and not as a substring of the correct name) is the stale form
        # that would silently never match.
        stale = r"broker_manager_stream_last_message(?!_age_seconds)"
        for path in (README, ENV_EXAMPLE):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(stale, text),
                f"{path.name} still names the truncated gauge form.",
            )


if __name__ == "__main__":
    unittest.main()
