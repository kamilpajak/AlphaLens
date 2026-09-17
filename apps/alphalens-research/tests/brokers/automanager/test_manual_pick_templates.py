"""The manual-pick templates and the copy recipe in the contract README (#1470).

`broker arm-manual` compiled operator flags into a document. Its replacement is a
JSON template the operator copies and edits, plus a `jq` line that turns an armed
journal line back into a document. Both are published text, so both are run here:

1. Every template in `examples/manual-pick/` arms through the real `broker arm`,
   and the README links each one.
2. The recipe is read LITERALLY out of the README and run with `bash`, so the text
   an operator copies and the text this test runs cannot drift apart.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from tests.brokers.automanager.cli_isolation import _isolate_home

_REPO_ROOT = Path(__file__).resolve().parents[5]
_CONTRACT = _REPO_ROOT / "apps" / "alphalens-broker-contract"
_TEMPLATES = _CONTRACT / "examples" / "manual-pick"
_README = _CONTRACT / "README.md"
_RECIPE_MARKER = "<!-- manual-pick-copy-recipe -->"
_TEMPLATE_NAMES = {
    "pullback-two-tiers.json",
    "pullback-trailing-stop.json",
    "immediate-plus-pullback.json",
}

# A Wednesday during the New York session, so a template without `trade_date`
# arms under this session.
_ARMING_MOMENT = dt.datetime(2026, 9, 16, 15, 0, tzinfo=dt.UTC)
_TRADE_DATE = "2026-09-16"


def _template(name: str) -> dict:
    return json.loads((_TEMPLATES / name).read_text(encoding="utf-8"))


def _recipe() -> str:
    """The first ```bash block after the marker, exactly as published."""
    text = _README.read_text(encoding="utf-8")
    after = text.split(_RECIPE_MARKER, 1)[1]
    match = re.search(r"```bash\n(.*?)```", after, flags=re.DOTALL)
    assert match is not None, "no bash block after the recipe marker"
    return match.group(1)


class _InboxCase(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self.home = _isolate_home(self)
        clock = mock.patch("alphalens_cli.commands.broker._arming_now", return_value=_ARMING_MOMENT)
        clock.start()
        self.addCleanup(clock.stop)
        self.inbox = self.home / ".alphalens" / "broker_orders" / "sim" / "picks.jsonl"

    def broker(self, argv: list[str], stdin: str | None = None):
        from alphalens_cli.commands.broker import broker_app

        return self.runner.invoke(broker_app, argv, input=stdin)

    def arm(self, document: dict, *extra: str):
        return self.broker(["arm", "-", *extra], stdin=json.dumps(document))

    def armed(self) -> list:
        from alphalens_pipeline.brokers.automanager.picks import read_pick_fold

        return [r for r in read_pick_fold(path=self.inbox).records if r.status == "armed"]


class EveryTemplateArmsThroughTheDoor(_InboxCase):
    def test_the_directory_holds_exactly_the_published_templates(self) -> None:
        # Without this a missing directory would make the loop below pass vacuously.
        self.assertEqual({path.name for path in _TEMPLATES.glob("*.json")}, _TEMPLATE_NAMES)

    def test_each_template_arms(self) -> None:
        for name in sorted(_TEMPLATE_NAMES):
            with self.subTest(template=name):
                self.inbox.unlink(missing_ok=True)

                result = self.arm(_template(name), "--format", "json")

                self.assertEqual(result.exit_code, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertTrue(payload["armed"])
                self.assertEqual(payload["intent"]["meta"]["source"], "manual")
                self.assertEqual(len(self.armed()), 1)

    def test_the_trailing_template_declares_management_and_places_no_levels(self) -> None:
        """`exit.initial_levels` would replace the ladder's stop and take-profits
        with its own two levels (#1414), which is not what "the same ladder with a
        trailing stop" means."""
        exit_spec = _template("pullback-trailing-stop.json")["exit"]
        self.assertIsNone(exit_spec["initial_levels"])
        self.assertEqual([r["kind"] for r in exit_spec["reaction_plan"]], ["trailing_stop"])

    def test_the_immediate_template_lists_its_immediate_tier_first(self) -> None:
        tiers = _template("immediate-plus-pullback.json")["spec"]["entry_tiers"]
        self.assertEqual(
            [t.get("entry_mode", "pullback") for t in tiers], ["immediate", "pullback"]
        )

    def test_the_readme_links_every_template(self) -> None:
        text = _README.read_text(encoding="utf-8")
        for name in sorted(_TEMPLATE_NAMES):
            with self.subTest(template=name):
                self.assertIn(f"examples/manual-pick/{name}", text)


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "the recipe needs jq and bash")
class TheCopyRecipeTurnsAnArmedLineBackIntoADocument(_InboxCase):
    def run_recipe(self) -> dict:
        env = {**os.environ, "HOME": str(self.home)}
        completed = subprocess.run(
            ["bash", "-c", _recipe()],
            cwd=self.home,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads((self.home / "ko.json").read_text(encoding="utf-8"))

    def disarm(self) -> None:
        result = self.broker(["disarm", "KO", "--date", _TRADE_DATE])
        self.assertEqual(result.exit_code, 0, result.output)

    def test_a_disarmed_pick_comes_back_as_the_next_generation(self) -> None:
        template = _template("pullback-two-tiers.json")
        self.assertEqual(self.arm(template).exit_code, 0)
        self.disarm()

        copy = self.run_recipe()
        result = self.arm(copy, "--format", "json")

        self.assertEqual(result.exit_code, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["generation"], 2)
        self.assertEqual(
            payload["intent"]["spec"]["entry_tiers"][0]["limit_price"],
            template["spec"]["entry_tiers"][0]["limit_price"],
        )

    def test_a_replaced_pick_copies_its_latest_line(self) -> None:
        template = _template("pullback-two-tiers.json")
        self.assertEqual(self.arm(template).exit_code, 0)
        replacement = json.loads(json.dumps(template))
        replacement["meta"]["trade_date"] = _TRADE_DATE
        replacement["meta"]["generation"] = 1
        replacement["spec"]["entry_tiers"][0]["limit_price"] = 68.25
        self.assertEqual(self.arm(replacement).exit_code, 0)
        self.disarm()

        copy = self.run_recipe()

        self.assertEqual(copy["spec"]["entry_tiers"][0]["limit_price"], 68.25)

    def test_a_torn_line_does_not_make_it_copy_an_older_pick(self) -> None:
        """A torn append stays in the journal as a fragment. Without `fromjson?`
        jq stops there and `tail` returns the line before it."""
        template = _template("pullback-two-tiers.json")
        self.assertEqual(self.arm(template).exit_code, 0)
        with self.inbox.open("a", encoding="utf-8") as journal:
            journal.write('{"ticker": "KO", "status": "armed", "intent": {\n')
        replacement = json.loads(json.dumps(template))
        replacement["meta"]["trade_date"] = _TRADE_DATE
        replacement["meta"]["generation"] = 1
        replacement["spec"]["entry_tiers"][0]["limit_price"] = 68.25
        self.assertEqual(self.arm(replacement).exit_code, 0)

        copy = self.run_recipe()

        self.assertEqual(copy["spec"]["entry_tiers"][0]["limit_price"], 68.25)

    def test_a_copied_brief_pick_is_refused_rather_than_re_sourced(self) -> None:
        """The recipe drops `trade_date`, which a brief document must state. A brief
        pick is produced again with `thematic intent`, not copied."""
        brief = _template("pullback-two-tiers.json")
        brief["meta"] = {"source": "brief", "trade_date": _TRADE_DATE}
        self.assertEqual(self.arm(brief).exit_code, 0)
        self.disarm()

        copy = self.run_recipe()
        result = self.arm(copy, "--format", "json")

        failure = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(failure["details"]["reason"], "trade_date_required")
        self.assertEqual(len(self.armed()), 0)


if __name__ == "__main__":
    unittest.main()
