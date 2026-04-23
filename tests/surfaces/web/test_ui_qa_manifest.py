"""Contract tests for `scripts/ui_qa/generate_manifest.py` (``make ui-qa-screens``)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from codex_autorunner.browser.actions import SUPPORTED_V1_ACTIONS, load_demo_manifest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_generate_module():
    path = _repo_root() / "scripts" / "ui_qa" / "generate_manifest.py"
    spec = importlib.util.spec_from_file_location("ui_qa_generate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load generate_manifest")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_and_load(manifest: dict, tmp_path: Path) -> object:
    p = tmp_path / "ui_qa.yaml"
    p.write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return load_demo_manifest(p)


def test_ui_qa_generate_module_exists() -> None:
    assert (_repo_root() / "scripts" / "ui_qa" / "generate_manifest.py").is_file()


def test_ui_qa_generated_manifest_parses_with_repo(tmp_path: Path) -> None:
    gen = _load_generate_module()
    raw = gen.build_manifest_dict("fixture-repo")
    manifest = _write_and_load(raw, tmp_path)
    assert manifest.version == 1
    for step in manifest.steps:
        assert step.action in SUPPORTED_V1_ACTIONS, step.action
    names = [s.data.get("output") for s in manifest.steps if s.action == "screenshot"]
    assert names == [
        "01-hub-home.png",
        "02-tickets.png",
        "03-inbox.png",
        "04-contextspace.png",
        "05-terminal.png",
        "06-analytics.png",
        "07-archive.png",
    ]
    gotos = [s.data.get("url") for s in manifest.steps if s.action == "goto"]
    assert gotos[0] == "/"
    assert gotos[1] == "/repos/fixture-repo/?tab=tickets"
    assert gotos[-1] == "/repos/fixture-repo/?tab=archive"


def test_ui_qa_generated_manifest_parses_hub_only(tmp_path: Path) -> None:
    gen = _load_generate_module()
    raw = gen.build_manifest_dict(None)
    manifest = _write_and_load(raw, tmp_path)
    names = [s.data.get("output") for s in manifest.steps if s.action == "screenshot"]
    assert names == ["01-hub-home.png"]
