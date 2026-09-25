"""Pin WHAT ``postdeploy_check.sh`` check 1 compares the live Prometheus rules to.

The live rules file is converged from the ``origin/main`` BLOB by
``alphalens-prometheus-rules-sync.service``. The postdeploy gate used to compare
it against the LOCAL WORKING TREE instead, which is a different reference: the
VPS checkout is routinely behind ``origin/main`` (three commits on 2026-09-24),
and in that state the gate reported drift on a live file that was already
correct, with a remedy telling the operator to copy the stale checkout file over
it (#1564).

Four arms below RUN the script against a fixture repository, because a text pin
cannot tell a correct comparison from a correct-looking one. The rest are static
pins on the constants the remedy text and the comparison depend on.

Honest limits: the static arms are text matches, and the suite says nothing
about the real VPS — that ``LIVE_RULES`` is the file actually bind-mounted into
the prometheus container, that the sync unit is installed with its timer
enabled, or that ``promtool`` exists in the container. Those stay operator facts,
checked by running the script on the host.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import re
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "postdeploy_check.sh"
SYNC_SCRIPT = REPO_ROOT / "apps" / "alphalens-research" / "scripts" / "sync_prometheus_rules.py"
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
RULES_SYNC_TIMER = SYSTEMD_DIR / "alphalens-prometheus-rules-sync.timer"

ORIGIN_BLOB_CONTENT = "groups: [ORIGIN_MAIN_BLOB]\n"
STALE_WORKING_TREE_CONTENT = "groups: [STALE_WORKING_TREE]\n"

# Every check-1 failure message begins with one of these. Used to assert that a
# WARN arm emitted no check-1 FAIL and contributed no problem bullet.
_CHECK1_FAILURE_PREFIXES = (
    "live rules",
    "cannot read origin/main",
    "origin/main has no",
)

# A copy instruction aimed at the live rules path. "cp " and "copy '" catch the
# old remedy line; the redirect catches a rewrite of the live file in place.
_COPY_INSTRUCTIONS = ("cp ", "copy '", '> "$LIVE_RULES"', '>"$LIVE_RULES"')


def _copy_instruction_in(text: str) -> str | None:
    """The first copy-into-the-live-rules instruction found in ``text``."""
    for needle in _COPY_INSTRUCTIONS:
        if needle in text:
            return needle
    return None


def _mentions_working_tree_rules_path(text: str) -> bool:
    """Whether ``text`` names the working-tree rules path the gate must not use.

    The real pin and its positive control BOTH go through this helper. An
    inline copy in each would let the real pin rot to a needle that can never
    match while the control kept passing against its own hand-written string.
    """
    return "REPO_RULES" in text


def _stub_systemctl(*, exit_status: str, exit_timestamp: str) -> str:
    """A ``systemctl`` stand-in printing one unit's last-run properties.

    The real command answers ``systemctl --user show <unit> -p <prop>...`` with
    one ``Key=Value`` line per property. A unit that has never run reports an
    EMPTY ``ExecMainExitTimestamp`` (verified on the VPS), which is the shape
    ``exit_timestamp=""`` reproduces.
    """
    return (
        "#!/bin/sh\n"
        f"printf 'ExecMainStatus={exit_status}\\n'\n"
        f"printf 'ExecMainExitTimestamp={exit_timestamp}\\n'\n"
    )


# GNU date, which is what the VPS runs, resolves `-d @<epoch>` to that epoch.
# BSD date on a developer laptop does not, so the suite supplies this shim to
# reach the branches that read the sync unit's exit time. Everything else is
# forwarded to the real binary.
_STUB_GNU_DATE = """#!/bin/sh
if [ "$1" = "-d" ]; then
  case "$2" in
    @*) printf '%s\\n' "${2#@}"; exit 0 ;;
    *) exit 1 ;;
  esac
fi
exec /bin/date "$@"
"""

_STUB_FAILING_MKTEMP = "#!/bin/sh\nexit 1\n"


def _load_sync_module():
    """Import sync_prometheus_rules.py by path — scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("_sync_prometheus_rules", SYNC_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_text() -> str:
    return SCRIPT.read_text()


def _parse_assignment(name: str) -> str:
    """Value of a plain ``NAME=value`` assignment in the script, quotes optional."""
    match = re.search(rf'^{re.escape(name)}="?([^"\n]*?)"?$', _script_text(), re.MULTILINE)
    if match is None:
        raise AssertionError(
            f'{name}="..." not found in postdeploy_check.sh — the assignment shape '
            "changed; update the parser rather than deleting the pin."
        )
    return match.group(1)


def _parse_live_rules_default() -> str:
    """Default of ``LIVE_RULES="${LIVE_RULES:-<default>}"``."""
    match = re.search(r'^LIVE_RULES="\$\{LIVE_RULES:-([^}]+)\}"$', _script_text(), re.MULTILINE)
    if match is None:
        raise AssertionError(
            'LIVE_RULES="${LIVE_RULES:-...}" not found in postdeploy_check.sh — '
            "the assignment shape changed; update the parser."
        )
    return match.group(1)


@dataclass(frozen=True)
class ScriptRun:
    """One execution of postdeploy_check.sh against a fixture repository."""

    stdout: str
    stderr: str

    @property
    def lines(self) -> list[str]:
        return self.stdout.splitlines()

    def check1_failures(self) -> list[str]:
        """FAIL lines emitted by check 1 (by message prefix)."""
        out = []
        for line in self.lines:
            if not line.startswith("FAIL "):
                continue
            message = line[len("FAIL ") :]
            if message.startswith(_CHECK1_FAILURE_PREFIXES):
                out.append(message)
        return out

    def problem_bullets(self) -> list[str]:
        """The ``  - <message>`` bullets printed after the FAIL summary line."""
        return [line[4:] for line in self.lines if line.startswith("  - ")]

    def has_line_starting(self, prefix: str) -> bool:
        return any(line.startswith(prefix) for line in self.lines)


def _git(repo: Path, *args: str, env: dict[str, str]) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )


def _fixture_env(home: Path) -> dict[str, str]:
    """Git env that is immune to this machine's global config.

    ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` are load-bearing: a global
    ``core.hooksPath`` pre-commit hook on the developer machine rejects commits
    made outside ``~/Developer``, which would fail every fixture commit.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }


def _run_fixture(
    *,
    rules_commit_age_seconds: int,
    live_content: str,
    working_tree_content: str,
    delete_origin: bool = False,
    merged_from_a_branch: bool = False,
    stubs: dict[str, str] | None = None,
) -> ScriptRun:
    """Build a fixture repo + live rules file and run the script against them.

    The fixture deliberately contains NO django image-trigger path, so check 2
    resolves an empty expected commit and never reaches out to GHCR.

    ``merged_from_a_branch`` commits the rules on a side branch at the requested
    age and merges it into main with a true merge commit AT RUN TIME, which is
    how the change's own date and the date it reached main come apart.

    ``stubs`` maps a command name to a script body; the directory holding them
    goes to the FRONT of ``PATH``, which is how a test without systemd reaches
    the branches that read the sync unit.
    """
    sync = _load_sync_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home"
        home.mkdir()
        env = _fixture_env(home)

        origin = root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(origin)],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
        clone = root / "clone"
        subprocess.run(
            ["git", "clone", str(origin), str(clone)],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )

        rules_file = clone / sync.RULES_REPO_PATH
        rules_file.parent.mkdir(parents=True, exist_ok=True)

        stamp = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=rules_commit_age_seconds)
        commit_env = dict(env)
        iso = stamp.strftime("%Y-%m-%dT%H:%M:%S+0000")
        commit_env["GIT_AUTHOR_DATE"] = iso
        commit_env["GIT_COMMITTER_DATE"] = iso

        if merged_from_a_branch:
            (clone / "seed.txt").write_text("seed\n")
            _git(clone, "add", "-A", env=env)
            _git(clone, "commit", "-m", "seed", env=env)
            _git(clone, "checkout", "-b", "rules-branch", env=env)
            rules_file.write_text(ORIGIN_BLOB_CONTENT)
            _git(clone, "add", "-A", env=env)
            _git(clone, "commit", "-m", "rules on a long-lived branch", env=commit_env)
            _git(clone, "checkout", "main", env=env)
            _git(clone, "merge", "--no-ff", "-m", "merge the rules branch", "rules-branch", env=env)
        else:
            rules_file.write_text(ORIGIN_BLOB_CONTENT)
            _git(clone, "add", "-A", env=env)
            _git(clone, "commit", "-m", "add rules", env=commit_env)
        _git(clone, "push", "origin", "main", env=env)

        if delete_origin:
            subprocess.run(["rm", "-rf", str(origin)], check=True, capture_output=True)

        rules_file.write_text(working_tree_content)
        live = root / "live.rules"
        live.write_text(live_content)

        run_env = dict(env)
        run_env["REPO"] = str(clone)
        run_env["LIVE_RULES"] = str(live)
        run_env["PROM_CONTAINER"] = "no-such-container-postdeploy-fixture"
        if stubs:
            stub_dir = root / "stubs"
            stub_dir.mkdir()
            for name, body in stubs.items():
                stub = stub_dir / name
                stub.write_text(body)
                stub.chmod(0o755)
            run_env["PATH"] = f"{stub_dir}:{run_env['PATH']}"
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            env=run_env,
            capture_output=True,
            text=True,
            cwd=str(root),
        )
        return ScriptRun(stdout=proc.stdout, stderr=proc.stderr)


class TestCheckOneComparesAgainstOriginMain(unittest.TestCase):
    """The four behavioural arms: the script is executed, not read."""

    def test_live_equal_to_the_origin_main_blob_passes_while_the_working_tree_differs(
        self,
    ) -> None:
        run = _run_fixture(
            rules_commit_age_seconds=0,
            live_content=ORIGIN_BLOB_CONTENT,
            working_tree_content=STALE_WORKING_TREE_CONTENT,
        )
        self.assertEqual(
            [],
            run.check1_failures(),
            "live matches the origin/main blob, so check 1 must not fail. It did, "
            f"which means it still consults the working tree.\n{run.stdout}",
        )
        self.assertTrue(
            run.has_line_starting("OK   live rules match origin/main"),
            f"expected an OK line naming origin/main.\n{run.stdout}",
        )

    def test_live_equal_to_a_stale_working_tree_fails_once_the_grace_window_has_passed(
        self,
    ) -> None:
        run = _run_fixture(
            rules_commit_age_seconds=3 * 3600,
            live_content=STALE_WORKING_TREE_CONTENT,
            working_tree_content=STALE_WORKING_TREE_CONTENT,
        )
        self.assertFalse(
            run.has_line_starting("OK   live rules"),
            "live differs from the origin/main blob, so check 1 must not pass — "
            f"agreeing with the working tree is not agreement with main.\n{run.stdout}",
        )
        self.assertFalse(
            run.has_line_starting("WARN live rules"),
            f"the rules commit is 3h old, past the grace window — WARN is wrong.\n{run.stdout}",
        )
        self.assertTrue(
            run.has_line_starting("FAIL live rules do not match origin/main"),
            f"expected a FAIL naming origin/main.\n{run.stdout}",
        )

    def test_a_rules_commit_younger_than_the_grace_window_warns_and_does_not_block_the_gate(
        self,
    ) -> None:
        run = _run_fixture(
            rules_commit_age_seconds=0,
            live_content=STALE_WORKING_TREE_CONTENT,
            working_tree_content=STALE_WORKING_TREE_CONTENT,
        )
        self.assertEqual(
            [],
            run.check1_failures(),
            "a difference younger than one sync cadence is pending convergence, "
            f"not drift — it must not fail the gate.\n{run.stdout}",
        )
        blocking = [b for b in run.problem_bullets() if b.startswith(_CHECK1_FAILURE_PREFIXES)]
        self.assertEqual(
            [],
            blocking,
            f"check 1 contributed a problem bullet while only warning.\n{run.stdout}",
        )
        self.assertTrue(
            run.has_line_starting("WARN live rules are behind origin/main"),
            f"expected a WARN explaining the pending sync.\n{run.stdout}",
        )

    def test_unreadable_origin_main_fails_and_never_falls_back_to_the_working_tree(self) -> None:
        run = _run_fixture(
            rules_commit_age_seconds=0,
            live_content=STALE_WORKING_TREE_CONTENT,
            working_tree_content=STALE_WORKING_TREE_CONTENT,
            delete_origin=True,
        )
        self.assertFalse(
            run.has_line_starting("OK   live rules"),
            "origin/main is unreadable, so check 1 established nothing — agreement "
            f"with the local checkout must not be reported as a pass.\n{run.stdout}",
        )
        self.assertFalse(
            run.has_line_starting("WARN live rules"),
            f"an unreadable origin/main is a hard fail, not a warning.\n{run.stdout}",
        )
        self.assertTrue(
            run.has_line_starting("FAIL cannot read origin/main"),
            f"expected a FAIL naming the unreadable origin/main.\n{run.stdout}",
        )


class TestTheGraceArmAsksWhetherTheSyncActuallyRan(unittest.TestCase):
    """The grace window must not be decided by the rules commit's age alone.

    A sync that already completed a run after the change and left the
    difference in place is drift, however young the commit is — and the worst
    shape of that is a live file which is empty or months old, which the
    age-only arm reported as "there is nothing to do".
    """

    GRACE_AGE_S = 600

    def _now(self) -> int:
        return int(dt.datetime.now(dt.UTC).timestamp())

    def test_a_sync_run_completed_after_the_rules_commit_is_drift_not_a_pending_copy(
        self,
    ) -> None:
        run = _run_fixture(
            rules_commit_age_seconds=self.GRACE_AGE_S,
            live_content="",  # the live file a partial write or a bad hand-edit leaves
            working_tree_content=STALE_WORKING_TREE_CONTENT,
            stubs={
                "systemctl": _stub_systemctl(exit_status="0", exit_timestamp=f"@{self._now()}"),
                "date": _STUB_GNU_DATE,
            },
        )
        self.assertFalse(
            run.has_line_starting("WARN live rules"),
            "the sync ran after the change and the file still differs, so this is "
            f"drift; a WARN reports it as a pending copy.\n{run.stdout}",
        )
        self.assertTrue(
            run.has_line_starting("FAIL live rules do not match origin/main"),
            f"expected check 1 to fail on drift the sync has already had a go at.\n{run.stdout}",
        )

    def test_no_sync_run_since_the_rules_commit_still_warns_inside_the_grace_window(
        self,
    ) -> None:
        older_than_the_commit = self._now() - self.GRACE_AGE_S - 600
        run = _run_fixture(
            rules_commit_age_seconds=self.GRACE_AGE_S,
            live_content=STALE_WORKING_TREE_CONTENT,
            working_tree_content=STALE_WORKING_TREE_CONTENT,
            stubs={
                "systemctl": _stub_systemctl(
                    exit_status="0", exit_timestamp=f"@{older_than_the_commit}"
                ),
                "date": _STUB_GNU_DATE,
            },
        )
        self.assertEqual(
            [],
            run.check1_failures(),
            "the sync has not run since the change, so the difference is pending "
            f"convergence and must not fail the gate.\n{run.stdout}",
        )
        self.assertTrue(
            run.has_line_starting("WARN live rules are behind origin/main"),
            f"expected a WARN explaining the pending sync.\n{run.stdout}",
        )

    def test_the_warn_arm_prints_the_difference_and_claims_nothing_about_the_live_file(
        self,
    ) -> None:
        run = _run_fixture(
            rules_commit_age_seconds=self.GRACE_AGE_S,
            live_content=STALE_WORKING_TREE_CONTENT,
            working_tree_content=STALE_WORKING_TREE_CONTENT,
        )
        self.assertTrue(
            run.has_line_starting("WARN live rules are behind origin/main"),
            f"expected the WARN arm to be the one under test.\n{run.stdout}",
        )
        self.assertNotIn(
            "There is nothing to do",
            run.stdout,
            "the gate never reads the sync unit's result in this arm, so it cannot "
            "promise that. An empty or months-old live file reaches the same arm.",
        )
        self.assertTrue(
            run.has_line_starting("--- origin/main") and run.has_line_starting("+++ live"),
            "the WARN arm must show WHAT differs; without it a corrupt live file "
            f"looks exactly like a one-minute-old pending copy.\n{run.stdout}",
        )


class TestTheRulesChangeIsDatedWhenItReachedMain(unittest.TestCase):
    def test_a_change_merged_now_from_an_old_branch_commit_is_not_reported_as_overdue(
        self,
    ) -> None:
        run = _run_fixture(
            rules_commit_age_seconds=3 * 86400,
            live_content=STALE_WORKING_TREE_CONTENT,
            working_tree_content=STALE_WORKING_TREE_CONTENT,
            merged_from_a_branch=True,
        )
        self.assertEqual(
            [],
            run.check1_failures(),
            "the change reached main seconds ago, so the sync has legitimately not "
            "copied it yet. Dating it by the branch commit makes the gate fail a "
            f"host that is behaving correctly.\n{run.stdout}",
        )


class TestCheckOneFailsHonestlyWhenItCannotRun(unittest.TestCase):
    def test_a_failing_mktemp_is_named_rather_than_blamed_on_a_moved_rules_path(self) -> None:
        run = _run_fixture(
            rules_commit_age_seconds=600,
            live_content=ORIGIN_BLOB_CONTENT,
            working_tree_content=STALE_WORKING_TREE_CONTENT,
            stubs={"mktemp": _STUB_FAILING_MKTEMP},
        )
        self.assertFalse(
            any("did the rules file move" in message for message in run.check1_failures()),
            "a temp file that could not be created was reported as a moved rules "
            f"path, sending the operator after a move that did not happen.\n{run.stdout}",
        )
        self.assertTrue(
            any("temp file" in message for message in run.check1_failures()),
            f"expected check 1 to name the temp-file failure.\n{run.stdout}",
        )


class TestNoRemedyRegressesTheLiveRules(unittest.TestCase):
    def test_no_failing_outcome_tells_the_operator_to_copy_a_file_over_the_live_rules(
        self,
    ) -> None:
        drifted = _run_fixture(
            rules_commit_age_seconds=3 * 3600,
            live_content=STALE_WORKING_TREE_CONTENT,
            working_tree_content=STALE_WORKING_TREE_CONTENT,
        )
        unreadable = _run_fixture(
            rules_commit_age_seconds=0,
            live_content=STALE_WORKING_TREE_CONTENT,
            working_tree_content=STALE_WORKING_TREE_CONTENT,
            delete_origin=True,
        )
        for run in (drifted, unreadable):
            for message in run.check1_failures():
                self.assertIsNone(
                    _copy_instruction_in(message),
                    "a check-1 failure told the operator to copy a file over the live "
                    f"rules; the checkout can be older than origin/main: {message}",
                )
        self.assertIsNone(
            _copy_instruction_in(_script_text()),
            "postdeploy_check.sh still contains a copy instruction aimed at the live "
            "rules path; the remedy must go through the sync, which promtool-checks "
            "and verifies the reload.",
        )

    def test_positive_control_a_copy_instruction_is_detected(self) -> None:
        fabricated = 'live rules drift — cp "$REPO_RULES" "$LIVE_RULES" then HUP'
        self.assertIsNotNone(
            _copy_instruction_in(fabricated),
            "positive control: the copy-instruction matcher has rotted to a no-op",
        )


class TestScriptConstantsAgreeWithTheSync(unittest.TestCase):
    def test_script_rules_path_equals_the_sync_script_constant(self) -> None:
        parsed = _parse_assignment("RULES_REPO_PATH")
        self.assertNotEqual("", parsed, "anti-rot: parsed an empty RULES_REPO_PATH")
        sync = _load_sync_module()
        self.assertEqual(
            sync.RULES_REPO_PATH,
            parsed,
            "postdeploy_check.sh must vouch for the same file sync_prometheus_rules.py "
            "converges the live copy to; naming different files makes the gate "
            "certify a file nothing syncs.",
        )

    def test_positive_control_a_diverging_rules_path_is_detected(self) -> None:
        sync = _load_sync_module()
        self.assertNotEqual(
            sync.RULES_REPO_PATH,
            _parse_assignment("RULES_REPO_PATH") + ".sentinel",
            "positive control: divergence not detected",
        )

    def test_script_no_longer_references_a_working_tree_rules_path(self) -> None:
        # assertFalse, not assertNotIn: the latter dumps the whole script.
        self.assertFalse(
            _mentions_working_tree_rules_path(_script_text()),
            "REPO_RULES is the working-tree rules path the gate must not compare "
            "against (#1564); its return reintroduces the false alarm.",
        )

    def test_positive_control_a_working_tree_rules_path_is_detected(self) -> None:
        fabricated = 'REPO_RULES="$REPO/deploy/monitoring/prometheus/rules/alphalens.yaml"'
        self.assertTrue(
            _mentions_working_tree_rules_path(fabricated),
            "positive control: the REPO_RULES scan has rotted to a no-op",
        )

    def test_live_rules_default_matches_the_path_the_sync_writes(self) -> None:
        # The home prefix is NOT pinned: the Python constant is
        # Path.home()-relative and home differs between the VPS and a laptop.
        # Everything below home is host-independent and IS pinned — a sync that
        # moves to another directory leaves the gate vouching for a file nothing
        # converges, which is the same defect as a wrong filename.
        default = Path(_parse_live_rules_default())
        sync = _load_sync_module()
        self.assertEqual(
            sync.LIVE_RULES_FILENAME,
            default.name,
            "the gate would check a file the sync never writes",
        )
        tail = Path(sync.DEFAULT_LIVE_DIR).relative_to(Path.home()).parts
        self.assertEqual(
            tail,
            default.parent.parts[-len(tail) :],
            "the gate would check a DIRECTORY the sync never writes into",
        )

    def test_the_unit_the_remedy_recommends_exists(self) -> None:
        unit = _parse_assignment("RULES_SYNC_UNIT")
        self.assertTrue(
            (SYSTEMD_DIR / unit).is_file(),
            f"the remedy points the operator at {unit}, which is not in deploy/systemd/",
        )

    def test_positive_control_a_missing_unit_is_detected(self) -> None:
        self.assertFalse(
            (SYSTEMD_DIR / "alphalens-no-such-unit.service").is_file(),
            "positive control: the unit-existence check has rotted to a no-op",
        )


class TestGraceWindowMatchesTheSyncCadence(unittest.TestCase):
    """The grace window is only correct relative to how often the sync runs."""

    HOURLY_ONCALENDAR_RE = re.compile(r"^OnCalendar=\*-\*-\* \*:(\d{2}):00 UTC$", re.MULTILINE)

    def _grace_seconds(self) -> int:
        return int(_parse_assignment("RULES_SYNC_GRACE_SECONDS"))

    def _timer_minute(self) -> str:
        match = self.HOURLY_ONCALENDAR_RE.search(RULES_SYNC_TIMER.read_text())
        self.assertIsNotNone(
            match,
            "the grace window assumes an hourly, minute-fixed sync; the timer no "
            "longer has that shape, so the window must be recomputed.",
        )
        assert match is not None
        return match.group(1)

    def test_the_sync_timer_is_hourly_at_a_fixed_minute(self) -> None:
        self.assertRegex(self._timer_minute(), r"^\d{2}$")

    def test_the_script_tells_the_operator_the_minute_the_timer_actually_fires(self) -> None:
        # The minute is prose in the script (a comment and the WARN message). A
        # timer moved off that minute would send the operator back at a time
        # nothing happens, and the message would teach them to distrust it.
        minute = self._timer_minute()
        self.assertIn(
            f":{minute} UTC",
            _script_text(),
            f"the timer fires at :{minute} UTC; postdeploy_check.sh names a "
            "different minute, so its WARN sends the operator back at the wrong time.",
        )

    def test_the_sync_timer_adds_no_randomized_delay(self) -> None:
        # RandomizedDelaySec widens the worst-case lag beyond one cadence, and
        # the grace window is sized for cadence + a little slack. Adding one
        # without widening the window turns a late-but-legitimate sync into the
        # hard FAIL that #1564 removed.
        self.assertNotIn(
            "RandomizedDelaySec",
            RULES_SYNC_TIMER.read_text(),
            "the grace window does not account for timer jitter; fold the delay "
            "into RULES_SYNC_GRACE_SECONDS before adding one.",
        )

    def test_the_grace_window_covers_one_whole_sync_cadence(self) -> None:
        grace = self._grace_seconds()
        self.assertGreater(
            grace,
            3600,
            "a grace window at or under one cadence fails the gate for the whole "
            "hour after every rules merge, which trains the operator to ignore it.",
        )
        self.assertLessEqual(
            grace,
            7200,
            "a grace window past two cadences hides a sync that missed a fire.",
        )

    def test_positive_control_a_too_small_grace_window_is_rejected(self) -> None:
        fabricated = 600
        self.assertFalse(
            3600 < fabricated <= 7200,
            "positive control: the grace-window bounds have rotted to a no-op",
        )


if __name__ == "__main__":
    unittest.main()
