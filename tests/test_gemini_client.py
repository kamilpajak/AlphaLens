"""Tests for the canonical GeminiClient.

The Google Gen AI SDK (google-genai) is mocked at module-load time via
``sys.modules`` so these tests run with or without the real SDK installed
— the canonical client owns the import boundary, and the tests exercise
the wrapper logic, not the SDK itself.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _install_fake_genai() -> tuple[MagicMock, MagicMock]:
    """Install a fake ``google.genai`` + ``google.genai.types`` into
    ``sys.modules`` so ``from google import genai`` resolves to the mock.

    Returns the mock ``genai`` module and a mock ``types`` module so each
    test can configure return values on ``genai.Client(...)`` etc.
    """
    fake_google = types.ModuleType("google")
    fake_genai = types.ModuleType("google.genai")
    fake_genai_types = types.ModuleType("google.genai.types")

    # Stand-in Client class so genai.Client(api_key=...) is observable.
    fake_genai.Client = MagicMock(name="genai.Client")
    fake_genai.types = fake_genai_types

    # GenerateContentConfig is just a passthrough type in tests; record the
    # call args so test_build_config_passes_through can verify them.
    fake_genai_types.GenerateContentConfig = MagicMock(name="types.GenerateContentConfig")

    sys.modules["google"] = fake_google
    sys.modules["google.genai"] = fake_genai
    sys.modules["google.genai.types"] = fake_genai_types
    fake_google.genai = fake_genai

    return fake_genai, fake_genai_types


def _uninstall_fake_genai() -> None:
    for name in ("google.genai.types", "google.genai", "google"):
        sys.modules.pop(name, None)


class _FakeGenaiTestCase(unittest.TestCase):
    """Base test case: installs a fake SDK around each test and resets the
    canonical client's lazy singleton + module-level SDK cache."""

    def setUp(self):
        self.fake_genai, self.fake_types = _install_fake_genai()
        from alphalens.data.alt_data import gemini_client as mod

        mod._reset_default_client_for_tests()
        mod._reset_sdk_cache_for_tests()

    def tearDown(self):
        from alphalens.data.alt_data import gemini_client as mod

        mod._reset_default_client_for_tests()
        mod._reset_sdk_cache_for_tests()
        _uninstall_fake_genai()


class TestClientConstruction(_FakeGenaiTestCase):
    def test_constructor_api_key_required(self):
        from alphalens.data.alt_data.gemini_client import GeminiClient

        with self.assertRaises(ValueError):
            GeminiClient(api_key="")

    def test_constructor_builds_underlying_sdk_client(self):
        from alphalens.data.alt_data.gemini_client import GeminiClient

        client = GeminiClient(api_key="DEMO")
        # genai.Client(api_key="DEMO") should have been called once.
        self.fake_genai.Client.assert_called_once_with(api_key="DEMO")
        self.assertIs(client.sdk_client, self.fake_genai.Client.return_value)

    def test_from_env_reads_google_api_key(self):
        from alphalens.data.alt_data.gemini_client import GeminiClient

        with patch.dict("os.environ", {"GOOGLE_API_KEY": "envkey"}, clear=False):
            client = GeminiClient.from_env()

        self.fake_genai.Client.assert_called_once_with(api_key="envkey")
        self.assertIsNotNone(client.sdk_client)

    def test_from_env_raises_when_env_missing(self):
        from alphalens.data.alt_data.gemini_client import GeminiClient

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                GeminiClient.from_env()

    def test_lazy_sdk_import_raises_with_actionable_message(self):
        """If google-genai is not installed, _load_genai_sdk must raise with
        the canonical actionable message. google-genai is a hard dep in
        pyproject so the real import succeeds — we patch the import
        machinery to simulate absence."""
        from alphalens.data.alt_data import gemini_client as mod

        mod._reset_sdk_cache_for_tests()
        _uninstall_fake_genai()

        real_import = (
            __builtins__["__import__"]
            if isinstance(__builtins__, dict)
            else __builtins__.__import__
        )

        def fake_import(name, *args, **kw):
            if name == "google" or name.startswith("google."):
                raise ImportError("simulated missing google-genai")
            return real_import(name, *args, **kw)

        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(RuntimeError) as cm:
                mod.GeminiClient(api_key="DEMO")
        self.assertIn("google-genai", str(cm.exception).lower())


class TestGenerateContent(_FakeGenaiTestCase):
    def test_generate_content_forwards_to_sdk(self):
        from alphalens.data.alt_data.gemini_client import GeminiClient

        client = GeminiClient(api_key="DEMO")
        sdk_client = client.sdk_client
        # The real call is sdk_client.models.generate_content(...). MagicMock
        # auto-creates the attribute chain.
        sdk_client.models.generate_content.return_value = MagicMock(text='{"ok": true}')

        config_obj = MagicMock(name="config")
        response = client.generate_content(
            model="gemini-3-pro-preview",
            contents="hello",
            config=config_obj,
        )

        sdk_client.models.generate_content.assert_called_once_with(
            model="gemini-3-pro-preview",
            contents="hello",
            config=config_obj,
        )
        self.assertEqual(response.text, '{"ok": true}')

    def test_build_config_passes_through_to_types_module(self):
        """build_config is a convenience around types.GenerateContentConfig —
        callers shouldn't need to import the SDK types module just to build
        a config dict. Verify the kwargs reach the SDK."""
        from alphalens.data.alt_data.gemini_client import GeminiClient

        client = GeminiClient(api_key="DEMO")
        config = client.build_config(
            response_mime_type="application/json",
            response_schema={"type": "object"},
            temperature=0.0,
            max_output_tokens=2000,
        )

        self.fake_types.GenerateContentConfig.assert_called_once_with(
            response_mime_type="application/json",
            response_schema={"type": "object"},
            temperature=0.0,
            max_output_tokens=2000,
        )
        self.assertIs(config, self.fake_types.GenerateContentConfig.return_value)


class TestDefaultClientSingleton(_FakeGenaiTestCase):
    def test_get_default_returns_same_instance(self):
        from alphalens.data.alt_data.gemini_client import get_default_gemini_client

        with patch.dict("os.environ", {"GOOGLE_API_KEY": "envkey"}, clear=False):
            c1 = get_default_gemini_client()
            c2 = get_default_gemini_client()
        self.assertIs(c1, c2)
        # Only one underlying SDK client was constructed despite two callers.
        self.assertEqual(self.fake_genai.Client.call_count, 1)

    def test_get_default_raises_without_env(self):
        from alphalens.data.alt_data.gemini_client import get_default_gemini_client

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                get_default_gemini_client()


if __name__ == "__main__":
    unittest.main()
