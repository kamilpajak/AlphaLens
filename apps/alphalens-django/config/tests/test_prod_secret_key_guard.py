"""Guard the prod-settings fail-fast on the dev-only SECRET_KEY fallback.

If a misconfigured prod container starts without ``SECRET_KEY`` in env, the
base settings transparently fall back to ``"dev-only-insecure-..."`` — a
known key that silently weakens cookie / CSRF / PRNG signing. ``prod.py``
detects the fallback at import time and raises ``ImproperlyConfigured``.

This test fires a fresh prod-settings import in an env that strips the
``SECRET_KEY`` variable, and asserts the guard activates. Run via
``pytest config/tests/test_prod_secret_key_guard.py``.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured


class TestProdSecretKeyGuard(unittest.TestCase):
    def test_raises_when_secret_key_uses_dev_fallback(self) -> None:
        """``prod.py`` import must raise if SECRET_KEY is the dev fallback."""
        # Strip SECRET_KEY + DEBUG + ALLOWED_HOSTS so base.py's defaults
        # produce the dev fallback. patch.dict with clear=True wipes the
        # process env, restoring it on exit.
        keep = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SECRET_KEY", "DEBUG", "ALLOWED_HOSTS"))
        }
        keep["ALLOWED_HOSTS"] = "localhost"  # base.py reads ALLOWED_HOSTS too
        # ``patch.dict(sys.modules)`` snapshots the module cache and restores
        # it on context exit so transient settings re-imports don't poison
        # downstream tests that depend on the cached config.settings.dev.
        with (
            patch.dict(os.environ, keep, clear=True),
            patch.dict(sys.modules),
        ):
            for mod in [m for m in list(sys.modules) if m.startswith("config.settings")]:
                del sys.modules[mod]
            with self.assertRaises(ImproperlyConfigured) as ctx:
                importlib.import_module("config.settings.prod")
            self.assertIn("SECRET_KEY", str(ctx.exception))

    def test_raises_when_secret_key_is_empty(self) -> None:
        """An explicit empty ``SECRET_KEY=""`` must also fail closed.

        An exact-match-only guard (``== sentinel``) would let an empty key
        through and boot with no signing key; ``prod.py`` now also rejects a
        falsy key.
        """
        keep = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SECRET_KEY", "DEBUG", "ALLOWED_HOSTS"))
        }
        keep["ALLOWED_HOSTS"] = "localhost"
        keep["SECRET_KEY"] = ""  # set-but-empty: not the sentinel, still insecure
        with (
            patch.dict(os.environ, keep, clear=True),
            patch.dict(sys.modules),
        ):
            for mod in [m for m in list(sys.modules) if m.startswith("config.settings")]:
                del sys.modules[mod]
            with self.assertRaises(ImproperlyConfigured) as ctx:
                importlib.import_module("config.settings.prod")
            self.assertIn("SECRET_KEY", str(ctx.exception))
