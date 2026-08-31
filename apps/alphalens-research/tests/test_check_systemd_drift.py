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


_GRANT_DROPIN = (
    "[Service]\n"
    "Environment=ALPHALENS_SAXO_LIVE_STANDING=opaque\n"
    "Environment=SAXO_LIVE_ACCOUNT_KEY=opaque\n"
)


class TestUnitRequirements(unittest.TestCase):
    """The requirement table itself, pinned against silent erasure.

    `main()` emits the grant gauge only for units that declare a requirement,
    so emptying the LIVE entry does not turn the gauge to 0 — it removes the
    SERIES. `AlphalensLiveGrantMissing` matches on `== 0` and there is no
    `absent()` companion, so the alert would simply stop being able to fire:
    the always-green rot this module exists to prevent, reachable by deleting
    two words. CI is the guard.
    """

    def test_the_live_unit_requires_exactly_the_grant_pair(self):
        required = {unit: req for unit, _base, req in drift.UNITS}
        self.assertEqual(required["alphalens-broker-manager-live"], drift.LIVE_GRANT_VARS)
        self.assertEqual(
            drift.LIVE_GRANT_VARS,
            frozenset({"ALPHALENS_SAXO_LIVE_STANDING", "SAXO_LIVE_ACCOUNT_KEY"}),
        )

    def test_only_units_that_build_a_live_client_require_a_grant(self):
        """The rule is "declares the grant iff it constructs a LIVE client",
        not "iff it is the daemon".

        This test used to say ONLY the LIVE daemon may require it, on the
        grounds that a requirement anywhere else "would emit a gauge that can
        only ever read 0". That reasoning holds for SIM and for the price
        reader — neither touches the grant — but it stopped being the whole
        rule when the capital reader (#1203) arrived: it places nothing, yet
        `create_saxo_broker_live_from_env` refuses it a client without the
        grant exactly as it refuses the daemon, so its gauge is meaningful and
        an explicit 0 there is the point.

        The positive control survives in the arm that matters: a unit that
        never builds a LIVE client must NOT declare a requirement, or the
        gauge really would be a permanent 0."""
        builds_live_client = {
            "alphalens-broker-manager-live",
            "alphalens-broker-capital-reader",
        }
        for unit, _base, required in drift.UNITS:
            if unit in builds_live_client:
                self.assertEqual(required, drift.LIVE_GRANT_VARS, unit)
            else:
                self.assertEqual(required, frozenset(), unit)


def _unwatched_units(
    service_names: set[str],
    units: tuple[tuple[str, str, frozenset[str]], ...],
    exempt: set[str],
) -> list[str]:
    """Service files that are neither in the UNITS table nor exempted.

    Pure so the positive controls can feed synthetic inputs — the gate's
    ability to fail is demonstrated, not assumed.
    """
    watched = {base for _unit, base, _req in units}
    return sorted(service_names - watched - exempt)


# Every tracked service that is deliberately NOT under the drift watch, with
# the reason (#1207). The watch exists for units whose host config is an arming
# or pricing surface (broker rails, ALLOW_ORDERS, the grant); the units below
# carry none of that, their config converges by `cp` at deploy, and a drift
# there breaks a measurable output rather than silently changing what real
# money does. Removing a name from here without adding a UNITS entry turns the
# gate red — the table cannot rot to always-green.
DRIFT_EXEMPT_UNITS: dict[str, str] = {
    "alphalens-bracket-cost.service": "measurement job; no broker rails or arming env",
    "alphalens-edgar-detect.service": "EDGAR poller; no broker env at all",
    "alphalens-edge-mirror.service": "Postgres cache rebuild wrapper; no broker env",
    "alphalens-feedback-shadow-returns.service": "population monitor; broker-free by ADR 0012",
    "alphalens-form4-backfill.service": "dormant one-shot seed; no broker env",
    "alphalens-form4-incremental.service": "SEC store top-up; no broker env",
    "alphalens-grafana-provisioning-sync.service": "dashboard sync; no broker env",
    "alphalens-grouped-daily-topup.service": "Polygon store top-up; no broker env",
    "alphalens-issue-wake.service": "issue-label timer; no broker env",
    "alphalens-literature-scan-monthly.service": "Perplexity scan; no broker env",
    "alphalens-literature-scan-weekly.service": "Perplexity scan; no broker env",
    "alphalens-prometheus-rules-sync.service": "rules sync; no broker env",
    "alphalens-saxo-marketdata-refresh.service": (
        "OAuth keep-alive; refreshes a token, carries no rails or arming env"
    ),
    "alphalens-saxo-refresh.service": (
        "OAuth keep-alive; refreshes a token, carries no rails or arming env"
    ),
    "alphalens-systemd-drift-check.service": "the checker itself; watching it here is circular",
    "alphalens-thematic-build.service": "Docker pipeline wrapper; no broker env",
}


class TestUnitsTableCompleteness(unittest.TestCase):
    """A new `.service` cannot merge without a UNITS entry or a documented
    exemption (#1207).

    The two sibling convention gates (metrics-hook completeness, staleness
    rule parity) already glob `deploy/systemd/*.service` and force every new
    unit into their registries; the drift table was the outlier — the capital
    reader (#1204) shipped without an entry and nothing flagged it until a
    review pass added one by hand.
    """

    @staticmethod
    def _service_names() -> set[str]:
        systemd_dir = drift.REPO_ROOT / drift.SYSTEMD_DIR
        return {p.name for p in systemd_dir.glob("alphalens-*.service")}

    def test_glob_finds_service_units(self):
        # Anti-vacuous guard: an empty glob (moved directory, renamed prefix)
        # would make every assertion below pass over nothing.
        self.assertGreater(len(self._service_names()), 0)

    def test_exempt_units_correspond_to_real_service_files(self):
        ghosts = set(DRIFT_EXEMPT_UNITS) - self._service_names()
        self.assertEqual(
            ghosts,
            set(),
            "exemptions for units that no longer exist — delete these entries",
        )

    def test_exempt_units_are_not_in_the_units_table(self):
        # Bidirectional honesty (the parity gate's pattern): an entry that is
        # both watched and exempted means one of the two statements is stale.
        watched = {base for _unit, base, _req in drift.UNITS}
        self.assertEqual(set(DRIFT_EXEMPT_UNITS) & watched, set())

    def test_every_service_unit_is_watched_or_exempt(self):
        unwatched = _unwatched_units(self._service_names(), drift.UNITS, set(DRIFT_EXEMPT_UNITS))
        self.assertEqual(
            unwatched,
            [],
            "new unit(s) without drift coverage — add a UNITS entry in "
            "check_systemd_drift.py, or a documented exemption in "
            "DRIFT_EXEMPT_UNITS here",
        )


class TestUnitsTableCompletenessPositiveControls(unittest.TestCase):
    """The gate can actually fail — over synthetic inputs, not the live tree."""

    _UNITS = (("u", "u.service", frozenset()),)

    def test_a_service_missing_from_both_registries_is_flagged(self):
        self.assertEqual(
            _unwatched_units({"u.service", "new.service"}, self._UNITS, set()),
            ["new.service"],
        )

    def test_an_exemption_clears_the_flag(self):
        self.assertEqual(
            _unwatched_units({"u.service", "new.service"}, self._UNITS, {"new.service"}),
            [],
        )


class TestHostOnlyGrantDropinPredicate(unittest.TestCase):
    """What makes an UNTRACKED host drop-in acceptable (#1193).

    A content contract, not a basename allowlist. The file is acceptable only
    when it does nothing but assign host-only vars — the same rule
    :func:`strip_host_only_environment_lines` already applies to the base
    unit, extended to one more place. A name-based allowlist would let any
    future file called ``99-live-grant.conf`` govern the daemon unseen, which
    is the #1136 lesson this module exists to keep.
    """

    def _is(self, text: str) -> bool:
        return drift.is_host_only_grant_dropin(text, drift.LIVE_GRANT_VARS)

    def test_accepts_a_file_that_only_assigns_host_only_vars(self):
        self.assertTrue(self._is(_GRANT_DROPIN))

    def test_accepts_comments_and_blank_lines_around_them(self):
        self.assertTrue(self._is("# operator-local\n\n" + _GRANT_DROPIN))

    def test_rejects_a_file_that_also_assigns_a_governed_var(self):
        self.assertFalse(self._is(_GRANT_DROPIN + "Environment=ALPHALENS_BROKER_MAX_OPEN=9\n"))

    def test_rejects_a_file_carrying_any_other_directive(self):
        # The dangerous shape: grant-only Environment= lines PLUS an
        # ExecStart= override. Judging by the variables alone would wave it
        # through.
        self.assertFalse(self._is(_GRANT_DROPIN + "ExecStart=/bin/evil\n"))

    def test_rejects_a_file_that_assigns_nothing(self):
        self.assertFalse(self._is("[Service]\n# nothing here\n"))


class TestGrantPresence(unittest.TestCase):
    """The INVERTED assertion (#1193).

    Every other signal in this module compares the host against the repo. The
    ADR 0017 grant is host-only by construction, so that comparison is
    structurally blind to it: wiping the grant off the host makes the two
    sides agree PERFECTLY and the check reports converged. Measured on the
    real blob before this existed — a fully wiped LIVE host produced zero
    findings, twice in production (2026-08-25, 2026-08-28).

    Only an assertion in the opposite direction — these names must be
    PRESENT — can see it.
    """

    def _findings(self, host_env, live_env, required=None):
        return drift.grant_findings(
            "alphalens-broker-manager-live",
            host_env=host_env,
            live_env=live_env,
            required=drift.LIVE_GRANT_VARS if required is None else required,
        )

    def test_a_wiped_host_reports_one_finding_per_grant_name(self):
        findings = self._findings(host_env={"ALPHALENS_BROKER_ENVIRONMENT": "live"}, live_env={})
        self.assertEqual(
            [(f.kind, f.subject) for f in findings],
            [
                ("missing_grant", "ALPHALENS_SAXO_LIVE_STANDING"),
                ("missing_grant", "SAXO_LIVE_ACCOUNT_KEY"),
            ],
        )

    def test_a_wipe_before_daemon_reload_is_still_reported(self):
        # The pre-failure window: the unit file has lost the grant but systemd
        # still holds the environment it loaded earlier, so the daemon trades
        # on and only the NEXT start breaks. The host-side half is the only
        # thing that can see this, and it is the half that matters.
        findings = self._findings(host_env={}, live_env=dict.fromkeys(drift.LIVE_GRANT_VARS, "x"))
        self.assertEqual(len(findings), 2)
        for f in findings:
            self.assertIn("host unit configuration", f.detail)
            self.assertNotIn("systemd-loaded", f.detail)

    def test_the_grant_in_the_base_unit_is_a_clean_bill(self):
        # Positive control: the layout production runs TODAY must not alert.
        env = dict.fromkeys(drift.LIVE_GRANT_VARS, "opaque")
        self.assertEqual(self._findings(host_env=env, live_env=dict(env)), [])

    def test_the_grant_in_a_dropin_is_equally_clean(self):
        # The check asserts PRESENCE, not location — that is what lets the
        # host migrate from the base unit to the drop-in without a window in
        # which it alerts.
        composed = drift.composed_environment(
            "[Service]\nEnvironment=ALPHALENS_BROKER_ENVIRONMENT=live\n",
            [("99-live-grant.conf", _GRANT_DROPIN)],
        )
        self.assertEqual(self._findings(host_env=composed, live_env=dict(composed)), [])

    def test_a_unit_requiring_nothing_never_reports(self):
        self.assertEqual(self._findings(host_env={}, live_env={}, required=frozenset()), [])

    def test_an_unreadable_loaded_environment_still_judges_the_host_side(self):
        # live_env=None means systemctl rendered a form the narrow parser
        # refuses. That must not silence the file half — the file half is the
        # one that predicts the next restart.
        env = dict.fromkeys(drift.LIVE_GRANT_VARS, "opaque")
        self.assertEqual(self._findings(host_env=env, live_env=None), [])
        self.assertEqual(len(self._findings(host_env={}, live_env=None)), 2)

    def test_an_empty_value_counts_as_absent(self):
        # `Environment=SAXO_LIVE_ACCOUNT_KEY=` sets the name to the empty
        # string, so a presence-by-name check would report a healthy grant.
        # The daemon disagrees: `_standing_grant_valid` requires present AND
        # non-empty AND equal, so it would refuse at the next start while this
        # gauge said 1 — a false green on the exact question it answers.
        env = dict.fromkeys(drift.LIVE_GRANT_VARS, "")
        self.assertEqual(len(self._findings(host_env=env, live_env=dict(env))), 2)

    def test_findings_never_carry_the_grant_value(self):
        findings = self._findings(
            host_env={"ALPHALENS_SAXO_LIVE_STANDING": "opaque-value"},
            live_env={"ALPHALENS_SAXO_LIVE_STANDING": "opaque-value"},
        )
        self.assertTrue(findings)
        for f in findings:
            self.assertNotIn("opaque-value", f.detail)


class TestDropinTexts(unittest.TestCase):
    def test_only_conf_files_feed_env_composition(self):
        # `.timer` (and README.md) entries are compared as FILES but must
        # never be composed as configuration — [Timer] has no Environment=
        # semantics and a stray parse there would fabricate env drift.
        files = {
            "u.service": _BASE,
            "u.timer": "[Timer]\nOnCalendar=*:00/15\n",
            "README.md": "prose",
            "10-a.conf": _DROPIN_A,
        }
        self.assertEqual(drift._dropin_texts(files), [("10-a.conf", _DROPIN_A)])


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

    def test_a_drifted_timer_reports_content_drift_on_raw_bytes(self):
        # A timer never carries Environment=, so it takes the raw-bytes branch
        # (no host-only stripping) — the #1206 scheduling-stanza edit is
        # exactly a byte difference.
        repo = {
            "alphalens-broker-manager.service": _BASE,
            "alphalens-broker-manager.timer": "[Timer]\nOnCalendar=*:00/15\n",
        }
        host = dict(repo)
        host["alphalens-broker-manager.timer"] = "[Timer]\nOnUnitActiveSec=15min\n"
        findings = self._findings(repo_files=repo, host_files=host)
        self.assertEqual(
            [(f.kind, f.subject) for f in findings],
            [("content_drift", "alphalens-broker-manager.timer")],
        )

    def test_a_tracked_timer_absent_on_the_host_is_a_missing_file(self):
        # The never-installed-timer state: #1206's dormancy would have started
        # here had the timer file not been copied at all.
        repo = {
            "alphalens-broker-manager.service": _BASE,
            "alphalens-broker-manager.timer": "[Timer]\nOnCalendar=*:00/15\n",
        }
        host = {"alphalens-broker-manager.service": _BASE}
        findings = self._findings(repo_files=repo, host_files=host)
        self.assertEqual(
            [(f.kind, f.subject) for f in findings],
            [("missing_file", "alphalens-broker-manager.timer")],
        )

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

    def test_a_grant_only_untracked_dropin_is_expected_state(self):
        # #1193: the grant moves OUT of the base unit into an untracked
        # drop-in a unit-file `cp` cannot reach. Reporting it as untracked
        # would trade a silent failure for a permanent false alert.
        findings = self._findings(
            host_files={
                "alphalens-broker-manager.service": _BASE,
                "10-a.conf": _DROPIN_A,
                "99-live-grant.conf": _GRANT_DROPIN,
            },
            host_only_vars=drift.LIVE_GRANT_VARS,
        )
        self.assertEqual(findings, [])

    def test_an_untracked_dropin_that_governs_anything_else_is_still_flagged(self):
        # Positive control for the tolerance: it is a content contract, so a
        # file that ALSO sets a governed rail, or overrides ExecStart, keeps
        # flagging exactly as before.
        for suffix, why in (
            ("Environment=ALPHALENS_BROKER_MAX_OPEN=9\n", "extra variable"),
            ("ExecStart=/bin/evil\n", "other directive"),
        ):
            with self.subTest(case=why):
                findings = self._findings(
                    host_files={
                        "alphalens-broker-manager.service": _BASE,
                        "10-a.conf": _DROPIN_A,
                        "99-live-grant.conf": _GRANT_DROPIN + suffix,
                    },
                    host_only_vars=drift.LIVE_GRANT_VARS,
                )
                self.assertIn(
                    ("untracked_file", "99-live-grant.conf"),
                    [(f.kind, f.subject) for f in findings],
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

    def test_renders_the_grant_gauge_only_for_units_that_require_one(self):
        # A `1` for the SIM daemon would be meaningless — it needs no grant.
        # An explicit 0 for the unit that DOES is the whole point: an absent
        # series must mean "broken emitter", never "the grant is fine".
        text = drift.render_grant_metrics({"alphalens-broker-manager-live": False})
        self.assertIn(
            'alphalens_systemd_live_grant_present{unit="alphalens-broker-manager-live"} 0',
            text,
        )
        self.assertNotIn('alphalens-broker-manager"', text)
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

    def test_a_sibling_timer_on_the_host_is_read_alongside_the_base(self):
        # #1207: the #1206 dormancy was a scheduling-stanza problem, and the
        # check could not see it — timers were simply never collected.
        with tempfile.TemporaryDirectory() as tmp:
            host = Path(tmp)
            (host / "u.service").write_text("base")
            (host / "u.timer").write_text("[Timer]\nOnCalendar=*:00/15\n")
            with mock.patch.object(drift, "HOST_UNIT_DIR", host):
                files = drift._host_files("u.service")
        self.assertEqual(
            files,
            {"u.service": "base", "u.timer": "[Timer]\nOnCalendar=*:00/15\n"},
        )


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

    def test_a_sibling_timer_in_the_tree_is_read_alongside_the_base(self):
        # #1207: from the parent listing the builder already holds — no extra
        # git call is spent learning whether the timer exists.
        def fake_run(argv, timeout=120):
            if argv[1] == "ls-tree":
                if argv[-1].endswith(".d"):
                    return "10-a.conf\n"
                return "u.service\nu.service.d\nu.timer\n"
            return f"blob:{argv[-1]}"

        with mock.patch.object(drift, "_run", side_effect=fake_run):
            files = drift._repo_files("u.service")
        self.assertEqual(
            files,
            {
                "u.service": "blob:origin/main:deploy/systemd/u.service",
                "u.timer": "blob:origin/main:deploy/systemd/u.timer",
                "10-a.conf": "blob:origin/main:deploy/systemd/u.service.d/10-a.conf",
            },
        )

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

    def _run_main(
        self,
        fetch_raises=False,
        repo_raises=False,
        drifted=False,
        grant_wiped=False,
        host_base_absent=False,
    ):
        base = "[Service]\nEnvironment=ALPHALENS_BROKER_ENVIRONMENT=sim\n"

        def fake_host_files(base_name):
            if host_base_absent:
                return {}
            files = {base_name: base}
            # The LIVE host carries the ADR 0017 grant; the repo blob never
            # does, which is exactly why the file comparison cannot see it go.
            if not grant_wiped and base_name.endswith("-live.service"):
                files[base_name] = base + _GRANT_DROPIN.removeprefix("[Service]\n")
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
                loaded = "Environment=ALPHALENS_BROKER_ENVIRONMENT=sim"
                if not grant_wiped and argv[3].endswith("-live"):
                    loaded += " " + " ".join(
                        f"{var}=opaque" for var in sorted(drift.LIVE_GRANT_VARS)
                    )
                return loaded + "\n"
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
        for unit, _base, _required in drift.UNITS:
            self.assertIn(f'alphalens_systemd_drift_findings{{unit="{unit}"}} 0', metrics)
        self.assertIn(
            'alphalens_systemd_live_grant_present{unit="alphalens-broker-manager-live"} 1',
            metrics,
        )

    def test_a_wiped_grant_leaves_the_drift_gauge_at_zero_and_drops_the_grant_gauge(self):
        # The finding this whole change exists for. The drift gauge staying 0
        # is not a bug — the host genuinely matches origin/main. That is why
        # the grant needs a gauge and an alert of its OWN: the drift alert's
        # remedy ("reinstall the tracked files") is what CAUSES this.
        code, metrics = self._run_main(grant_wiped=True)
        self.assertEqual(code, 0)
        self.assertIn(
            'alphalens_systemd_drift_findings{unit="alphalens-broker-manager-live"} 0', metrics
        )
        self.assertIn(
            'alphalens_systemd_live_grant_present{unit="alphalens-broker-manager-live"} 0',
            metrics,
        )

    def test_a_host_with_no_installed_unit_reports_rather_than_crashing(self):
        # Composing the host environment from a base unit the host does not
        # have would raise KeyError, which main() does not catch — the job
        # would die with a traceback instead of reporting missing_file.
        code, metrics = self._run_main(host_base_absent=True)
        self.assertEqual(code, 0)
        self.assertIn(
            'alphalens_systemd_live_grant_present{unit="alphalens-broker-manager-live"} 0',
            metrics,
        )

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
