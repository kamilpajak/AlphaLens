"""Pin the env-var tri-source parity contract for the pipeline deploy shape.

Incident (2026-05-31): PR-G swapped the thematic pipeline to DeepSeek v4 via
OpenRouter and added ``OPENROUTER_API_KEY`` to the VPS ``/etc/alphalens/env``
plus the ``-e OPENROUTER_API_KEY`` pass-through in
``alphalens-thematic-build.service``. The key was NOT added to
``deploy/docker/.env.example``. The unit ran, docker forwarded an *unset*
variable (``-e KEY`` with no value silently copies "" when the var is absent
from the calling env), and the LLM stage degraded inside the container with
no loud failure on a fresh checkout — the ``.env.example`` catalogue (the only
onboarding doc telling an operator which secrets to fill in) never mentioned
the key. Failure class: "a systemd unit expects a secret passed through to the
container, but the operator-facing ``.env.example`` catalogue forgot it ->
silent container degradation".

The defensible invariant pinned here:

    Every env KEY that a systemd ``.service`` unit PASSES THROUGH to a
    container via the ``-e KEY`` / ``--env KEY`` form (NO inline ``=value``,
    so the value MUST come from the surrounding environment) MUST be
    documented as a ``KEY=`` line in ``deploy/docker/.env.example``.

This is deliberately NOT set-equality across the three sources. They have
different scopes:

  - ``deploy/docker/.env.example`` is the FULL operator catalogue (it may
    list optional keys no unit happens to pass through today, e.g. a backlog
    Telegram key).
  - A systemd unit only passes through the SUBSET of keys it needs.
  - ``Dockerfile.pipeline`` ``ENV`` lines set build-time defaults (PATH,
    UV_PROJECT_ENVIRONMENT, …) that are NOT secrets and must NOT be required
    in ``.env.example``.

Only the pass-through direction is load-bearing: a value-bearing ``-e
HOME=/app/home`` is self-contained (docker has the value) and is correctly
excluded from the requirement.

House-style sibling tests: ``test_deploy_systemd_units.py`` (which pins the
single ``-e OPENROUTER_API_KEY`` line is present in the unit) and
``test_pipeline_runtime_deps_declared.py``. This file is the other half of
that incident: the unit passing the key through is necessary but not
sufficient — ``.env.example`` must also document it.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

# Test file lives at apps/alphalens-research/tests/<name>.py; deploy/ sits at
# the repo root, three parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_DIR = REPO_ROOT / "deploy"
ENV_EXAMPLE = DEPLOY_DIR / "docker" / ".env.example"
DOCKERFILE_PIPELINE = DEPLOY_DIR / "docker" / "Dockerfile.pipeline"
SYSTEMD_DIR = DEPLOY_DIR / "systemd"

# A ``KEY=value`` line in a dotenv-style file. The value half is optional
# (``.env.example`` ships ``KEY=`` placeholders). Anchored to a line start so
# a commented ``# KEY=`` line is ignored. Optional whitespace around ``=``
# tolerates a hand-edited ``KEY = value`` line.
ENV_EXAMPLE_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)

# A docker pass-through env flag with NO inline value: ``-e KEY`` or
# ``--env KEY`` where the next token is NOT ``KEY=value``. Matched on a
# whole token boundary so ``-e HOME=/app/home`` (value-bearing) does NOT
# match — the negative lookahead ``(?!=)`` after the key rejects an ``=``.
#
# Capturing the key name lets us collect the pass-through set. The trailing
# ``(?=\s|\\|$)`` ensures the key is a complete token (so ``-e KEYFOO`` can't
# be mistaken when scanning for ``KEY``).
PASSTHROUGH_ENV_RE = re.compile(r"(?:-e|--env)\s+([A-Za-z_][A-Za-z0-9_]*)(?!=)(?=\s|\\|$)")


def _env_example_keys(text: str) -> set[str]:
    """Return the set of documented KEY names in a dotenv-style file body."""
    return set(ENV_EXAMPLE_KEY_RE.findall(text))


def _passthrough_keys(unit_text: str) -> set[str]:
    """Return env KEYs a unit passes through with no inline value.

    Skips lines that are systemd comments (``# ...``) so the prose in the
    thematic-build unit header ("add a ``-e NEW_KEY`` line below") does not
    count as a real pass-through directive.
    """
    keys: set[str] = set()
    for raw_line in unit_text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue
        keys.update(PASSTHROUGH_ENV_RE.findall(raw_line))
    return keys


class TestEnvKeyTriSourceParity(unittest.TestCase):
    """Every passed-through systemd env KEY is documented in .env.example."""

    def setUp(self) -> None:
        self.assertTrue(ENV_EXAMPLE.is_file(), f"missing {ENV_EXAMPLE}")
        self.assertTrue(SYSTEMD_DIR.is_dir(), f"missing {SYSTEMD_DIR}")
        self.documented = _env_example_keys(ENV_EXAMPLE.read_text())

    def test_env_example_parses_at_least_the_known_catalogue(self) -> None:
        # Guard the parser itself: if the KEY= regex ever rots to an empty
        # match set, the parity check below would pass vacuously. These two
        # keys are unconditionally present in the catalogue today (the SEC
        # contact is mandatory; Polygon drives the press gate). A regex that
        # stops finding them is broken, not the file.
        for canary in ("SEC_EDGAR_USER_AGENT", "POLYGON_API_KEY"):
            self.assertIn(
                canary,
                self.documented,
                f"{canary} must parse out of .env.example — the KEY= regex "
                "looks broken (refusing to run parity on an empty catalogue).",
            )

    def test_every_passthrough_key_is_documented_in_env_example(self) -> None:
        # The core invariant. Walk every .service unit, collect the keys it
        # forwards via the value-less ``-e KEY`` form, and assert each is a
        # KEY= line in .env.example. A miss is the exact PR-G incident.
        offenders: dict[str, list[str]] = {}
        for service_path in sorted(SYSTEMD_DIR.glob("*.service")):
            passthrough = _passthrough_keys(service_path.read_text())
            for key in sorted(passthrough - self.documented):
                offenders.setdefault(key, []).append(service_path.name)

        if offenders:
            lines = [
                "env KEY(s) passed through to a container via `-e KEY` in a "
                "systemd unit but MISSING from deploy/docker/.env.example:"
            ]
            for key, units in sorted(offenders.items()):
                lines.append(f"  - {key}: forwarded by {', '.join(units)}")
            lines.append(
                "\nAdd a `KEY=` placeholder line for each to "
                "deploy/docker/.env.example. `-e KEY` (no =value) copies the "
                "value from the calling env; if .env.example never documents "
                "it, a fresh operator leaves it unset and the container "
                "degrades silently (the OPENROUTER_API_KEY incident, "
                "2026-05-31)."
            )
            self.fail("\n".join(lines))

    def test_openrouter_api_key_specifically_pinned(self) -> None:
        # Pin the literal regression so a future loosening of the general
        # walk cannot silently re-open it. The thematic-build unit forwards
        # OPENROUTER_API_KEY (DeepSeek v4 via OpenRouter, PR-G); .env.example
        # MUST document it.
        thematic = SYSTEMD_DIR / "alphalens-thematic-build.service"
        self.assertTrue(thematic.is_file(), f"missing {thematic}")
        self.assertIn(
            "OPENROUTER_API_KEY",
            _passthrough_keys(thematic.read_text()),
            "thematic-build.service must forward OPENROUTER_API_KEY via "
            "`-e OPENROUTER_API_KEY` (the LLM stage needs it).",
        )
        self.assertIn(
            "OPENROUTER_API_KEY",
            self.documented,
            "deploy/docker/.env.example MUST document OPENROUTER_API_KEY — "
            "the thematic-build unit forwards it to the container. Missing it "
            "is the PR-G silent-degradation incident (2026-05-31).",
        )

    def test_value_bearing_env_flag_is_not_required_in_env_example(self) -> None:
        # Scope guard: a value-bearing ``-e HOME=/app/home`` is self-contained
        # (docker already has the value) and must NOT be treated as a
        # pass-through that .env.example has to document. The thematic unit
        # sets HOME=/app/home this way; confirm the parser excludes it.
        thematic = SYSTEMD_DIR / "alphalens-thematic-build.service"
        self.assertNotIn(
            "HOME",
            _passthrough_keys(thematic.read_text()),
            "Value-bearing `-e HOME=/app/home` must not be collected as a "
            "pass-through key (it carries its own value, .env.example needs "
            "no entry).",
        )


class TestPassthroughParserPositiveControl(unittest.TestCase):
    """Positive control — the parity check MUST fail on broken input.

    Without these, the scanner could silently rot to a no-op (e.g. a regex
    that never matches any pass-through key) and still pass against the real
    repo, defeating the whole guard. This is the mandatory project rule.
    """

    def test_synthetic_undocumented_passthrough_is_detected(self) -> None:
        # A fabricated unit body that forwards a key absent from any real
        # .env.example. The parser MUST surface it as a pass-through key, and
        # the parity comparison MUST flag it as missing.
        fake_unit = (
            "[Service]\n"
            "Type=oneshot\n"
            "ExecStart=/usr/bin/docker run --rm \\\n"
            "    -e HOME=/app/home \\\n"
            "    -e MADE_UP_SECRET \\\n"
            "    alphalens-pipeline:latest thematic ingest\n"
        )
        keys = _passthrough_keys(fake_unit)
        self.assertIn(
            "MADE_UP_SECRET",
            keys,
            "Parser failed to detect a value-less `-e MADE_UP_SECRET` "
            "pass-through — the regex has rotted to a no-op.",
        )
        # And the value-bearing HOME must still be excluded even here.
        self.assertNotIn("HOME", keys)

        documented = _env_example_keys(ENV_EXAMPLE.read_text())
        self.assertEqual(
            keys - documented,
            {"MADE_UP_SECRET"},
            "The parity comparison must flag MADE_UP_SECRET as the sole "
            "undocumented pass-through key from the synthetic unit.",
        )

    def test_double_dash_env_form_also_detected(self) -> None:
        # Docker accepts both ``-e KEY`` and ``--env KEY``. A unit author
        # using the long form must not slip past the scanner.
        fake_unit = "ExecStart=docker run --env ANOTHER_FAKE_SECRET img cmd\n"
        self.assertIn("ANOTHER_FAKE_SECRET", _passthrough_keys(fake_unit))

    def test_commented_passthrough_is_ignored(self) -> None:
        # The real thematic unit header documents the pattern in a comment
        # ("add a `-e NEW_KEY` line below"). A commented line is prose, not a
        # live directive — it must NOT be collected as a pass-through key.
        commented = "# add a -e COMMENTED_KEY line below\nExecStart=docker run img\n"
        self.assertNotIn("COMMENTED_KEY", _passthrough_keys(commented))

    def test_env_example_key_regex_ignores_comments(self) -> None:
        # ``# SEC_EDGAR_USER_AGENT contact ...`` prose must not be parsed as
        # a documented KEY= line, or a commented-out key would falsely
        # satisfy the parity check.
        body = "# THIS_IS_PROSE=not a key line\nREAL_KEY=\n"
        keys = _env_example_keys(body)
        self.assertIn("REAL_KEY", keys)
        self.assertNotIn("THIS_IS_PROSE", keys)


if __name__ == "__main__":
    unittest.main()
