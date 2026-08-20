"""Hermetic unit tests for the L3 VCR cassette infra (test-strategy Phase 3).

No network, no live capture: a fake real client returns canned OpenRouter
payloads, RecordingOpenRouter tees them to a tmp cassette dir, and
ReplayOpenRouter serves them back byte-identically. Pins the two memo §10 rules:
the cassette key covers the FULL request descriptor (model + contents + config),
and a miss fails loud rather than silently calling out.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from alphalens_pipeline.data.alt_data.openrouter_client import (
    OpenRouterClient,
    _wrap_response,
)

from tests.golden.replay_client import (
    CassetteMissError,
    RecordingOpenRouter,
    ReplayOpenRouter,
    cassette_is_truncated,
    cassette_key,
    load_final_answer_cassette_records,
)

_MODEL = "deepseek/deepseek-v4-flash"


def _payload(content: str, finish_reason: str = "stop") -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


class _FakeRealClient:
    """Stands in for OpenRouterClient: returns a scripted payload per call."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls = 0

    def build_config(self, **kwargs):
        # Delegate to the real (self-independent) translation, mirroring how the
        # production OpenRouterClient builds configs.
        return OpenRouterClient.build_config(self, **kwargs)  # type: ignore[arg-type]

    def generate_content(self, *, model, contents, config=None) -> SimpleNamespace:
        self.calls += 1
        return _wrap_response(self._payload)


class TestRecordReplayRoundTrip(unittest.TestCase):
    def test_recorded_response_replays_identically(self):
        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td)
            fake = _FakeRealClient(_payload('{"event_type": "m_and_a"}'))
            recorder = RecordingOpenRouter(fake, cdir)
            cfg = recorder.build_config(
                response_mime_type="application/json", max_output_tokens=8000
            )

            live = recorder.generate_content(model=_MODEL, contents="news A", config=cfg)
            self.assertEqual(live.text, '{"event_type": "m_and_a"}')
            self.assertEqual(len(list(cdir.glob("*.json"))), 1)

            # Replay from disk: no real client involved, byte-identical response.
            replay = ReplayOpenRouter(cdir)
            self.assertEqual(len(replay), 1)
            got = replay.generate_content(model=_MODEL, contents="news A", config=cfg)
            self.assertEqual(got.text, live.text)
            self.assertEqual(got.candidates[0].finish_reason.name, "STOP")

    def test_max_tokens_finish_reason_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td)
            fake = _FakeRealClient(_payload("partial", finish_reason="length"))
            recorder = RecordingOpenRouter(fake, cdir)
            cfg = recorder.build_config(max_output_tokens=10)
            recorder.generate_content(model=_MODEL, contents="x", config=cfg)

            replay = ReplayOpenRouter(cdir)
            got = replay.generate_content(model=_MODEL, contents="x", config=cfg)
            # OpenRouter "length" → Gemini "MAX_TOKENS"; the brief retry logic
            # keys on this, so replay must preserve it.
            self.assertEqual(got.candidates[0].finish_reason.name, "MAX_TOKENS")


class TestFailLoudOnMiss(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        cdir = Path(self._tmp.name)
        fake = _FakeRealClient(_payload('{"ok": true}'))
        rec = RecordingOpenRouter(fake, cdir)
        self.cfg = rec.build_config(temperature=0.0)
        rec.generate_content(model=_MODEL, contents="recorded prompt", config=self.cfg)
        self.cdir = cdir

    def tearDown(self):
        self._tmp.cleanup()

    def test_unrecorded_contents_raises(self):
        replay = ReplayOpenRouter(self.cdir)
        with self.assertRaises(CassetteMissError):
            replay.generate_content(model=_MODEL, contents="NEVER recorded", config=self.cfg)

    def test_config_param_is_part_of_key(self):
        # Same model + contents, DIFFERENT temperature → different key → miss.
        # This is the memo §10 lesson: keying on prompt alone misses sampling
        # params that change the output.
        replay = ReplayOpenRouter(self.cdir)
        other_cfg = ReplayOpenRouter(self.cdir).build_config(temperature=0.7)
        with self.assertRaises(CassetteMissError):
            replay.generate_content(model=_MODEL, contents="recorded prompt", config=other_cfg)

    def test_different_model_is_part_of_key(self):
        replay = ReplayOpenRouter(self.cdir)
        with self.assertRaises(CassetteMissError):
            replay.generate_content(
                model="deepseek/deepseek-v4-pro", contents="recorded prompt", config=self.cfg
            )

    def test_fail_on_miss_false_returns_empty(self):
        replay = ReplayOpenRouter(self.cdir, fail_on_miss=False)
        got = replay.generate_content(model=_MODEL, contents="unrecorded", config=self.cfg)
        self.assertEqual(got.text, "")


class TestBuildConfigDelegation(unittest.TestCase):
    def test_json_mode_synthesises_system_message_with_schema(self):
        # ReplayOpenRouter.build_config delegates to the real (self-independent)
        # translation, so JSON mode produces response_format + a system message
        # containing 'json' + the schema — identical to production.
        cfg = ReplayOpenRouter.build_config(
            ReplayOpenRouter.__new__(ReplayOpenRouter),
            response_mime_type="application/json",
            response_schema={"type": "object", "properties": {"x": {"type": "string"}}},
            temperature=0.2,
            max_output_tokens=2000,
        )
        self.assertEqual(cfg.response_format, {"type": "json_object"})
        self.assertEqual(cfg.temperature, 0.2)
        self.assertEqual(cfg.max_tokens, 2000)
        self.assertIn("json", cfg.system_message.lower())
        self.assertIn('"x"', cfg.system_message)

    def test_key_stable_across_equal_descriptors(self):
        cfg_a = ReplayOpenRouter.build_config(
            ReplayOpenRouter.__new__(ReplayOpenRouter), temperature=0.0, max_output_tokens=100
        )
        cfg_b = ReplayOpenRouter.build_config(
            ReplayOpenRouter.__new__(ReplayOpenRouter), temperature=0.0, max_output_tokens=100
        )
        self.assertEqual(
            cassette_key(model=_MODEL, contents="same", config=cfg_a),
            cassette_key(model=_MODEL, contents="same", config=cfg_b),
        )


class TestTruncatedCassetteDetection(unittest.TestCase):
    """A cassette on disk is not automatically a final answer.

    ``generate_brief_with_retry`` records EVERY draw it makes, including a
    truncated (``finish_reason == "length"``) attempt it discards before
    retrying at a higher token cap. A caller that globs every cassette and
    parses its ``content`` unconditionally is exactly the defect this pins:
    a real CRSP recording (2026-08-19 golden re-baseline) truncated at the
    base 2000-token cap, succeeded on retry at 4000, and both cassettes were
    written to the SAME directory — the eval-golden loaders that glob-and-
    parse crashed on the truncated one with ``Unterminated string``.
    """

    def test_stop_finish_reason_is_not_truncated(self):
        record = {"openrouter_response": _payload('{"tldr": "ok"}', finish_reason="stop")}
        self.assertFalse(cassette_is_truncated(record))

    def test_length_finish_reason_is_truncated(self):
        record = {"openrouter_response": _payload('{"tldr": "cut off', finish_reason="length")}
        self.assertTrue(cassette_is_truncated(record))

    def test_missing_choices_is_not_truncated(self):
        # A cassette recorded for a non-brief call (e.g. a map-themes stage)
        # may carry no `choices` at all under a hand-built payload; absence
        # must not be misread as truncation.
        self.assertFalse(cassette_is_truncated({"openrouter_response": {}}))

    def test_load_final_answer_cassette_records_skips_truncated(self):
        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td)
            # The discarded truncated attempt at the base cap.
            fake_truncated = _FakeRealClient(_payload('{"tldr": "cut off', finish_reason="length"))
            recorder = RecordingOpenRouter(fake_truncated, cdir)
            cfg_base = recorder.build_config(max_output_tokens=2000)
            recorder.generate_content(model=_MODEL, contents="CRSP facts", config=cfg_base)

            # The successful retry at an escalated cap — a DIFFERENT cassette
            # key (max_tokens differs), same directory.
            fake_ok = _FakeRealClient(_payload('{"tldr": "ok"}', finish_reason="stop"))
            recorder_ok = RecordingOpenRouter(fake_ok, cdir)
            cfg_retry = recorder_ok.build_config(max_output_tokens=4000)
            recorder_ok.generate_content(model=_MODEL, contents="CRSP facts", config=cfg_retry)

            self.assertEqual(len(list(cdir.glob("*.json"))), 2)
            final = load_final_answer_cassette_records(cdir)
            self.assertEqual(len(final), 1)
            self.assertEqual(final[0]["openrouter_response"]["choices"][0]["finish_reason"], "stop")

    def test_load_final_answer_cassette_records_empty_dir(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_final_answer_cassette_records(Path(td)), [])


if __name__ == "__main__":
    unittest.main()
