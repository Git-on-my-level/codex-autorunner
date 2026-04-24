"""Contract tests for `scripts/ui_qa/generate_manifest.py` (``make ui-qa-screens``)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
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


def _expected_ui_mock_screenshot_names() -> list[str]:
    mod = _load_generate_module()
    order = list(mod.UI_MOCK_SCENARIO_ORDER_FALLBACK)
    gen = (
        _repo_root()
        / "src"
        / "codex_autorunner"
        / "static"
        / "generated"
        / "uiMockScenarios.js"
    )
    if gen.is_file():
        uri = gen.resolve().as_uri()
        code = (
            "import * as m from " + json.dumps(uri) + "; "
            "process.stdout.write(JSON.stringify(m.UI_MOCK_SCENARIO_ORDER));"
        )
        try:
            raw = subprocess.check_output(
                ["node", "--input-type=module", "-e", code],
                text=True,
                timeout=15,
            )
            order = list(json.loads(raw))
        except (
            OSError,
            FileNotFoundError,
            subprocess.CalledProcessError,
            json.JSONDecodeError,
        ):
            pass
    return [f"{i:02d}-ui-mock-{sid}.png" for i, sid in enumerate(order, start=1)]


def _expected_repo_screenshot_names(hub_screenshot_count: int) -> list[str]:
    stems = (
        "tickets.png",
        "inbox.png",
        "contextspace.png",
        "terminal.png",
        "analytics.png",
        "archive.png",
    )
    # First repo tab uses the number after the last hub-mock index (1-based count).
    first = hub_screenshot_count + 1
    return [f"{first + i:02d}-repo-{s}" for i, s in enumerate(stems)]


def test_ui_qa_generated_manifest_parses_with_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen = _load_generate_module()
    monkeypatch.setenv("UI_QA_UI_MOCKS", "1")
    raw = gen.build_manifest_dict("fixture-repo", _repo_root())
    manifest = _write_and_load(raw, tmp_path)
    assert manifest.version == 1
    for step in manifest.steps:
        assert step.action in SUPPORTED_V1_ACTIONS, step.action
    names = [s.data.get("output") for s in manifest.steps if s.action == "screenshot"]
    hub_names = _expected_ui_mock_screenshot_names()
    n_hub = len(hub_names)
    assert names == hub_names + _expected_repo_screenshot_names(n_hub)
    gotos = [s.data.get("url") for s in manifest.steps if s.action == "goto"]
    # First hub shot is the empty mock
    assert "uiMock=empty" in (gotos[0] or "")
    assert any("pma-agents-ok" in (u or "") and "view=pma" in (u or "") for u in gotos)
    assert any(
        "uiMock=onboarding" in (u or "")
        and "view=pma" in (u or "")
        and "carOnboarding=1" in (u or "")
        for u in gotos
    )
    assert gotos[n_hub] == "/repos/fixture-repo/?tab=tickets"
    assert gotos[-1] == "/repos/fixture-repo/?tab=archive"


def test_ui_qa_generated_manifest_parses_hub_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen = _load_generate_module()
    monkeypatch.setenv("UI_QA_UI_MOCKS", "1")
    raw = gen.build_manifest_dict(None, _repo_root())
    manifest = _write_and_load(raw, tmp_path)
    names = [s.data.get("output") for s in manifest.steps if s.action == "screenshot"]
    assert names == _expected_ui_mock_screenshot_names()


def test_ui_mock_fallback_list_matches_node_export() -> None:
    """``UI_MOCK_SCENARIO_ORDER_FALLBACK`` must stay in sync with ``static_src/uiMockScenarios.ts``."""
    gen = _load_generate_module()
    gen_root = _repo_root()
    mod_path = (
        gen_root
        / "src"
        / "codex_autorunner"
        / "static"
        / "generated"
        / "uiMockScenarios.js"
    )
    if not mod_path.is_file():
        pytest.skip("run pnpm build to generate uiMockScenarios.js")
    uri = mod_path.resolve().as_uri()
    code = (
        "import * as m from " + json.dumps(uri) + "; "
        "process.stdout.write(JSON.stringify(m.UI_MOCK_SCENARIO_ORDER));"
    )
    try:
        raw = subprocess.check_output(
            ["node", "--input-type=module", "-e", code],
            text=True,
            timeout=15,
        )
    except (OSError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"node or import failed: {exc!r}")
    from_node = json.loads(raw)
    assert list(from_node) == list(gen.UI_MOCK_SCENARIO_ORDER_FALLBACK)


def test_ui_qa_skip_ui_mocks_single_hub_shot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen = _load_generate_module()
    monkeypatch.setenv("UI_QA_UI_MOCKS", "0")
    raw = gen.build_manifest_dict("fixture-repo", _repo_root())
    manifest = _write_and_load(raw, tmp_path)
    names = [s.data.get("output") for s in manifest.steps if s.action == "screenshot"]
    assert names[0] == "01-hub-home.png"
    assert len(names) == 1 + 6
