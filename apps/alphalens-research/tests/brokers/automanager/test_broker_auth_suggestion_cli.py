"""``broker_auth`` carries ONE code for five causes, so its suggestion must fit
all of them (#1811).

The code is a CONTRACT code and stays one code: the five causes are a CLI
concept and splitting them in ``broker_contract`` would be the #1122 mistake
again. What changes is which argv is attached:

* an emitter that knows nothing about its cause gets the DEFAULT from
  ``_SUGGESTIONS_BY_CODE``, so the structural guarantee still holds — a
  ``retryable: false`` that promises a way out never comes out empty. That
  default is therefore a read-only store INSPECTION, not a re-bootstrap: with
  ``SAXO_APP_KEY`` unset the chain can be alive, and the old default sent the
  caller to replace a healthy one;
* the five sites that DO know their cause pass the bootstrap for their own
  rail, because ``broker auth`` bootstraps SIM and ``broker marketdata-auth``
  bootstraps LIVE market data, and naming the wrong one sends an operator at
  the wrong chain (2026-10-23: the LIVE chain died and the fix was
  ``marketdata-auth``).

Two of the three classes below drive the emitters with the output-format handle
set to ``json`` on purpose. ``auth`` and ``marketdata-auth`` take no
``--format`` (the group's rule), so their refusals render as prose and the
``suggestions`` tuple they build is discarded by the renderer. Asserting it
needs the handle, and the handle is the documented seam for exactly this.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

REGISTRY_SEAM = "alphalens_pipeline.brokers.registry.get_default_broker"

SIM_STATUS_ARGV = ["alphalens", "broker", "auth", "--status"]
LIVE_STATUS_ARGV = ["alphalens", "broker", "marketdata-auth", "--status"]
SIM_BOOTSTRAP_ARGV = ["alphalens", "broker", "auth"]
LIVE_BOOTSTRAP_ARGV = ["alphalens", "broker", "marketdata-auth"]


def _failure_on_stderr(stderr: str) -> dict:
    """The one JSON object on a stderr that also carries human chrome."""
    objects = []
    for line in stderr.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    assert len(objects) == 1, f"expected exactly one JSON line on stderr, got {objects}"
    return objects[0]


class _EmitterCase(unittest.TestCase):
    """Calls the two ``--status`` helpers directly, in JSON rendering mode.

    Direct calls rather than ``CliRunner``: these two commands have no
    ``--format``, so the Typer path can only ever show prose, and the object
    under test would never be rendered.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        home_patcher = mock.patch("pathlib.Path.home", return_value=self.home)
        home_patcher.start()
        self.addCleanup(home_patcher.stop)
        env_patcher = mock.patch.dict("os.environ", {}, clear=True)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

        from alphalens_cli.commands._cli_envelope import _FORMAT_JSON, _OUTPUT_FORMAT

        token = _OUTPUT_FORMAT.set(_FORMAT_JSON)
        self.addCleanup(_OUTPUT_FORMAT.reset, token)

    def emit(self, call) -> dict:
        """Run a failing emitter and return the failure object it published."""
        import typer

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(typer.Exit):
                call()
        return _failure_on_stderr(stderr.getvalue())


class ACauselessEmitterStillGetsARunnableRead(unittest.TestCase):
    """Cause 1: a missing env var, raised inside the token store and classified
    off the exception CLASS, so the call site says nothing about the cause."""

    def setUp(self) -> None:
        self.runner = CliRunner()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        home_patcher = mock.patch("pathlib.Path.home", return_value=self.home)
        home_patcher.start()
        self.addCleanup(home_patcher.stop)
        self.textfile_dir = self.home / "textfiles"
        self.textfile_dir.mkdir()
        env_patcher = mock.patch.dict(
            "os.environ", {"ALPHALENS_TEXTFILE_DIR": str(self.textfile_dir)}, clear=True
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _auth_failure(self) -> dict:
        from alphalens_cli.commands.broker import broker_app
        from alphalens_pipeline.brokers.saxo.errors import SaxoAuthError

        broker = mock.Mock()
        broker.get_account.side_effect = SaxoAuthError(
            "SAXO_APP_KEY environment variable not set — required for Saxo OAuth"
        )
        with mock.patch(REGISTRY_SEAM, return_value=broker):
            result = self.runner.invoke(broker_app, ["account", "--format", "json"])
        self.assertEqual(result.exit_code, 1)
        return _failure_on_stderr(result.stderr)

    def test_the_guarantee_holds_the_suggestion_is_not_empty(self) -> None:
        """The structural half: a ``retryable: false`` that promises a way out
        must not depend on this emitter having remembered one."""
        failure = self._auth_failure()

        self.assertEqual(failure["code"], "broker_auth")
        self.assertFalse(failure["retryable"])
        self.assertTrue(failure["suggestions"], "broker_auth must still carry an argv")
        self.assertTrue(all(s["argv"] for s in failure["suggestions"]))

    def test_it_is_handed_a_store_READ_and_never_a_re_bootstrap(self) -> None:
        """The bug: with ``SAXO_APP_KEY`` unset, ``broker auth --status`` in the
        same shell reports the refresh chain ALIVE. The old default sent the
        caller to re-bootstrap a healthy chain."""
        argvs = [s["argv"] for s in self._auth_failure()["suggestions"]]

        self.assertNotIn(SIM_BOOTSTRAP_ARGV, argvs)
        self.assertNotIn(LIVE_BOOTSTRAP_ARGV, argvs)
        self.assertTrue(all("--status" in argv for argv in argvs), argvs)

    def test_the_read_covers_both_rails_because_the_code_does(self) -> None:
        """One code spans the SIM chain and the LIVE market-data chain, and a
        cause-less emitter cannot tell which store is at fault — so both reads
        are offered rather than a sentence naming the second one."""
        argvs = [s["argv"] for s in self._auth_failure()["suggestions"]]

        self.assertEqual(argvs, [SIM_STATUS_ARGV, LIVE_STATUS_ARGV])


class TheSimRailNamesTheSimBootstrap(_EmitterCase):
    """Causes 2 and 3 — ``broker auth --status`` over the SIM store."""

    def _sim_status_failure(self, state) -> dict:
        from alphalens_cli.commands.broker import _auth_status

        store = mock.Mock()
        store.path = self.home / "token_store.json"
        store.load.return_value = state
        with mock.patch("alphalens_pipeline.brokers.saxo.tokens.TokenStore", return_value=store):
            return self.emit(_auth_status)

    def test_an_absent_store_points_at_the_sim_bootstrap(self) -> None:
        failure = self._sim_status_failure(None)

        self.assertEqual(failure["code"], "broker_auth")
        self.assertEqual([s["argv"] for s in failure["suggestions"]], [SIM_BOOTSTRAP_ARGV])

    def test_a_dead_sim_chain_points_at_the_sim_bootstrap(self) -> None:
        from alphalens_pipeline.brokers.saxo.tokens import TokenStoreState

        past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
        failure = self._sim_status_failure(
            TokenStoreState(
                environment="sim",
                app_key_fingerprint="deadbeef",
                access_token="x",
                access_token_expires_at=past,
                refresh_token="y",
                refresh_token_expires_at=past,
                obtained_at=past,
            )
        )

        self.assertEqual(failure["code"], "broker_auth")
        self.assertEqual([s["argv"] for s in failure["suggestions"]], [SIM_BOOTSTRAP_ARGV])

    def test_the_prose_names_the_SIM_rail_and_not_the_other_one(self) -> None:
        """The argv half of this fix was pinned; the ``why`` half was not, and a
        mutation proved it: swapping the two rails' prose left the whole
        automanager suite green while each refusal explained the OTHER rail.
        That is the same misnamed-rail defect #1811 exists to remove, so it is
        pinned on both halves. Literal phrases, never the production constant —
        a test that imports the string it asserts on cannot see a swap."""
        why = self._sim_status_failure(None)["suggestions"][0]["why"]

        self.assertIn("the SIM OAuth session is absent", why)
        self.assertNotIn("the LIVE market-data OAuth session is absent", why)


class TheLiveRailNamesTheLiveBootstrap(_EmitterCase):
    """Causes 4 and 5 — the operationally expensive ones. On 2026-10-23 the LIVE
    market-data chain died; the fix was ``broker marketdata-auth``, and a
    suggestion naming ``broker auth`` points at the SIM rail instead."""

    def _live_status_failure(self, status) -> dict:
        from alphalens_cli.commands.broker import _marketdata_auth_status

        with mock.patch(
            "alphalens_pipeline.data.alt_data.saxo_marketdata_auth.inspect_store",
            return_value=status,
        ):
            return self.emit(_marketdata_auth_status)

    def _status(self, **overrides):
        from alphalens_pipeline.data.alt_data.saxo_marketdata_auth import LiveTokenStatus

        past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
        fields = {
            "store_path": self.home / "live_token_store.json",
            "present": True,
            "access_expires_at": past,
            "access_valid": False,
            "refresh_present": True,
            "refresh_expires_at": past,
        }
        fields.update(overrides)
        return LiveTokenStatus(**fields)

    def test_an_absent_live_store_points_at_marketdata_auth(self) -> None:
        failure = self._live_status_failure(
            self._status(
                present=False,
                access_expires_at=None,
                refresh_present=False,
                refresh_expires_at=None,
            )
        )

        self.assertEqual(failure["code"], "broker_auth")
        self.assertEqual([s["argv"] for s in failure["suggestions"]], [LIVE_BOOTSTRAP_ARGV])

    def test_a_dead_live_chain_with_a_recorded_expiry_points_at_marketdata_auth(self) -> None:
        failure = self._live_status_failure(self._status())

        self.assertEqual([s["argv"] for s in failure["suggestions"]], [LIVE_BOOTSTRAP_ARGV])

    def test_a_legacy_live_store_without_a_refresh_token_points_at_marketdata_auth(self) -> None:
        """The third LIVE site: no recorded refresh expiry AND no refresh token,
        which is its own branch and had the same wrong argv."""
        failure = self._live_status_failure(
            self._status(refresh_expires_at=None, refresh_present=False)
        )

        self.assertEqual([s["argv"] for s in failure["suggestions"]], [LIVE_BOOTSTRAP_ARGV])

    def test_the_prose_names_the_LIVE_rail_and_not_the_other_one(self) -> None:
        """The LIVE twin of the SIM prose test. This is the rail whose misnaming
        cost a day on 2026-10-23, so its explanation must say LIVE even though
        the same sentence also mentions ``broker auth`` to warn against it."""
        why = self._live_status_failure(self._status())["suggestions"][0]["why"]

        self.assertIn("the LIVE market-data OAuth session is absent", why)
        self.assertNotIn("the SIM OAuth session is absent", why)


#: Every code in the merged contract + CLI registry that declares
#: ``needs_suggestion``, measured 2026-10-28 over
#: ``{**CONTRACT_FAILURE_CODES, **_CLI_FAILURE_CODES}``. Written out because the
#: loop below derives the same set by comprehension and would pass on an empty
#: one: a renamed ``needs_suggestion`` flag, or a registry that stopped merging,
#: makes the loop green over nothing.
CODES_DEMANDING_A_SUGGESTION = frozenset(
    {
        "broker_auth",
        "pick_already_armed",
        "pick_not_writable",
        "stream_metrics_missing",
        "write_outcome_unknown",
    }
)


def _codes_demanding_a_suggestion() -> set[str]:
    from alphalens_cli.commands._cli_envelope import _CLI_FAILURE_CODES
    from broker_contract.failure import CONTRACT_FAILURE_CODES

    return {
        name
        for name, entry in {**CONTRACT_FAILURE_CODES, **_CLI_FAILURE_CODES}.items()
        if entry.needs_suggestion
    }


class TheTableStillHoldsTheGuarantee(unittest.TestCase):
    """The comment on ``_SUGGESTIONS_BY_CODE`` is the requirement: the way out
    is held in the table, not at the call sites, so no emitter can forget."""

    def test_the_derived_set_is_the_one_that_was_measured(self) -> None:
        """Non-vacuity guard for the loop below."""
        self.assertEqual(_codes_demanding_a_suggestion(), set(CODES_DEMANDING_A_SUGGESTION))

    def test_every_code_that_demands_a_suggestion_has_one_in_the_table(self) -> None:
        from alphalens_cli.commands._cli_envelope import _SUGGESTIONS_BY_CODE

        demanding = _codes_demanding_a_suggestion()

        for name in demanding:
            with self.subTest(code=name):
                self.assertTrue(_SUGGESTIONS_BY_CODE.get(name), f"{name} has no default argv")
                self.assertTrue(all(s.argv for s in _SUGGESTIONS_BY_CODE[name]))

    def test_the_broker_auth_default_is_read_only(self) -> None:
        """A default is what an emitter that knows NOTHING publishes, so it must
        be true for all five causes. Only an offline store read is."""
        from alphalens_cli.commands._cli_envelope import _SUGGESTIONS_BY_CODE

        for suggestion in _SUGGESTIONS_BY_CODE["broker_auth"]:
            with self.subTest(argv=suggestion.argv):
                self.assertIn("--status", suggestion.argv)


if __name__ == "__main__":
    unittest.main()
