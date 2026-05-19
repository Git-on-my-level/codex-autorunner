"""Contract tests for ``scripts/web_ui_dev_smoke.py``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_module():
    path = _repo_root() / "scripts" / "web_ui_dev_smoke.py"
    spec = importlib.util.spec_from_file_location("web_ui_dev_smoke", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load web_ui_dev_smoke.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_dev_smoke_routes_stay_small() -> None:
    mod = _load_module()

    assert mod.FAILURE_MARKERS == ("Internal Error", "Invalid export")
    assert mod.DEFAULT_VITE_URL == "http://127.0.0.1:5173"


def test_dev_smoke_is_documented() -> None:
    docs = (_repo_root() / "src/codex_autorunner/web_frontend/AGENTS.md").read_text(
        encoding="utf-8"
    )

    assert "pnpm web:smoke:dev" in docs
    assert "pageModuleExports.test.ts" in docs
