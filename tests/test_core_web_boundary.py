from __future__ import annotations

import importlib
import sys


def test_core_engine_does_not_import_web_modules(monkeypatch):
    # Ensure any previously loaded web modules don't mask regressions.
    for name in list(sys.modules):
        if name.startswith("codex_autorunner.web"):
            sys.modules.pop(name, None)

    importlib.invalidate_caches()

    import codex_autorunner.core.engine  # noqa: F401

    leaked = [name for name in sys.modules if name.startswith("codex_autorunner.web")]
    assert not leaked, f"core.engine should not import web modules, found {leaked}"
