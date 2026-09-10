"""Tests for ``unit_env`` — composing a one-off LIVE environment from the
installed systemd unit (#1377).

A LIVE read command needs BOTH sources the daemon gets: the unit's
``Environment=`` (the nine risk rails + the account-bound grant, drop-ins
merged) and the ``EnvironmentFile=`` the unit names (the ``SAXO_LIVE_*``
credentials `LiveAuthConfig.from_env` reads). This module composes them the
way systemd does — ``Environment=`` first, the file second, so the FILE wins —
and refuses loud when it cannot read either honestly.

Every check here is hermetic: the ``run`` and ``read_text`` seams are injected,
so no test touches systemctl or ``/etc/alphalens/env``. The payloads below
mirror the shapes measured on the production VPS on 2026-09-08 (21 unit keys
including ``ALLOW_ORDERS=1`` from a drop-in; a file with an ``export`` line and
a quoted value; ``EnvironmentFiles`` rendered with an ``(ignore_errors=no)``
suffix; a missing unit answering with an EMPTY payload and exit code 0).
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from alphalens_pipeline.brokers.automanager import unit_env

_UNIT_PAYLOAD = (
    "ALPHALENS_BROKER_ENVIRONMENT=live ALPHALENS_BROKER_ALLOW_ORDERS=1 "
    "ALPHALENS_BROKER_MAX_OPEN=10 ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC=1.0 "
    "ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R=1.0 ALPHALENS_BROKER_SIZING_EQUITY=15000 "
    "ALPHALENS_BROKER_SIZING_EQUITY_MODE=declared "
    "ALPHALENS_BROKER_EXIT_POLICY=breakeven_trail ALPHALENS_BROKER_MAX_FEE_BPS=1000 "
    "ALPHALENS_BROKER_ENTRY_TRAIL_BPS=50 ALPHALENS_BROKER_ENTRY_WATCH_MAX_PICKS=10 "
    "ALPHALENS_SAXO_LIVE_STANDING=ACCT-1 SAXO_LIVE_ACCOUNT_KEY=ACCT-1"
)
_ENV_FILE_TEXT = """\
# shared credentials
SAXO_LIVE_APP_KEY=key-live
export SAXO_LIVE_APP_SECRET=secret-live
SAXO_LIVE_AUTH_REDIRECT_URL="http://localhost:8766/callback"

TELEGRAM_BOT_TOKEN=tg
"""
_ENV_FILE_PATH = "/etc/alphalens/env"


def _fake_run(
    *,
    payload: str = _UNIT_PAYLOAD,
    load_state: str = "loaded",
    env_files: str = f"{_ENV_FILE_PATH} (ignore_errors=no)",
    dropins: str = "/h/10-allow-orders.conf /h/99-live-grant.conf",
    needs_reload: str = "no",
    missing: bool = False,
):
    """A ``run`` seam standing in for ``systemctl --user show -p <prop> --value``."""
    answers = {
        "LoadState": load_state,
        "Environment": payload,
        "EnvironmentFiles": env_files,
        "DropInPaths": dropins,
        "NeedDaemonReload": needs_reload,
    }

    def run(_unit: str, prop: str) -> str:
        if missing:
            raise FileNotFoundError("systemctl")
        return answers[prop]

    return run


def _compose(**kwargs):
    return unit_env.compose_live_environment(
        run=_fake_run(**kwargs), read_text=lambda _path: _ENV_FILE_TEXT
    )


class TestUnitForEnv(unittest.TestCase):
    def test_the_two_instances_map_to_the_daemon_units(self) -> None:
        self.assertEqual(unit_env.unit_for_env("sim"), "alphalens-broker-manager.service")
        self.assertEqual(unit_env.unit_for_env("live"), "alphalens-broker-manager-live.service")

    def test_an_unknown_instance_is_refused(self) -> None:
        with self.assertRaises(unit_env.UnitEnvError):
            unit_env.unit_for_env("staging")


class TestParseEnvironmentPayload(unittest.TestCase):
    def test_space_separated_assignments(self) -> None:
        parsed = unit_env.parse_environment_payload("A=1 B=2")
        self.assertEqual(parsed, {"A": "1", "B": "2"})

    def test_a_quoted_value_keeps_its_space(self) -> None:
        # systemd quotes a value containing a space; splitting on whitespace
        # would shred it into a phantom variable.
        parsed = unit_env.parse_environment_payload('A="one two" B=3')
        self.assertEqual(parsed, {"A": "one two", "B": "3"})

    def test_only_the_first_equals_splits(self) -> None:
        parsed = unit_env.parse_environment_payload("URL=http://h/?a=b")
        self.assertEqual(parsed, {"URL": "http://h/?a=b"})

    def test_empty_payload_is_empty(self) -> None:
        self.assertEqual(unit_env.parse_environment_payload("   "), {})

    def test_a_token_without_equals_is_refused_never_misread(self) -> None:
        with self.assertRaises(unit_env.UnitEnvError):
            unit_env.parse_environment_payload("A=1 garbage")


class TestParseEnvironmentFile(unittest.TestCase):
    def test_comments_blanks_export_and_quotes(self) -> None:
        parsed = unit_env.parse_environment_file(_ENV_FILE_TEXT)
        self.assertEqual(parsed["SAXO_LIVE_APP_KEY"], "key-live")
        self.assertEqual(parsed["SAXO_LIVE_APP_SECRET"], "secret-live")
        self.assertEqual(parsed["SAXO_LIVE_AUTH_REDIRECT_URL"], "http://localhost:8766/callback")
        self.assertEqual(parsed["TELEGRAM_BOT_TOKEN"], "tg")
        self.assertNotIn("#", "".join(parsed))

    def test_single_quotes_and_inline_hash_inside_a_value(self) -> None:
        parsed = unit_env.parse_environment_file("A='v#1'\nB=plain\n")
        self.assertEqual(parsed, {"A": "v#1", "B": "plain"})

    def test_a_line_without_equals_is_ignored_not_fatal(self) -> None:
        # EnvironmentFile syntax tolerates junk lines; systemd logs and skips.
        self.assertEqual(unit_env.parse_environment_file("nonsense\nA=1\n"), {"A": "1"})


class TestParseEnvironmentFilesProperty(unittest.TestCase):
    def test_the_ignore_errors_suffix_is_stripped(self) -> None:
        parsed = unit_env.parse_environment_files_property("/etc/alphalens/env (ignore_errors=no)")
        self.assertEqual(parsed, [(Path("/etc/alphalens/env"), False)])

    def test_ignore_errors_yes_is_carried(self) -> None:
        parsed = unit_env.parse_environment_files_property("/tmp/opt (ignore_errors=yes)")
        self.assertEqual(parsed, [(Path("/tmp/opt"), True)])

    def test_empty_property_is_no_files(self) -> None:
        self.assertEqual(unit_env.parse_environment_files_property(""), [])


class TestTextfileDirForEnv(unittest.TestCase):
    """The metrics directory the named instance's unit publishes (#1394).

    `stream-status --env <x>` needs ONE path from the unit, not the whole
    environment. Measured on the VPS 2026-09-10: `ALPHALENS_TEXTFILE_DIR` is in
    the `Environment=` of BOTH units, while the shared `EnvironmentFile=`
    carries keys (`ALPHA_VANTAGE_API_KEY` and friends) that this narrow read
    deliberately never touches — `systemctl show -p Environment` returned 16
    keys against the file's 19, and the file-only keys were absent from it.
    """

    _WITH_DIR = _UNIT_PAYLOAD + " ALPHALENS_TEXTFILE_DIR=/var/lib/node_exporter/textfile"

    def test_both_instances_resolve_their_own_unit(self) -> None:
        # sim is the case `_apply_env_option` cannot serve: it never shells out
        # for sim by design (#1377), which is why this helper exists.
        for env in ("sim", "live"):
            with self.subTest(env=env):
                seen: list[str] = []

                def run(unit: str, prop: str, _seen: list[str] = seen) -> str:
                    _seen.append(unit)
                    return {"LoadState": "loaded", "Environment": self._WITH_DIR}[prop]

                self.assertEqual(
                    unit_env.textfile_dir_for_env(env, run=run),
                    "/var/lib/node_exporter/textfile",
                )
                self.assertEqual(set(seen), {unit_env.unit_for_env(env)})

    def test_no_systemctl_is_a_None_not_a_raise(self) -> None:
        # A developer Mac has no user manager; the caller falls back to the
        # process env, exactly as it behaves today.
        self.assertIsNone(unit_env.textfile_dir_for_env("sim", run=_fake_run(missing=True)))

    def test_a_hung_systemctl_is_a_None_not_a_raise(self) -> None:
        # TimeoutExpired is a SubprocessError and NOT an OSError — the #1384
        # lesson, where catching OSError alone let a hung probe abort a read.
        def run(_unit: str, _prop: str) -> str:
            raise subprocess.TimeoutExpired(cmd="systemctl", timeout=30)

        self.assertIsNone(unit_env.textfile_dir_for_env("sim", run=run))

    def test_a_unit_that_is_not_loaded_yields_None(self) -> None:
        run = _fake_run(payload=self._WITH_DIR, load_state="not-found")
        self.assertIsNone(unit_env.textfile_dir_for_env("live", run=run))

    def test_a_unit_without_the_key_yields_None(self) -> None:
        # The payload is a REAL one (the live rails), just without the metrics
        # dir — so this cannot pass by accident on an empty payload.
        self.assertIsNone(unit_env.textfile_dir_for_env("live", run=_fake_run()))

    def test_an_unknown_instance_is_refused_not_silently_None(self) -> None:
        with self.assertRaises(unit_env.UnitEnvError):
            unit_env.textfile_dir_for_env("staging", run=_fake_run())


class TestComposeLiveEnvironment(unittest.TestCase):
    def test_both_sources_land_in_the_composed_values(self) -> None:
        composed = _compose()
        self.assertEqual(composed.values["ALPHALENS_BROKER_MAX_OPEN"], "10")
        self.assertEqual(composed.values["ALPHALENS_BROKER_EXIT_POLICY"], "breakeven_trail")
        self.assertEqual(composed.values["SAXO_LIVE_APP_KEY"], "key-live")
        self.assertEqual(composed.unit, "alphalens-broker-manager-live.service")
        self.assertEqual(composed.env_file, Path(_ENV_FILE_PATH))
        self.assertEqual(composed.dropins, 2)
        self.assertFalse(composed.needs_daemon_reload)
        self.assertEqual(composed.warnings, [])

    def test_the_composed_set_satisfies_the_live_factory(self) -> None:
        # The factory needs the nine rails (assert_live_rails), the grant pair
        # and the three SAXO_LIVE_* auth vars before any I/O; the union of the
        # two sources is what makes `--env live` work in a plain shell.
        composed = _compose()
        for key in (
            "ALPHALENS_BROKER_MAX_OPEN",
            "ALPHALENS_BROKER_PORTFOLIO_GROSS_FRAC",
            "ALPHALENS_BROKER_DAILY_LOSS_LIMIT_R",
            "ALPHALENS_BROKER_SIZING_EQUITY",
            "ALPHALENS_BROKER_SIZING_EQUITY_MODE",
            "ALPHALENS_BROKER_EXIT_POLICY",
            "ALPHALENS_BROKER_MAX_FEE_BPS",
            "ALPHALENS_BROKER_ENTRY_TRAIL_BPS",
            "ALPHALENS_BROKER_ENTRY_WATCH_MAX_PICKS",
            "ALPHALENS_SAXO_LIVE_STANDING",
            "SAXO_LIVE_ACCOUNT_KEY",
            "SAXO_LIVE_APP_KEY",
            "SAXO_LIVE_APP_SECRET",
            "SAXO_LIVE_AUTH_REDIRECT_URL",
        ):
            with self.subTest(key=key):
                self.assertTrue(composed.values.get(key))

    def test_the_file_overrides_the_unit_the_way_systemd_does(self) -> None:
        # Verified on the host 2026-08-11 and recorded in the unit file:
        # EnvironmentFile= is applied AFTER every Environment= line.
        composed = unit_env.compose_live_environment(
            run=_fake_run(payload="ALPHALENS_TEXTFILE_DIR=/unit A=1"),
            read_text=lambda _path: "ALPHALENS_TEXTFILE_DIR=/file\n",
        )
        self.assertEqual(composed.values["ALPHALENS_TEXTFILE_DIR"], "/file")
        self.assertEqual(composed.values["A"], "1")

    def test_a_banned_key_in_the_file_warns_by_name_never_by_value(self) -> None:
        composed = unit_env.compose_live_environment(
            run=_fake_run(),
            read_text=lambda _path: "ALPHALENS_BROKER_MAX_OPEN=99\n",
        )
        self.assertEqual(composed.values["ALPHALENS_BROKER_MAX_OPEN"], "99")
        joined = " ".join(composed.warnings)
        self.assertIn("ALPHALENS_BROKER_MAX_OPEN", joined)
        self.assertIn(_ENV_FILE_PATH, joined)
        self.assertNotIn("99", joined)

    def test_the_grant_pair_in_the_file_also_warns(self) -> None:
        for key in ("ALPHALENS_SAXO_LIVE_STANDING", "SAXO_LIVE_ACCOUNT_KEY"):
            with self.subTest(key=key):
                composed = unit_env.compose_live_environment(
                    run=_fake_run(), read_text=lambda _path, k=key: f"{k}=leaked\n"
                )
                self.assertIn(key, " ".join(composed.warnings))
                self.assertNotIn("leaked", " ".join(composed.warnings))

    def test_a_pending_daemon_reload_warns(self) -> None:
        composed = _compose(needs_reload="yes")
        self.assertTrue(composed.needs_daemon_reload)
        self.assertTrue(any("daemon-reload" in w for w in composed.warnings))

    def test_a_missing_unit_is_refused_via_load_state_not_via_an_empty_payload(self) -> None:
        # Measured: systemctl answers a non-existent unit with an EMPTY
        # Environment payload and exit code 0, so emptiness cannot be the test.
        with self.assertRaises(unit_env.UnitEnvError) as caught:
            _compose(load_state="not-found", payload="")
        self.assertIn("not-found", str(caught.exception))
        self.assertIn("systemctl", str(caught.exception))

    def test_a_loaded_unit_with_no_environment_is_refused(self) -> None:
        with self.assertRaises(unit_env.UnitEnvError):
            _compose(payload="")

    def test_a_missing_systemctl_names_the_manual_recipe(self) -> None:
        with self.assertRaises(unit_env.UnitEnvError) as caught:
            _compose(missing=True)
        message = str(caught.exception)
        self.assertIn("/etc/alphalens/env", message)
        self.assertIn("systemctl", message)

    def test_an_unreadable_mandatory_file_is_refused_and_says_it_is_cli_side(self) -> None:
        def read_text(_path: Path) -> str:
            raise PermissionError("denied")

        with self.assertRaises(unit_env.UnitEnvError) as caught:
            unit_env.compose_live_environment(run=_fake_run(), read_text=read_text)
        message = str(caught.exception)
        self.assertIn(_ENV_FILE_PATH, message)
        # The daemon read the file at ITS start; a CLI-side read failure says
        # nothing about the running process, and the message must not imply it.
        self.assertIn("daemon", message)

    def test_an_unreadable_optional_file_is_skipped_like_systemd_does(self) -> None:
        def read_text(_path: Path) -> str:
            raise FileNotFoundError("gone")

        composed = unit_env.compose_live_environment(
            run=_fake_run(env_files="/tmp/opt (ignore_errors=yes)"), read_text=read_text
        )
        self.assertEqual(composed.values["ALPHALENS_BROKER_MAX_OPEN"], "10")
        self.assertIsNone(composed.env_file)

    def test_no_environment_file_at_all_still_composes_the_unit(self) -> None:
        composed = unit_env.compose_live_environment(
            run=_fake_run(env_files=""), read_text=lambda _path: ""
        )
        self.assertEqual(composed.values["ALPHALENS_BROKER_MAX_OPEN"], "10")
        self.assertIsNone(composed.env_file)


if __name__ == "__main__":
    unittest.main()
