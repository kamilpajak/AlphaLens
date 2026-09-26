"""The replay copy recipe in the package README, run literally on a real
journal line.

The arming door's own recipe (contract README) deletes `meta.trade_date`
because a NEW pick gets a new date; the replay wants the date the pick was
armed under, so its recipe keeps it and removes only what the door derived.
The line is produced by the real `broker arm` (the precedent in
`test_manual_pick_templates.py`), not typed by hand, so a new derived field in
the stored rendering would be caught here rather than trusted.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from intent_replay.door import DoorRefusalError, admit

from tests.brokers.automanager.test_manual_pick_templates import _InboxCase, _template

README = Path(__file__).resolve().parents[4] / "apps" / "intent-replay" / "README.md"
MARKER = "<!-- replay-copy-recipe -->"


def _recipe() -> str:
    """The first ```bash block after the marker, exactly as published."""
    after = README.read_text(encoding="utf-8").split(MARKER, 1)[1]
    match = re.search(r"```bash\n(.*?)```", after, flags=re.DOTALL)
    assert match is not None, "no bash block after the recipe marker"
    return match.group(1)


@unittest.skipUnless(shutil.which("jq") and shutil.which("bash"), "the recipe needs jq and bash")
class TheReplayRecipeYieldsADocumentTheDoorAdmits(_InboxCase):
    def run_recipe(self) -> dict:
        completed = subprocess.run(
            ["bash", "-c", _recipe()],
            cwd=self.home,
            env={**os.environ, "HOME": str(self.home)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads((self.home / "ko.json").read_text(encoding="utf-8"))

    def test_the_copy_keeps_the_date_and_passes_the_replay_door(self) -> None:
        result = self.arm(_template("pullback-two-tiers.json"), "--format", "json")
        self.assertEqual(result.exit_code, 0, result.stderr)
        armed = json.loads(result.stdout)["intent"]

        # Positive control: the raw journal line is refused for its derived fields,
        # so the recipe has something to remove.
        with self.assertRaises(DoorRefusalError) as caught:
            admit(armed)
        self.assertEqual(caught.exception.reason, "derived_field_supplied")

        document = self.run_recipe()
        self.assertEqual(document["meta"]["trade_date"], armed["meta"]["trade_date"])
        self.assertEqual(document["meta"]["generation"], armed["meta"]["generation"])
        self.assertNotIn("intent_id", document)
        self.assertNotIn("armed_ts", document["meta"])
        self.assertNotIn("r_multiple", document["spec"]["tp_tranches"][0])
        admitted = admit(document)
        self.assertEqual(admitted.intent.meta.trade_date, armed["meta"]["trade_date"])


if __name__ == "__main__":
    unittest.main()
