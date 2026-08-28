"""Unit tests for the host-side systemd drift check (#1135).

The script under test is the promoted home of the narrow ``Environment=``
parser that ``test_deploy_systemd_units.py`` grew for #1134 — one parser, two
consumers. Everything here exercises the PURE functions over fixture text;
the VPS-facing IO (git blobs, ``systemctl show``) is a thin shell around them
and is verified live during the deploy cutover, not mocked here.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class TestSpecifierExpansion(unittest.TestCase):
    """``Environment=`` values may carry systemd specifiers — #1172 introduced
    the first one, ``%h`` in the price-reader socket path.

    systemd reports the EXPANDED value through ``show -p Environment``, so a
    raw text comparison reported permanent ``env_drift`` on a host that was in
    fact converged. That is not a cosmetic miss: the gauge feeds
    ``alphalens_systemd_drift_findings > 0``, so it alerted on every
    evaluation, and an alert that is always on is an alert nobody reads.
    """

    def test_percent_h_expands_to_the_running_users_home(self):
        composed = drift.composed_environment(
            "[Service]\nEnvironment=SOCK=%h/.alphalens/price_reader/reader.sock\n", []
        )
        self.assertEqual(composed["SOCK"], f"{Path.home()}/.alphalens/price_reader/reader.sock")

    def test_double_percent_is_a_literal_percent(self):
        composed = drift.composed_environment("[Service]\nEnvironment=A=100%%\n", [])
        self.assertEqual(composed["A"], "100%")

    def test_an_unsupported_specifier_in_the_REPO_blob_is_refused_too(self):
        """The refusal must not depend on the host happening to carry the same
        bytes.

        Only the host text used to be checked for unreadable forms. A repo
        blob introducing, say, `%t` would then be left unexpanded and compared
        as literal text against systemd's expanded value — a confidently wrong
        answer, which is worse than the loud false positive this PR removes.
        """
        # DIVERGENT on purpose. With identical texts the host-side check
        # already catches it, so that case cannot fail for the reason claimed
        # here. Only a repo blob the host does not carry exercises the gap.
        findings = drift.drift_findings(
            "alphalens-broker-manager",
            repo_files={
                "alphalens-broker-manager.service": "[Service]\nEnvironment=RUNTIME=%t/run\n"
            },
            host_files={
                "alphalens-broker-manager.service": "[Service]\nEnvironment=RUNTIME=/run/user/1000/run\n"
            },
            repo_env={},
            live_env={},
            host_only_vars=frozenset(),
        )
        self.assertTrue(
            any(f.kind == "unreadable_file" for f in findings),
            f"an unsupported specifier must surface as unreadable_file; got {findings}",
        )

    def test_an_unknown_specifier_is_refused_rather_than_compared(self):
        """Refusal, not best effort — the module's existing contract. An
        unexpanded specifier compared as literal text is exactly the silent
        wrong answer this check exists to prevent."""
        self.assertIsNotNone(drift.unreadable_reason("RUNTIME=%t/run"))
        self.assertIsNone(drift.unreadable_reason("SOCK=%h/run"))


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

    def test_unreadable_loaded_environment_is_a_finding_and_skips_env_comparison(self):
        # systemctl renders a value containing spaces QUOTED; the narrow
        # parser would shred it into phantom variables and then report
        # garbage env_drift (embedding value fragments). live_env=None means
        # "the loaded environment could not be honestly read": one finding,
        # no fabricated per-variable diffs.
        findings = self._findings(live_env=None)
        self.assertEqual(
            [(f.kind, f.subject) for f in findings],
            [("unreadable_file", "systemd:Environment")],
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


class TestMetricsWriting(unittest.TestCase):
    def test_writes_atomically_into_the_textfile_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"ALPHALENS_TEXTFILE_DIR": tmp}):
                drift._write_metrics("metric 1\n")
            target = Path(tmp) / drift.METRICS_BASENAME
            self.assertEqual(target.read_text(), "metric 1\n")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], [target.name])

    def test_skips_quietly_when_the_textfile_dir_is_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "ALPHALENS_TEXTFILE_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            drift._write_metrics("metric 1\n")  # must not raise


class TestLiveEnvironmentParsing(unittest.TestCase):
    def test_parses_the_property_payload(self):
        with mock.patch.object(drift, "_run", return_value="Environment=A=1 B=2\n"):
            self.assertEqual(drift._live_environment("u"), {"A": "1", "B": "2"})

    def test_empty_property_is_an_empty_environment(self):
        with mock.patch.object(drift, "_run", return_value="Environment=\n"):
            self.assertEqual(drift._live_environment("u"), {})

    def test_unreadable_property_returns_none(self):
        with mock.patch.object(drift, "_run", return_value='Environment=A="x y"\n'):
            self.assertIsNone(drift._live_environment("u"))


class TestHostFileReading(unittest.TestCase):
    def test_reads_base_unit_and_every_regular_dropin_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Path(tmp)
            (host / "u.service").write_text("base")
            d = host / "u.service.d"
            d.mkdir()
            (d / "10-a.conf").write_text("a")
            (d / "stale.disabled").write_text("s")
            (d / "sub").mkdir()  # directories are not files; skipped
            with mock.patch.object(drift, "HOST_UNIT_DIR", host):
                files = drift._host_files("u.service")
        self.assertEqual(
            files,
            {"u.service": "base", "10-a.conf": "a", "stale.disabled": "s"},
        )

    def test_missing_base_and_dropin_dir_yield_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(drift, "HOST_UNIT_DIR", Path(tmp)):
                self.assertEqual(drift._host_files("u.service"), {})


class TestRepoFileReading(unittest.TestCase):
    def test_reads_the_base_blob_and_every_listed_dropin_blob(self):
        def fake_run(argv, timeout=120):
            if argv[1] == "ls-tree":
                # Two distinct listings now: the tracked systemd directory
                # (to learn whether a drop-in dir exists at all) and then the
                # drop-in directory itself.
                if argv[-1].endswith(".d"):
                    return "10-a.conf\nREADME.md\n"
                return "u.service\nu.service.d\n"
            return f"blob:{argv[-1]}"

        with mock.patch.object(drift, "_run", side_effect=fake_run):
            files = drift._repo_files("u.service")
        self.assertEqual(
            files,
            {
                "u.service": "blob:origin/main:deploy/systemd/u.service",
                "10-a.conf": "blob:origin/main:deploy/systemd/u.service.d/10-a.conf",
                "README.md": "blob:origin/main:deploy/systemd/u.service.d/README.md",
            },
        )

    def test_a_unit_with_no_tracked_dropin_directory_reads_only_its_base(self):
        """Not every unit has a drop-in directory.

        ``git ls-tree origin/main:<dir>`` exits 128 when the path does not
        exist in the tree, which ``_run`` raises. Before this was handled,
        adding such a unit to ``UNITS`` (the shared price reader, #1172) made
        the whole check report ``check_failed`` and exit 1 — and exit 1 is
        reserved for "could not measure", so it stalled the job's last-success
        clock and would page through the staleness pair. ``_host_files``
        already guards the same case with ``is_dir()``; this is the repo half.
        """

        def fake_run(argv, timeout=120):
            if argv[1] == "ls-tree":
                # The tracked systemd directory holds the unit but no `.d` for it.
                return "u.service\nother.service\nother.service.d\n"
            return f"blob:{argv[-1]}"

        with mock.patch.object(drift, "_run", side_effect=fake_run):
            files = drift._repo_files("u.service")
        self.assertEqual(files, {"u.service": "blob:origin/main:deploy/systemd/u.service"})

    def test_a_git_failure_still_propagates(self):
        """The fix must not swallow a real git failure — an unreadable repo is
        exactly the 'cannot measure' case that exit 1 exists for."""

        def fake_run(argv, timeout=120):
            if argv[1] == "ls-tree":
                raise subprocess.CalledProcessError(2, argv)
            return "base"

        with mock.patch.object(drift, "_run", side_effect=fake_run):
            with self.assertRaises(subprocess.CalledProcessError):
                drift._repo_files("u.service")


class TestMainExitSemantics(unittest.TestCase):
    """Drift is a measurement (exit 0); only an inability to MEASURE is a
    job failure (exit 1)."""

    def _run_main(self, fetch_raises=False, repo_raises=False, drifted=False):
        base = "[Service]\nEnvironment=ALPHALENS_BROKER_ENVIRONMENT=sim\n"

        def fake_host_files(base_name):
            files = {base_name: base}
            if drifted and base_name == "alphalens-broker-manager.service":
                files["extra.conf"] = "[Service]\nEnvironment=X=1\n"
            return files

        def fake_run(argv, timeout=120):
            if argv[1] == "fetch":
                if fetch_raises:
                    raise subprocess.SubprocessError("fetch down")
                return ""
            if repo_raises:
                raise subprocess.SubprocessError("git down")
            if argv[1] == "ls-tree":
                return ""
            if argv[1] == "show":
                return base
            if argv[0] == "systemctl":
                return "Environment=ALPHALENS_BROKER_ENVIRONMENT=sim\n"
            raise AssertionError(f"unexpected argv {argv}")

        written: dict[str, str] = {}
        with (
            mock.patch.object(drift, "_run", side_effect=fake_run),
            mock.patch.object(drift, "_host_files", side_effect=fake_host_files),
            mock.patch.object(
                drift, "_write_metrics", side_effect=lambda text: written.update(metrics=text)
            ),
        ):
            code = drift.main()
        return code, written.get("metrics", "")

    def test_converged_host_exits_zero_with_zero_gauges(self):
        code, metrics = self._run_main()
        self.assertEqual(code, 0)
        for unit, _base in drift.UNITS:
            self.assertIn(f'alphalens_systemd_drift_findings{{unit="{unit}"}} 0', metrics)

    def test_drift_still_exits_zero_and_the_gauge_carries_the_count(self):
        code, metrics = self._run_main(drifted=True)
        self.assertEqual(code, 0)
        self.assertIn('unit="alphalens-broker-manager"} 1', metrics)

    def test_fetch_failure_exits_one_and_writes_no_metrics(self):
        code, metrics = self._run_main(fetch_raises=True)
        self.assertEqual(code, 1)
        self.assertEqual(metrics, "")

    def test_repo_read_failure_exits_one(self):
        code, _metrics = self._run_main(repo_raises=True)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
