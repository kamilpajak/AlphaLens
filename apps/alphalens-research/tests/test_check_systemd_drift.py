"""Unit tests for the host-side systemd drift check (#1135).

The script under test is the promoted home of the narrow ``Environment=``
parser that ``test_deploy_systemd_units.py`` grew for #1134 — one parser, two
consumers. Everything here exercises the PURE functions over fixture text;
the VPS-facing IO (git blobs, ``systemctl show``) is a thin shell around them
and is verified live during the deploy cutover, not mocked here.
"""

from __future__ import annotations

import unittest

from scripts import check_systemd_drift as drift

_BASE = """
[Service]
Environment=ALPHALENS_BROKER_ENVIRONMENT=sim
Environment=ALPHALENS_TEXTFILE_DIR=/var/lib/node_exporter/textfile
"""

_DROPIN_A = "[Service]\nEnvironment=ALPHALENS_BROKER_MAX_OPEN=10\n"
_DROPIN_B = "[Service]\nEnvironment=ALPHALENS_BROKER_MAX_OPEN=2\n"


class TestEnvironmentParser(unittest.TestCase):
    def test_reads_simple_assignments_and_lists(self):
        parsed = drift.environment_assignments("Environment=FIRST=1 SECOND=2\n")
        self.assertEqual(parsed, {"FIRST": "1", "SECOND": "2"})

    def test_refuses_every_form_it_would_misread(self):
        # The refusal guard travels with the parser: a quietly wrong
        # composition is the one failure this module exists to prevent.
        for assignments, why in (
            ('"FOO=1 BAR=2"', "quoted"),
            ("FOO='a b'", "quoted"),
            ("FOO=1 \\", "line continuation"),
            ("", "bare Environment= reset"),
            ("JUSTANAME", "not an assignment"),
        ):
            with self.subTest(form=why):
                self.assertIsNotNone(drift.unreadable_reason(assignments), why)

    def test_accepts_the_simple_form(self):
        self.assertIsNone(drift.unreadable_reason("FOO=1 BAR=x"))


class TestComposedEnvironment(unittest.TestCase):
    def test_dropins_apply_after_the_base_in_the_given_order(self):
        composed = drift.composed_environment(
            _BASE, [("10-a.conf", _DROPIN_A), ("20-b.conf", _DROPIN_B)]
        )
        self.assertEqual(composed["ALPHALENS_BROKER_MAX_OPEN"], "2")
        self.assertEqual(composed["ALPHALENS_BROKER_ENVIRONMENT"], "sim")


class TestStripHostOnlyLines(unittest.TestCase):
    def test_drops_lines_assigning_only_allowlisted_vars(self):
        text = (
            "[Service]\n"
            "Environment=ALPHALENS_SAXO_LIVE_STANDING=opaque\n"
            "Environment=SAXO_LIVE_ACCOUNT_KEY=opaque\n"
            "ExecStart=/bin/true\n"
        )
        stripped = drift.strip_host_only_environment_lines(
            text, {"ALPHALENS_SAXO_LIVE_STANDING", "SAXO_LIVE_ACCOUNT_KEY"}
        )
        self.assertEqual(stripped, "[Service]\nExecStart=/bin/true\n")

    def test_keeps_a_line_that_mixes_allowlisted_and_governed_vars(self):
        # A mixed line is NOT host-only: dropping it would hide a governed
        # assignment behind the allowlist.
        text = "Environment=SAXO_LIVE_ACCOUNT_KEY=opaque ALPHALENS_BROKER_MAX_OPEN=9\n"
        stripped = drift.strip_host_only_environment_lines(text, {"SAXO_LIVE_ACCOUNT_KEY"})
        self.assertEqual(stripped, text)


class TestDriftFindings(unittest.TestCase):
    """The pure comparison over (repo files, host files, repo env, live env)."""

    def _findings(self, **overrides):
        repo_files = {
            "alphalens-broker-manager.service": _BASE,
            "10-a.conf": _DROPIN_A,
        }
        args = {
            "unit": "alphalens-broker-manager",
            "repo_files": repo_files,
            "host_files": dict(repo_files),
            "repo_env": {"ALPHALENS_BROKER_MAX_OPEN": "10"},
            "live_env": {"ALPHALENS_BROKER_MAX_OPEN": "10"},
            "host_only_vars": frozenset(),
        }
        args.update(overrides)
        return drift.drift_findings(**args)

    def test_identical_state_yields_no_findings(self):
        self.assertEqual(self._findings(), [])

    def test_untracked_host_file_is_flagged_by_name(self):
        # The #1136 lesson: a stale file with matching values is still a host
        # governed by something unreadable from the repo.
        findings = self._findings(
            host_files={
                "alphalens-broker-manager.service": _BASE,
                "10-a.conf": _DROPIN_A,
                "zz-mystery.conf": "[Service]\nEnvironment=X=1\n",
            }
        )
        self.assertEqual(
            [(f.kind, f.subject) for f in findings],
            [("untracked_file", "zz-mystery.conf")],
        )

    def test_tracked_file_missing_from_host_is_flagged(self):
        findings = self._findings(host_files={"alphalens-broker-manager.service": _BASE})
        self.assertEqual(
            [(f.kind, f.subject) for f in findings],
            [("missing_file", "10-a.conf")],
        )

    def test_content_drift_is_flagged(self):
        findings = self._findings(
            host_files={
                "alphalens-broker-manager.service": _BASE,
                "10-a.conf": "[Service]\nEnvironment=ALPHALENS_BROKER_MAX_OPEN=99\n",
            }
        )
        self.assertEqual(
            [(f.kind, f.subject) for f in findings],
            [("content_drift", "10-a.conf")],
        )

    def test_env_drift_changed_extra_and_missing_variables(self):
        findings = self._findings(
            repo_env={"A": "1", "B": "2"},
            live_env={"A": "OTHER", "C": "3"},
        )
        self.assertEqual(
            {(f.kind, f.subject) for f in findings},
            {("env_drift", "A"), ("env_drift", "B"), ("env_drift", "C")},
        )

    def test_host_only_vars_are_excluded_from_env_comparison(self):
        findings = self._findings(
            live_env={
                "ALPHALENS_BROKER_MAX_OPEN": "10",
                "SAXO_LIVE_ACCOUNT_KEY": "opaque",
            },
            host_only_vars=frozenset({"SAXO_LIVE_ACCOUNT_KEY"}),
        )
        self.assertEqual(findings, [])

    def test_host_only_vars_do_not_mask_real_env_drift(self):
        # Positive control for the exclusion: everything else still compares.
        findings = self._findings(
            live_env={
                "ALPHALENS_BROKER_MAX_OPEN": "99",
                "SAXO_LIVE_ACCOUNT_KEY": "opaque",
            },
            host_only_vars=frozenset({"SAXO_LIVE_ACCOUNT_KEY"}),
        )
        self.assertEqual(
            [(f.kind, f.subject) for f in findings],
            [("env_drift", "ALPHALENS_BROKER_MAX_OPEN")],
        )

    def test_base_unit_grant_lines_are_tolerated_but_other_diffs_flag(self):
        # The LIVE base unit legitimately carries the two account-bound grant
        # lines on the host only (ADR 0017). Stripping them must make the
        # bytes match — and must NOT swallow any other difference.
        host_base = _BASE + "Environment=SAXO_LIVE_ACCOUNT_KEY=opaque\n"
        clean = self._findings(
            host_files={
                "alphalens-broker-manager.service": host_base,
                "10-a.conf": _DROPIN_A,
            },
            host_only_vars=frozenset({"SAXO_LIVE_ACCOUNT_KEY"}),
        )
        self.assertEqual(clean, [])
        tampered = self._findings(
            host_files={
                "alphalens-broker-manager.service": host_base + "ExecStart=/bin/evil\n",
                "10-a.conf": _DROPIN_A,
            },
            host_only_vars=frozenset({"SAXO_LIVE_ACCOUNT_KEY"}),
        )
        self.assertEqual(
            [(f.kind, f.subject) for f in tampered],
            [("content_drift", "alphalens-broker-manager.service")],
        )

    def test_unreadable_host_file_is_a_finding_not_a_crash(self):
        # Repo files are guarded readable by the deploy-units tests; a HOST
        # file the parser cannot honestly read is itself drift evidence.
        findings = self._findings(
            host_files={
                "alphalens-broker-manager.service": _BASE,
                "10-a.conf": '[Service]\nEnvironment="A=1 B=2"\n',
            }
        )
        self.assertEqual(
            {(f.kind, f.subject) for f in findings},
            {("content_drift", "10-a.conf"), ("unreadable_file", "10-a.conf")},
        )

    def test_findings_never_carry_host_only_values(self):
        # The grant values are opaque account identifiers; no finding text may
        # embed them even when the base unit ALSO has real drift.
        host_base = _BASE + "Environment=SAXO_LIVE_ACCOUNT_KEY=opaque-value\nExecStart=/bin/evil\n"
        findings = self._findings(
            host_files={
                "alphalens-broker-manager.service": host_base,
                "10-a.conf": _DROPIN_A,
            },
            host_only_vars=frozenset({"SAXO_LIVE_ACCOUNT_KEY"}),
        )
        self.assertTrue(findings)
        for f in findings:
            self.assertNotIn("opaque-value", f.detail)


class TestMetricsRendering(unittest.TestCase):
    def test_renders_a_zero_sample_per_unit(self):
        # An absent series must mean "broken emitter", never "no drift".
        text = drift.render_metrics(
            {"alphalens-broker-manager": 0, "alphalens-broker-manager-live": 3}
        )
        self.assertIn('alphalens_systemd_drift_findings{unit="alphalens-broker-manager"} 0', text)
        self.assertIn(
            'alphalens_systemd_drift_findings{unit="alphalens-broker-manager-live"} 3',
            text,
        )
        self.assertTrue(text.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
