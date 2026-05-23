"""Pin equality of the duplicate ``DEFAULT_SMOKE_TIMEOUT_S`` constants.

``alphalens_cli.commands.preaudit._DEFAULT_SMOKE_TIMEOUT_S`` duplicates
``alphalens_research.preaudit.runner.DEFAULT_SMOKE_TIMEOUT_S`` at the
pipeline-side CLI surface because typer.Option evaluates its default at
function-definition time (module import) and we lazy-import the research
tier inside command bodies to keep the pipeline → research direction
clean at workspace level.

If either constant changes, this test fails and the developer must update
the other to match — preserving the help-text default a user sees from
``alphalens preaudit --help``.
"""

from __future__ import annotations

import unittest

from alphalens_cli.commands.preaudit import _DEFAULT_SMOKE_TIMEOUT_S
from alphalens_research.preaudit.runner import DEFAULT_SMOKE_TIMEOUT_S


class TestPreauditCliDefaultInSync(unittest.TestCase):
    def test_cli_default_matches_research_default(self):
        self.assertEqual(
            _DEFAULT_SMOKE_TIMEOUT_S,
            DEFAULT_SMOKE_TIMEOUT_S,
            "alphalens_cli.commands.preaudit._DEFAULT_SMOKE_TIMEOUT_S must equal "
            "alphalens_research.preaudit.runner.DEFAULT_SMOKE_TIMEOUT_S — typer.Option "
            "evaluates the default at module import time so it can't lazy-import the "
            "research-side value. Update whichever drifted to restore parity.",
        )


if __name__ == "__main__":
    unittest.main()
