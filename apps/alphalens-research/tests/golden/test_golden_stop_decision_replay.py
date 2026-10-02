"""Golden-master replay of the daemon's two post-fill stop arms (#1581 PR B1).

Drives ``position_manager._maybe_trail`` and ``_maybe_reanchor`` over the frozen
table in ``tests/golden/stop_decision_cases.py`` and asserts, field by field and
bit for bit, that they still answer what they answered when the corpus was
recorded.

WHAT THIS IS FOR. The arms are about to delegate their decision to
``broker_contract.stop_decision``, a guard-for-guard copy of them (#1581). The
safety claim is that the live answers do not move. This module is that claim as
a test: the corpus was recorded from the UNMODIFIED arms and committed BEFORE a
single production line changed, so a delegation that moves an answer is red here
and nowhere else.

WHAT IT CANNOT SEE, stated rather than left to be discovered. The record holds
every ``AmendStop`` field and every log message, so it witnesses a changed
level, a changed amend field, a changed log TEXT and a gained or lost log line.
It does NOT witness the ORDER of two log lines within one call (no case emits
two), and it cannot distinguish WHY an answer is ``None`` beyond what the logs
say -- which is exactly why the trail arm's silent ratchet refusal needed its
own bucket and its own comment in the table.

Missing fixture is fail-loud, the convention ``test_golden_score_replay.py``
follows: a corpus that must be re-recorded says so by name.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from tests.golden.stop_decision_cases import CASES, run_case, same_float, shape_violations

_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "stop_decision" / "golden" / "corpus.json"
_RECORDER = "scripts/record_golden_stop_decision.py"


def _load() -> list[dict[str, Any]]:
    if not _GOLDEN.exists():
        raise FileNotFoundError(
            f"golden corpus missing at {_GOLDEN} -- run {_RECORDER} "
            "(from apps/alphalens-research: uv run python -m "
            "scripts.record_golden_stop_decision) to record it. It MUST be "
            "recorded against unmodified arms; see #1581."
        )
    return json.loads(_GOLDEN.read_text())


class TheArmsStillAnswerWhatTheCorpusRecordedTest(unittest.TestCase):
    """One assertion concept: the replayed record equals the committed one."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.recorded = _load()

    def test_the_corpus_covers_exactly_the_frozen_table(self) -> None:
        """A case added to the table without re-recording, or a stale row left
        behind after one was removed, is a red here rather than a silent gap."""
        self.assertEqual(
            [row["name"] for row in self.recorded],
            [case.name for case in CASES],
            "the corpus and the case table disagree -- re-run the recorder",
        )

    def test_every_case_matches_the_shape_its_bucket_declares(self) -> None:
        """The non-vacuity gate, and it checks the SHAPE rather than a row count.

        Counting rows per bucket has no power: the first recording of this
        corpus had all fourteen buckets non-empty while every one of the ten
        re-anchor cases answered None, ``reanchor_clean_places`` included,
        because their brief floor sat above their own proposal. A property can
        be true and empty. So each bucket declares whether its cases place a
        level, whether the arm logs, and whether the #1015 triple is stamped,
        and a case that drifts out of its own name is red here."""
        problems = shape_violations(self.recorded)
        self.assertEqual(problems, [], "\n  - ".join(["bucket shapes violated:", *problems]))

    def test_every_case_replays_bit_for_bit(self) -> None:
        for case, row in zip(CASES, self.recorded, strict=True):
            with self.subTest(case=case.name):
                self.assertEqual(case.name, row["name"])
                replayed = run_case(case)
                self.assertEqual(
                    replayed["answer"] is None,
                    row["answer"] is None,
                    f"{case.name}: one side moved the stop and the other did not",
                )
                if row["answer"] is not None:
                    self.assertEqual(
                        sorted(replayed["answer"]),
                        sorted(row["answer"]),
                        f"{case.name}: the AmendStop field set changed",
                    )
                    for field, expected in row["answer"].items():
                        self.assertTrue(
                            same_float(replayed["answer"][field], expected),
                            f"{case.name}.{field}: {replayed['answer'][field]!r} "
                            f"!= recorded {expected!r}",
                        )
                self.assertTrue(
                    same_float(replayed["composed_ratchet_floor"], row["composed_ratchet_floor"]),
                    f"{case.name}: the composed ratchet floor changed",
                )
                self.assertEqual(
                    replayed["logs"], row["logs"], f"{case.name}: the log lines changed"
                )


if __name__ == "__main__":
    unittest.main()
