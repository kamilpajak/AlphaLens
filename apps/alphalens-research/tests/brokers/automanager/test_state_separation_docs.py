"""Doc/code consistency for per-environment broker-state separation (ADR 0016).

Design memo `docs/research/broker_env_state_separation_design_2026_08_10.md`
D8 requires the deploy artifacts to carry three things the code alone cannot
enforce: the one-time VPS state-migration runbook (memo §6), the doctrine
that `ALPHALENS_BROKER_ENVIRONMENT` is pinned IN the unit file and forbidden
in `/etc/alphalens/env`, and the global-vs-per-instance KILL semantics. This
pins that `deploy/systemd/README.md` and the root `.env.example` actually say
these things, using the seam module's own constants so a future rename of
the env var or the "sim" literal cannot let the docs drift silently out of
sync (mirrors the ``test_stream_metric_docs.py`` pattern).
"""

from __future__ import annotations

import unittest
from pathlib import Path

from alphalens_pipeline.brokers.automanager.state_paths import (
    BROKER_ENVIRONMENT_ENV,
    ENV_SIM,
)

# tests/brokers/automanager/ is two levels deeper than tests/, so the repo
# root is parents[5] (mirrors test_stream_metric_docs.py).
WORKSPACE_ROOT = Path(__file__).resolve().parents[5]
README = WORKSPACE_ROOT / "deploy" / "systemd" / "README.md"
ROOT_ENV_EXAMPLE = WORKSPACE_ROOT / ".env.example"

_SIM_PIN = f"{BROKER_ENVIRONMENT_ENV}={ENV_SIM}"


class TestReadmeStateMigrationRunbook(unittest.TestCase):
    """README §"Saxo auto-manager (SIM)" carries the memo §6 migration steps."""

    def setUp(self) -> None:
        self.text = README.read_text(encoding="utf-8")

    def test_stops_the_daemon_before_migrating(self) -> None:
        self.assertIn("systemctl --user stop alphalens-broker-manager", self.text)

    def test_creates_the_per_env_state_roots(self) -> None:
        self.assertIn(
            "mkdir -p ~/.alphalens/broker_orders/sim ~/.alphalens/exec_quality/sim",
            self.text,
        )

    def test_moves_the_flat_journals_into_the_sim_subdirectory(self) -> None:
        self.assertIn(
            "mv ~/.alphalens/broker_orders/*.jsonl ~/.alphalens/broker_orders/sim/",
            self.text,
        )

    def test_moves_the_exec_quality_parquet_into_the_sim_subdirectory(self) -> None:
        self.assertIn(
            "mv ~/.alphalens/exec_quality/tranche_fills.parquet ~/.alphalens/exec_quality/sim/",
            self.text,
        )

    def test_removes_the_stale_prometheus_textfiles(self) -> None:
        self.assertIn(
            "rm -f /var/lib/node_exporter/textfile/alphalens_domain_broker-manager.prom",
            self.text,
        )

    def test_verify_step_names_the_sim_job_label(self) -> None:
        self.assertIn('job="broker-manager-sim"', self.text)

    def test_documents_leftover_parent_kill_as_the_normal_global_state(self) -> None:
        self.assertIn(
            "leftover `broker_orders/KILL` at the parent level is now the GLOBAL kill",
            self.text,
        )


class TestReadmeEnvVarDoctrineNote(unittest.TestCase):
    """The unit-file-only pin + /etc/alphalens/env ban must be spelled out."""

    def setUp(self) -> None:
        self.text = README.read_text(encoding="utf-8")

    def test_names_the_env_var(self) -> None:
        self.assertIn(BROKER_ENVIRONMENT_ENV, self.text)

    def test_forbids_setting_it_in_the_etc_env_file(self) -> None:
        # Markdown backticks may sit between "Never set" and the var name, so
        # match the two halves independently rather than one literal phrase.
        lowered = self.text.lower()
        self.assertIn("never set", lowered)
        self.assertIn(
            f"{BROKER_ENVIRONMENT_ENV.lower()}` in `/etc/alphalens/env",
            lowered,
        )

    def test_explains_environmentfile_overrides_drop_ins(self) -> None:
        # The 08-10 incident lesson: EnvironmentFile wins over drop-ins, so the
        # in-unit Environment= line is the only place the pin cannot be lost.
        self.assertIn("EnvironmentFile", self.text)
        self.assertIn("drop-in", self.text.lower())


class TestReadmeGlobalVsInstanceKill(unittest.TestCase):
    def setUp(self) -> None:
        self.text = README.read_text(encoding="utf-8")

    def test_names_the_global_kill_path(self) -> None:
        self.assertIn("broker_orders/KILL", self.text)

    def test_names_the_per_instance_kill_path(self) -> None:
        self.assertIn("broker_orders/sim/KILL", self.text)

    def test_states_global_kill_stops_every_instance(self) -> None:
        self.assertIn("every instance", self.text.lower())


class TestRootEnvExampleBrokerEnvironmentEntry(unittest.TestCase):
    def setUp(self) -> None:
        self.text = ROOT_ENV_EXAMPLE.read_text(encoding="utf-8")

    def test_documents_the_var_as_commented_out(self) -> None:
        self.assertIn(f"# {_SIM_PIN}", self.text)

    def test_comment_states_it_is_a_per_unit_systemd_pin_not_a_dotenv_value(self) -> None:
        self.assertIn("per-unit", self.text)
        self.assertIn("systemd", self.text)
        self.assertIn("NOT a .env value", self.text)


if __name__ == "__main__":
    unittest.main()
