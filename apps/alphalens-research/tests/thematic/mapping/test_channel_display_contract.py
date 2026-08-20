"""Cross-boundary pins between the channel taxonomy and what the card renders.

Issue #1069 put the causal-support record on the SPA. The SPA cannot import the
pipeline, so three things live in two places at once and can silently drift:

1. the sentence that bounds what the scale claims,
2. the SUPPORT vocabulary the card has a plain-language line for,
3. the GROUNDING vocabulary the card has a plain-language line for.

A drift is invisible at runtime — an unknown token degrades to "not assessed",
which reads like a real answer. These tests read the SPA sources directly so a
pipeline-side rename fails here, naming the file to update, rather than shipping
a card that quietly mislabels every row of a new level.

Deliberately a STRING check, not an import: Django and the SPA are separate
deployables and the pipeline is not on their path. The alternative (publishing
the vocabulary through the API) would put a display concern in the wire
contract for no gain.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from alphalens_pipeline.thematic.mapping import channel_assessor

_REPO_ROOT = Path(__file__).resolve().parents[5]
_FORMAT_TS = _REPO_ROOT / "apps" / "web" / "src" / "lib" / "format.ts"


def _format_source() -> str:
    return _FORMAT_TS.read_text(encoding="utf-8")


def _record_keys(source: str, const_name: str) -> set[str]:
    """Keys of a `const NAME: Record<string, string> = { ... }` literal."""
    start = source.index(f"const {const_name}")
    body = source[start : source.index("};", start)]
    return set(re.findall(r"^\t(\w+):", body, flags=re.MULTILINE))


class TestSpaMirrorsChannelVocabulary(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(
            _FORMAT_TS.exists(),
            f"the SPA format module moved; update this pin: {_FORMAT_TS}",
        )
        self.source = _format_source()

    def test_not_a_forecast_sentence_matches_the_pipeline_verbatim(self):
        # The card must not soften or embellish the one sentence the assessor
        # prompt is held to. Concatenated across two TS lines, so compare on the
        # de-quoted text rather than on the literal. The declaration block ends
        # at the blank line — NOT at the first ";", which the sentence contains.
        block = self.source[
            self.source.index("export const CAUSAL_SUPPORT_NOT_A_FORECAST") :
        ].split("\n\n")[0]
        spa_text = "".join(re.findall(r"'([^']*)'", block))
        self.assertEqual(spa_text, channel_assessor.CAUSAL_SUPPORT_NOT_A_FORECAST)

    def test_every_support_level_has_a_plain_language_line(self):
        # Plus no_record, which is a facts-level value rather than a level — the
        # card needs a line for it precisely BECAUSE it is not one.
        expected = set(channel_assessor.CHANNEL_SUPPORT_LEVELS) | {"no_record"}
        self.assertEqual(_record_keys(self.source, "SUPPORT_HEADLINES"), expected)

    def test_every_grounding_failure_has_a_plain_language_line(self):
        # `grounded` is deliberately absent: the common case renders no note at
        # all. Every OTHER value, including the `unknown` instrument failure,
        # must have wording of its own.
        expected = (
            set(channel_assessor.CHANNEL_GROUNDING_STATUSES) | {channel_assessor.GROUNDING_UNKNOWN}
        ) - {channel_assessor.GROUNDING_GROUNDED}
        self.assertEqual(_record_keys(self.source, "GROUNDING_NOTES"), expected)

    def test_pin_cannot_rot_to_an_empty_comparison(self):
        # Positive control: the extractor must actually find keys, so a refactor
        # that renames the TS constants fails loudly instead of comparing two
        # empty sets and passing.
        self.assertGreater(len(_record_keys(self.source, "SUPPORT_HEADLINES")), 0)
        with self.assertRaises(ValueError):
            _record_keys(self.source, "A_CONSTANT_THAT_DOES_NOT_EXIST")


if __name__ == "__main__":
    unittest.main()
