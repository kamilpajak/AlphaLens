"""Pin the GHCR tag scheme shared by the workflow that writes it and the check that reads it.

``.github/workflows/django-image.yml`` publishes the django image under a tag
derived from the commit SHA. ``deploy/scripts/postdeploy_check.sh`` asks the
registry for that tag by name, to cross-check the digest of the running image
against the digest GHCR serves. Neither file reads the other, so the tag is one
fact written down twice.

They drifted, silently, for as long as nobody looked (#1569). The script built
the tag with ``git rev-parse --short``, whose length is ADAPTIVE — git grows the
abbreviation as the repository grows. ``docker/metadata-action`` with
``format=short`` does not abbreviate, it TRUNCATES to a fixed 7 characters. Once
git moved to 8 the script asked for ``sha-c6ea90e8`` while the registry held
``sha-c6ea90e``, and the lookup failed for every commit. It surfaced only as a
WARN blaming an unreachable registry, so it read as a network blip.

The fix removes the abbreviation from both sides: ``format=long`` publishes the
full SHA, and the script concatenates the prefix onto the SHA it already has.
This module pins that, by BUILDING the tag with the script's own assignment and
comparing it to the tag the workflow's declared scheme produces for the same
commit.

What this cannot catch: whether ``docker/metadata-action`` honours
``format=long`` the way it is read here. That is the vendor's contract, checked
against the real registry the first time the gate runs after a deploy, not here.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "postdeploy_check.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "django-image.yml"

# A commit SHA long enough that any abbreviation of it differs from the whole.
SAMPLE_SHA = "c6ea90e8a7613868b34bb3e6f96d19cb58000cc1"

_SHA_TAG_RE = re.compile(r"^type=sha(?:,(?P<params>.*))?$")
_PREFIX_ASSIGN_RE = re.compile(r'^IMAGE_TAG_PREFIX="(?P<value>[^"]*)"', re.MULTILINE)
# The script declares EXPECTED_TAG empty first, so the assignment that BUILDS
# it is the one whose right-hand side is not the empty string.
_TAG_ASSIGN_RE = re.compile(r'^\s*(?P<line>EXPECTED_TAG=(?!""$).+)$', re.MULTILINE)
_ABBREVIATION_RE = re.compile(r"rev-parse\s+--short")


def _workflow_sha_tag_params() -> dict[str, str]:
    """The ``type=sha`` entry of ``metadata-action``'s ``tags``, as a dict.

    PyYAML parses the reserved word ``on:`` as the boolean key ``True``
    (YAML 1.1), but the tags list sits under ``jobs``, so no such workaround is
    needed here. The ``tags`` value is a newline-delimited string, not a list.
    """
    doc = yaml.safe_load(WORKFLOW.read_text())
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            if not uses.startswith("docker/metadata-action"):
                continue
            for entry in str(step["with"]["tags"]).splitlines():
                match = _SHA_TAG_RE.match(entry.strip())
                if match is None:
                    continue
                params = match.group("params") or ""
                return dict(part.split("=", 1) for part in params.split(",") if "=" in part)
    raise AssertionError(
        "no `type=sha` entry found under a docker/metadata-action step in "
        f"{WORKFLOW.name} — the tag scheme moved; update this parser rather than "
        "deleting the pin."
    )


def _script_prefix() -> str:
    match = _PREFIX_ASSIGN_RE.search(SCRIPT.read_text())
    if match is None:
        raise AssertionError(
            'IMAGE_TAG_PREFIX="..." assignment not found in postdeploy_check.sh '
            "— the assignment shape changed; update _PREFIX_ASSIGN_RE."
        )
    return match.group("value")


def _tag_the_script_builds(sha: str) -> str:
    """Run the script's own EXPECTED_TAG assignment for ``sha`` and return it.

    Executing the assignment rather than reading it is what makes this a parity
    check and not a spelling check: a line that silently abbreviates, pads or
    reorders would produce a different string here.
    """
    builders = _TAG_ASSIGN_RE.findall(SCRIPT.read_text())
    if len(builders) != 1:
        raise AssertionError(
            "expected exactly one assignment that BUILDS EXPECTED_TAG in "
            f"postdeploy_check.sh, found {len(builders)}: {builders}. Two "
            "builders would mean the tag is constructed in more than one place, "
            "which is the drift this module exists to prevent."
        )
    program = (
        f'IMAGE_TAG_PREFIX="{_script_prefix()}"\n'
        f'EXPECTED_SHA="{sha}"\n'
        f"{builders[0]}\n"
        'printf "%s" "$EXPECTED_TAG"\n'
    )
    done = subprocess.run(
        ["bash", "-uo", "pipefail", "-c", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


class TestTheWorkflowPublishesTheWholeSha(unittest.TestCase):
    def test_the_sha_tag_entry_parses(self) -> None:
        # Anti-rot: an empty parse would make every assertion below vacuous.
        self.assertNotEqual({}, _workflow_sha_tag_params())

    def test_the_sha_tag_is_not_abbreviated(self) -> None:
        params = _workflow_sha_tag_params()
        self.assertEqual(
            "long",
            params.get("format"),
            "`format=short` truncates to a fixed 7 characters while the script "
            "names the whole commit. That mismatch is #1569: the lookup misses "
            f"for every commit. Parsed: {params}",
        )

    def test_the_prefix_is_declared_and_not_empty(self) -> None:
        self.assertTrue(
            _workflow_sha_tag_params().get("prefix"),
            "a tag with no prefix collides with the branch and `latest` tags",
        )


class TestTheScriptBuildsTheTagTheWorkflowPublishes(unittest.TestCase):
    def test_the_prefix_matches(self) -> None:
        self.assertEqual(_workflow_sha_tag_params()["prefix"], _script_prefix())

    def test_the_built_tag_equals_the_published_tag(self) -> None:
        published = _workflow_sha_tag_params()["prefix"] + SAMPLE_SHA
        self.assertEqual(
            published,
            _tag_the_script_builds(SAMPLE_SHA),
            "the script must ask the registry for exactly the tag the workflow "
            "wrote. Any transformation of the SHA between the two is a way for "
            "them to drift apart again.",
        )

    def test_the_script_does_not_abbreviate_a_sha_anywhere(self) -> None:
        # `git rev-parse --short` has an ADAPTIVE length: it grows with the
        # repository. It is what broke the lookup, and it has no remaining use
        # in this script, so its absence is the durable form of the pin.
        source = SCRIPT.read_text()
        self.assertIsNone(
            _ABBREVIATION_RE.search(source),
            "postdeploy_check.sh abbreviates a SHA again. The registry tag is "
            "the whole commit; an abbreviation can only diverge from it.",
        )

    def test_positive_control_an_abbreviating_script_is_detected(self) -> None:
        # A fabricated regression MUST be caught, or the check above is a no-op.
        self.assertIsNotNone(
            _ABBREVIATION_RE.search('SHORT="$(git rev-parse --short "$SHA")"'),
            "positive control: an abbreviation went undetected",
        )

    def test_positive_control_a_truncating_assignment_is_detected(self) -> None:
        # The exact regression #1569 was: a tag built from part of the SHA.
        published = _workflow_sha_tag_params()["prefix"] + SAMPLE_SHA
        truncated = _workflow_sha_tag_params()["prefix"] + SAMPLE_SHA[:7]
        self.assertNotEqual(
            published,
            truncated,
            "positive control: a truncated tag compared equal to the whole one",
        )


class TestTheRegistryFailureStatesAreToldApart(unittest.TestCase):
    """A tag that does not exist is permanent; an unreachable registry is not.

    Reporting both with one sentence is what let #1569 sit unnoticed for
    months: a permanent mismatch read as a passing network problem.
    """

    def test_the_script_branches_on_a_not_found_answer(self) -> None:
        self.assertIn(
            "not found",
            SCRIPT.read_text(),
            "the script must recognise the registry's `not found` answer and "
            "say so, instead of reporting every empty digest as an unreachable "
            "registry.",
        )

    def test_the_unreachable_message_no_longer_claims_both_states(self) -> None:
        source = SCRIPT.read_text()
        self.assertNotIn(
            "registry unreachable or image not built",
            source,
            "this single message covered two states, one temporary and one "
            "permanent. Reporting them apart is the point of #1569.",
        )


if __name__ == "__main__":
    unittest.main()
