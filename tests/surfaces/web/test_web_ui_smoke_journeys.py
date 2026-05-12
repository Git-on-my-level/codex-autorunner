"""Contract tests for ``scripts/web_ui_smoke_journeys.py``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_module():
    path = _repo_root() / "scripts" / "web_ui_smoke_journeys.py"
    spec = importlib.util.spec_from_file_location("web_ui_smoke_journeys", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load web_ui_smoke_journeys.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_smoke_routes_stay_intentionally_small() -> None:
    mod = _load_module()

    assert [name for name, _path, _text in mod.SMOKE_ROUTES] == [
        "hub",
        "repos",
        "repo-detail",
        "worktree-detail",
        "ticket-detail",
        "contextspace",
    ]
    assert len(mod.SMOKE_ROUTES) < 10


def test_smoke_runner_documents_failure_artifacts() -> None:
    docs = (_repo_root() / "docs" / "ops" / "web-ui-qa.md").read_text(encoding="utf-8")

    assert "make web-ui-smoke" in docs
    assert ".codex-autorunner/render/web_ui_smoke/latest/manifest.json" in docs
    assert "Trace, video, and failure screenshots are retained only" in docs
